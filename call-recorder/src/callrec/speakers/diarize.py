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


def clean_intervals(turns: list[SpeakerTurn]) -> dict[str, list[tuple[float, float]]]:
    """Per speaker: the parts of its turns where no *other* speaker's turn overlaps.

    sherpa-onnx reports overlapping speech as overlapping turns. An embedding taken over an
    overlap is a mix of two voices - it pulls the speaker's embedding (and any profile learned
    from it) towards the other person, which is one way profiles end up matching the wrong people.
    """
    out: dict[str, list[tuple[float, float]]] = {}
    for t in turns:
        pieces = [(t.start, t.end)]
        for o in turns:
            if o.speaker == t.speaker or o.end <= t.start or o.start >= t.end:
                continue
            nxt: list[tuple[float, float]] = []
            for s, e in pieces:
                if o.end <= s or o.start >= e:
                    nxt.append((s, e))
                    continue
                if o.start > s:
                    nxt.append((s, o.start))
                if o.end < e:
                    nxt.append((o.end, e))
            pieces = nxt
        out.setdefault(t.speaker, []).extend(p for p in pieces if p[1] - p[0] > 0.05)
    return out


def speech_seconds(turns: list[SpeakerTurn]) -> dict[str, float]:
    """Seconds of clean (non-overlapped) speech per speaker."""
    return {spk: round(sum(e - s for s, e in iv), 2) for spk, iv in clean_intervals(turns).items()}


def embed_spans(
    spans: list[tuple[float, float]],
    samples: np.ndarray,
    sample_rate: int,
    embedder: Embedder,
    min_segment_s: float = 1.0,
    max_segment_s: float = 10.0,
    max_total_s: float = 90.0,
    label: str = "",
) -> np.ndarray | None:
    """L2-normalised mean embedding over ``spans`` (longest first, up to ``max_total_s``).

    If there are only short spans, they are concatenated so that there is still enough audio."""
    vectors: list[np.ndarray] = []
    total = 0.0
    short_parts: list[np.ndarray] = []
    for s0, e0 in sorted(spans, key=lambda x: x[1] - x[0], reverse=True):
        dur = e0 - s0
        s = int(s0 * sample_rate)
        e = int(min(e0, s0 + max_segment_s) * sample_rate)
        clip = samples[s:e]
        if clip.size == 0:
            continue
        if dur >= min_segment_s:
            if total >= max_total_s:
                break
            try:
                vectors.append(l2_normalize(embedder.embed(clip, sample_rate)))
                total += min(dur, max_segment_s)
            except Exception:
                log.debug("Embedding für %s fehlgeschlagen", label, exc_info=True)
        else:
            short_parts.append(clip)
    if not vectors and short_parts:
        joined = np.concatenate(short_parts)[: int(max_segment_s * sample_rate)]
        try:
            vectors.append(l2_normalize(embedder.embed(joined, sample_rate)))
        except Exception:
            log.info("Zu wenig Audio für ein Stimmprofil von %s", label)
    if not vectors:
        return None
    return l2_normalize(np.mean(np.stack(vectors), axis=0))


def speaker_embeddings(
    turns: list[SpeakerTurn],
    samples: np.ndarray,
    sample_rate: int,
    embedder: Embedder,
    min_segment_s: float = 1.0,
    max_segment_s: float = 10.0,
    max_total_s: float = 90.0,
) -> dict[str, np.ndarray]:
    """One L2-normalised embedding per speaker (mean of per-turn embeddings), computed only
    over speech where no other speaker overlaps (see :func:`clean_intervals`)."""
    result: dict[str, np.ndarray] = {}
    for spk, spans in clean_intervals(turns).items():
        v = embed_spans(spans, samples, sample_rate, embedder, min_segment_s, max_segment_s, max_total_s, spk)
        if v is not None:
            result[spk] = v
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
        self.num_threads = num_threads

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
        self.num_threads = num_threads

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
