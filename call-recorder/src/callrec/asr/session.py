"""One transcription session in the worker: model loaded once, both tracks, parallel lanes.

What a session does for a recording (all in the one child process, see :mod:`callrec.asr.worker`):

1. decode each track once (faster-whisper's ``decode_audio``, 16 kHz) and run the Silero VAD once
   per track - the same speech chunks serve the language detection and the transcription;
2. language detection on speech windows of both tracks (stops early once a track's windows agree
   clearly), decision with :func:`callrec.asr.language.decide`;
3. a track without any speech is not transcribed at all;
4. the speech of both tracks is cut at VAD pauses into pieces; ``lanes`` threads take pieces from
   one queue (largest first) and transcribe them on the same model (CTranslate2 ``num_workers``
   replicas share the weights). Each piece is transcribed exactly like faster-whisper's own VAD
   path: the speech chunks are concatenated, transcribed, and the times mapped back with
   ``restore_speech_timestamps``.

The number of working lanes can be lowered while a recording/call runs (``LaneGate``): the
parent writes ``LANES <n>`` to the worker's stdin (0 = pause after the current piece).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

SR = 16000
PIECE_MIN_S = 60.0
PIECE_MAX_S = 600.0
CLIP_MAX_S = 30.0
DETECT_MIN_WINDOWS = 2
DETECT_STOP_PROB = 0.9


@dataclass
class Piece:
    track: str
    chunks: list[dict[str, int]]  # VAD chunks, absolute sample positions in the track
    index: int = 0

    @property
    def speech_s(self) -> float:
        return sum(int(c["end"]) - int(c["start"]) for c in self.chunks) / SR

    @property
    def span(self) -> tuple[float, float]:
        return int(self.chunks[0]["start"]) / SR, int(self.chunks[-1]["end"]) / SR


def plan_pieces(chunks_by_track: dict[str, list[dict[str, int]]], lanes: int,
                min_s: float = PIECE_MIN_S, max_s: float = PIECE_MAX_S) -> list[Piece]:
    """Cut each track's speech at VAD pauses into pieces; largest first (balanced lanes).

    One lane: pieces of up to ``max_s`` speech (only for progress and pausing). Several lanes:
    about three pieces per lane, at least ``min_s`` each, so that both lanes end together."""
    total = sum(sum(int(c["end"]) - int(c["start"]) for c in ch) for ch in chunks_by_track.values()) / SR
    target = max_s if lanes <= 1 else max(min_s, min(max_s, total / (3 * lanes)))
    pieces: list[Piece] = []
    for track, chunks in chunks_by_track.items():
        cur: list[dict[str, int]] = []
        acc = 0.0
        n = 0
        for c in chunks:
            cur.append(c)
            acc += (int(c["end"]) - int(c["start"])) / SR
            if acc >= target:
                pieces.append(Piece(track, cur, n))
                n += 1
                cur, acc = [], 0.0
        if cur:
            if pieces and pieces[-1].track == track and acc < target / 4:
                pieces[-1].chunks.extend(cur)  # no tiny last piece
            else:
                pieces.append(Piece(track, cur, n))
    return sorted(pieces, key=lambda p: -p.speech_s)


def merge_clips(chunks: list[dict[str, int]], max_s: float = CLIP_MAX_S) -> list[dict[str, float]]:
    """Clips (seconds) on the *concatenated* timeline of ``chunks``, each at most ``max_s`` long -
    what ``collect_chunks(max_duration=30)`` builds for faster-whisper's batched pipeline."""
    clips: list[dict[str, float]] = []
    pos = 0
    start = 0
    for c in chunks:
        n = int(c["end"]) - int(c["start"])
        if pos + n - start > max_s * SR and pos > start:
            clips.append({"start": start / SR, "end": pos / SR})
            start = pos
        pos += n
    if pos > start:
        clips.append({"start": start / SR, "end": pos / SR})
    return clips


