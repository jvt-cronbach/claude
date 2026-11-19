"""Speaker diarization and embeddings (sherpa-onnx backend + pure helpers)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import numpy as np

from callrec.models import SpeakerTurn

log = logging.getLogger(__name__)

ProgressFn = Callable[[float], None]


class Diarizer(Protocol):
    def diarize(self, samples: np.ndarray, sample_rate: int, progress: ProgressFn) -> list[SpeakerTurn]: ...


class Embedder(Protocol):
    model_id: str

    def embed(self, samples: np.ndarray, sample_rate: int) -> np.ndarray: ...


def renumber_turns(turns: list[SpeakerTurn]) -> list[SpeakerTurn]:
    """Speaker ids S1..Sn in order of first appearance."""
    mapping: dict[str, str] = {}
    out = []
    for t in sorted(turns, key=lambda t: (t.start, t.end)):
        if t.speaker not in mapping:
            mapping[t.speaker] = f"S{len(mapping) + 1}"
        out.append(SpeakerTurn(t.start, t.end, mapping[t.speaker]))
    return out


def l2_normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def speaker_embeddings(
    turns: list[SpeakerTurn],
    samples: np.ndarray,
    sample_rate: int,
    embedder: Embedder,
    min_segment_s: float = 1.0,
    max_segment_s: float = 10.0,
    max_total_s: float = 90.0,
) -> dict[str, np.ndarray]:
    """One L2-normalised embedding per speaker (mean of per-turn embeddings).

    Long turns are preferred; if a speaker only has short turns, they are
    concatenated so that there is still enough audio for one embedding.
    """
    by_speaker: dict[str, list[SpeakerTurn]] = {}
    for t in turns:
        by_speaker.setdefault(t.speaker, []).append(t)
    result: dict[str, np.ndarray] = {}
    for spk, spk_turns in by_speaker.items():
        spk_turns = sorted(spk_turns, key=lambda t: t.end - t.start, reverse=True)
        vectors: list[np.ndarray] = []
        total = 0.0
        short_parts: list[np.ndarray] = []
        for t in spk_turns:
            dur = t.end - t.start
            s = int(t.start * sample_rate)
            e = int(min(t.end, t.start + max_segment_s) * sample_rate)
            clip = samples[s:e]
            if clip.size == 0:
                continue
            if dur >= min_segment_s and total < max_total_s:
                try:
                    vectors.append(l2_normalize(embedder.embed(clip, sample_rate)))
                    total += min(dur, max_segment_s)
                except Exception:
                    log.debug("Embedding für %s fehlgeschlagen", spk, exc_info=True)
            else:
                short_parts.append(clip)
        if not vectors and short_parts:
            joined = np.concatenate(short_parts)[: int(max_segment_s * sample_rate)]
            try:
                vectors.append(l2_normalize(embedder.embed(joined, sample_rate)))
            except Exception:
                log.info("Zu wenig Audio für ein Stimmprofil von %s", spk)
        if vectors:
            result[spk] = l2_normalize(np.mean(np.stack(vectors), axis=0))
    return result


class SherpaEmbedder:
    def __init__(self, model_path: Path, model_id: str, num_threads: int = 2) -> None:
        import sherpa_onnx

        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(model_path), num_threads=num_threads, debug=False, provider="cpu"
        )
        if not config.validate():
            raise RuntimeError(f"Ungültige Embedding-Konfiguration: {model_path}")
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self.model_id = model_id

    def embed(self, samples: np.ndarray, sample_rate: int) -> np.ndarray:
        stream = self._extractor.create_stream()
        stream.accept_waveform(sample_rate=sample_rate, waveform=np.ascontiguousarray(samples, dtype=np.float32))
        stream.input_finished()
        if not self._extractor.is_ready(stream):
            raise ValueError("Zu wenig Audio für ein Embedding")
        return np.asarray(self._extractor.compute(stream), dtype=np.float32)


class SherpaDiarizer:
    def __init__(
        self,
        segmentation_model: Path,
        embedding_model: Path,
        cluster_threshold: float = 0.4,
        num_speakers: int = 0,
        min_duration_on: float = 0.3,
        min_duration_off: float = 0.5,
        num_threads: int = 2,
    ) -> None:
        import sherpa_onnx

        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(segmentation_model)),
                num_threads=num_threads,
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(embedding_model), num_threads=num_threads),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=num_speakers if num_speakers > 0 else -1, threshold=cluster_threshold
            ),
            min_duration_on=min_duration_on,
            min_duration_off=min_duration_off,
        )
        if not config.validate():
            raise RuntimeError("Ungültige Diarisierungs-Konfiguration (Modelldateien prüfen)")
        self._sd = sherpa_onnx.OfflineSpeakerDiarization(config)

    @property
    def sample_rate(self) -> int:
        return int(self._sd.sample_rate)

    def diarize(self, samples: np.ndarray, sample_rate: int, progress: ProgressFn = lambda f: None) -> list[SpeakerTurn]:
        if sample_rate != self.sample_rate:
            import soxr

            samples = soxr.resample(samples, sample_rate, self.sample_rate).astype(np.float32)

        def cb(done: int, total: int) -> int:
            if total:
                progress(done / total)
            return 0

        result = self._sd.process(np.ascontiguousarray(samples, dtype=np.float32), callback=cb).sort_by_start_time()
        turns = [SpeakerTurn(float(r.start), float(r.end), f"raw{int(r.speaker)}") for r in result]
        return renumber_turns(turns)