class LaneGate:
    """How many lanes may start a new piece (``LANES n`` from the parent; 0 = pause)."""

    def __init__(self, lanes: int, active: int | None = None) -> None:
        self.lanes = lanes
        self._active = lanes if active is None else max(0, min(lanes, active))
        self._cond = threading.Condition()
        self.closed = False

    @property
    def active(self) -> int:
        return self._active

    def set_active(self, n: int) -> None:
        with self._cond:
            self._active = max(0, min(self.lanes, n))
            self._cond.notify_all()

    def wait_turn(self, lane: int, timeout: float | None = None) -> bool:
        """Block while this lane is switched off; False when closed."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while not self.closed and lane >= self._active:
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self._cond.wait(left if left is not None else 1.0)
            return not self.closed

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._cond.notify_all()


def read_control(stream: Iterable[str], gate: LaneGate) -> None:
    """Parent -> worker commands on stdin: ``LANES <n>``."""
    for line in stream:
        parts = line.split()
        if len(parts) == 2 and parts[0] == "LANES":
            try:
                gate.set_active(int(parts[1]))
            except ValueError:
                continue


# -- language detection --------------------------------------------------------------------------
def window_order(count: int) -> list[int]:
    """Indices of ``count`` evenly spread windows in measuring order: first, last, middle, rest.

    With an early stop after two or three windows they still cover the recording."""
    if count <= 0:
        return []
    order: list[int] = []
    for i in [0, count - 1, count // 2, *range(count)]:
        if i not in order:
            order.append(i)
    return order


def clear_agreement(windows: list[dict[str, Any]], candidates: list[str], min_windows: int = DETECT_MIN_WINDOWS,
                    stop_prob: float = DETECT_STOP_PROB, min_speech_s: float = 20.0) -> bool:
    """All measured windows name the same candidate with high probability and enough speech."""
    if len(windows) < min_windows or sum(float(w.get("speech_s") or 0) for w in windows) < min_speech_s:
        return False
    tops = {w.get("top") for w in windows}
    return len(tops) == 1 and next(iter(tops)) in candidates and all(float(w.get("top_prob") or 0) >= stop_prob
                                                                      for w in windows)


def _original_time(offset_samples: int, chunks: list[dict[str, Any]]) -> float:
    pos = 0
    for c in chunks:
        length = int(c["end"]) - int(c["start"])
        if offset_samples < pos + length:
            return (int(c["start"]) + offset_samples - pos) / SR
        pos += length
    return int(chunks[-1]["end"]) / SR if chunks else 0.0


def detect_windows(model: Any, samples: Any, chunks: list[dict[str, Any]], windows: int,
                   candidates: list[str] | None = None, early_stop: bool = False,
                   min_speech_s: float = 20.0) -> dict[str, Any]:
    """Language probabilities of up to ``windows`` 30-s speech windows spread over the track.

    Same building blocks as WhisperModel.detect_language in faster-whisper 1.2.1 (feature
    extractor, pad_or_trim, encode, ctranslate2 ``detect_language``), on windows of *speech*
    spread over the whole recording; with ``early_stop`` it stops once the windows agree clearly."""
    import numpy as np
    from faster_whisper.audio import pad_or_trim

    fe = model.feature_extractor
    duration = len(samples) / SR
    if not chunks:
        return {"duration": round(duration, 2), "speech_s": 0.0, "windows": []}
    speech = np.concatenate([samples[int(c["start"]):int(c["end"])] for c in chunks])
    n = int(fe.n_samples)
    count = max(1, min(windows, -(-len(speech) // n)))
    if len(speech) <= n:
        starts = [0]
    else:
        starts = sorted({int(round(x)) for x in np.linspace(0, len(speech) - n, count)})
    out: list[dict[str, Any]] = []
    for idx in window_order(len(starts)):
        start = starts[idx]
        seg = speech[start:start + n]
        features = fe(seg)
        enc = model.encode(pad_or_trim(features[..., : fe.nb_max_frames]))
        results = model.model.detect_language(enc)[0]
        probs = {tok[2:-2]: float(p) for tok, p in results}
        top = results[0][0][2:-2] if results else ""
        out.append({"start": round(_original_time(start, chunks), 1), "speech_s": round(len(seg) / SR, 1),
                    "top": top, "top_prob": round(probs.get(top, 0.0), 4),
                    "probs": {k: round(v, 5) for k, v in probs.items() if v >= 0.001}})
        if early_stop and candidates and clear_agreement(out, candidates, min_speech_s=min_speech_s):
            break
    out.sort(key=lambda w: w["start"])
    return {"duration": round(duration, 2), "speech_s": round(len(speech) / SR, 1), "windows": out,
            "measured": len(out), "planned": len(starts)}


# -- transcription -------------------------------------------------------------------------------
def segment_dict(seg: Any) -> dict[str, Any]:
    words = [{"start": float(w.start), "end": float(w.end), "text": w.word, "probability": float(w.probability)}
             for w in (seg.words or [])]
    return {"start": float(seg.start), "end": float(seg.end), "text": seg.text.strip(), "words": words,
            "avg_logprob": round(float(seg.avg_logprob), 4),
            "compression_ratio": round(float(seg.compression_ratio), 3),
            "temperature": float(seg.temperature) if seg.temperature is not None else None}


def transcribe_piece(engine: Any, samples: Any, piece: Piece, language: str | None, options: dict[str, Any],
                     batched: bool, on_segment: Callable[[float], None] = lambda f: None) -> list[dict[str, Any]]:
    """Transcribe one piece exactly like faster-whisper's VAD path and return original-time segments.

    ``engine``: WhisperModel (sequential) or BatchedInferencePipeline (``batched``)."""
    import numpy as np
    from faster_whisper.transcribe import restore_speech_timestamps

    audio = np.concatenate([samples[int(c["start"]):int(c["end"])] for c in piece.chunks]).astype(np.float32)
    if batched:
        segments, _info = engine.transcribe(audio, language=language, vad_filter=False,
                                            clip_timestamps=merge_clips(piece.chunks), **options)
    else:
        segments, _info = engine.transcribe(audio, language=language, vad_filter=False, **options)
    s0, s1 = piece.span
    out: list[dict[str, Any]] = []
    for seg in restore_speech_timestamps(segments, piece.chunks, SR):
        if not seg.text.strip() and not seg.words:
            continue
        out.append(segment_dict(seg))
        if s1 > s0:
            on_segment(min(1.0, max(0.0, (float(seg.end) - s0) / (s1 - s0))))
    return out


def decoding_kwargs(dec: dict[str, Any], opts: dict[str, Any], batched: bool) -> dict[str, Any]:
    """faster-whisper 1.2.1 ``transcribe`` keyword arguments for one profile (``dec``) and the
    profile-independent options (``opts``: thresholds, prompt, hotwords ...)."""
    kw: dict[str, Any] = {
        "beam_size": int(dec["beam_size"]),
        "best_of": int(dec.get("best_of", 5)),
        "temperature": list(dec["temperatures"]) if len(dec["temperatures"]) > 1 else float(dec["temperatures"][0]),
        "word_timestamps": bool(dec.get("word_timestamps", True)),
        "compression_ratio_threshold": opts.get("compression_ratio_threshold") or None,
        "log_prob_threshold": opts.get("log_prob_threshold") or None,
        "no_speech_threshold": opts.get("no_speech_threshold") or None,
        "initial_prompt": opts.get("initial_prompt") or None,
        "hotwords": opts.get("hotwords") or None,
        "repetition_penalty": float(opts.get("repetition_penalty", 1.0)),
        "no_repeat_ngram_size": int(opts.get("no_repeat_ngram_size", 0)),
        "multilingual": bool(opts.get("multilingual", False)),
    }
    if batched:
        kw["batch_size"] = int(dec.get("batch_size", 8))
    else:
        kw["condition_on_previous_text"] = bool(opts.get("condition_on_previous_text", False))
        kw["hallucination_silence_threshold"] = opts.get("hallucination_silence_threshold") or None
    return kw


@dataclass
class TrackState:
    name: str
    audio: str
    samples: Any = None
    chunks: list[dict[str, int]] = field(default_factory=list)
    segments: list[dict[str, Any]] = field(default_factory=list)
    lane_s: float = 0.0  # summed lane time spent on this track
    decode_s: float = 0.0
    vad_s: float = 0.0

    @property
    def duration(self) -> float:
        return 0.0 if self.samples is None else len(self.samples) / SR

    @property
    def speech_s(self) -> float:
        return sum(int(c["end"]) - int(c["start"]) for c in self.chunks) / SR


def run_lanes(pieces: list[Piece], lanes: int, gate: LaneGate, work: Callable[[int, Piece], float],
              cancelled: Callable[[], bool] = lambda: False) -> None:
    """Run ``work(lane, piece)`` on ``lanes`` threads, pieces taken from one queue in order."""
    queue = list(pieces)
    lock = threading.Lock()
    errors: list[BaseException] = []

    def lane_loop(lane: int) -> None:
        while not errors and not cancelled():
            with lock:
                if not queue:
                    return
            if lane >= gate.active:  # switched off (call running / paused): wait, but end with the queue
                if gate.closed:
                    return
                gate.wait_turn(lane, timeout=0.5)
                continue
            with lock:
                if not queue:
                    return
                piece = queue.pop(0)
            try:
                work(lane, piece)
            except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
                errors.append(exc)
                return

    threads = [threading.Thread(target=lane_loop, args=(i,), name=f"lane{i}", daemon=True) for i in range(lanes)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise errors[0]
