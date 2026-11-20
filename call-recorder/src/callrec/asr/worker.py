"""Child-process entry point: ``python -m callrec.asr.worker ...``.

Running Whisper in a separate process isolates hard crashes (e.g. a missing
cuDNN DLL aborts the whole process instead of raising) so the parent can fall
back to the CPU. Protocol on stdout (one message per line):

- ``STAGE download|load|transcribe [seconds]`` - phase changes (``load`` = "Modell wird geladen")
- ``DOWNLOAD <written> <total> <received>`` - model download progress in bytes
- ``PROGRESS <0..1>`` - transcription progress
- ``TIMING <download_s> <load_s> <transcribe_s>`` - for the real-time factor

``--download-only`` just downloads the model (``callrec models download --whisper``); it also
emits ``DOWNLOADDISK <elapsed_s> <bytes>`` (bytes that appeared on disk so far, see
:mod:`callrec.asr.download`) and finally ``DOWNLOADED <bytes>`` (size of the verified model) or fails.

``--detect-language`` measures language probabilities for one or more tracks
(``--detect-audio mic=PATH``) and writes them to ``--out`` (see :mod:`callrec.asr.language`).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any


def resolve_compute_type(device: str, compute_type: str) -> str:
    if compute_type and compute_type != "auto":
        return compute_type
    return "float16" if device == "cuda" else "int8"


def emit(line: str) -> None:
    print(line, flush=True)


def download(model: str, download_root: str | None, downloader: Any = None) -> float:
    """Download with progress lines; returns the seconds spent."""
    from callrec.asr.download import download_whisper_model

    started = time.monotonic()
    emit("STAGE download")
    print(f"Modell {model} nicht im lokalen Cache - wird heruntergeladen", file=sys.stderr, flush=True)
    kwargs: dict[str, Any] = {"snapshot_download": downloader} if downloader is not None else {}
    download_whisper_model(model, download_root or "", lambda w, t, r: emit(f"DOWNLOAD {w} {t} {r}"),
                           disk_report=lambda e, b: emit(f"DOWNLOADDISK {e:.0f} {b}"), **kwargs)
    return time.monotonic() - started


def load_model(factory: Any, model: str, device: str, compute_type: str, download_root: str | None,
               downloader: Any = None, timing: dict[str, float] | None = None) -> Any:
    """Load from the local cache first.

    Without ``local_files_only`` faster-whisper/huggingface_hub contact huggingface.co on
    *every* load to resolve the revision, i.e. once per transcribed track. The network is
    only used when the model is not downloaded yet - then with visible progress.
    """
    timing = timing if timing is not None else {}
    emit("STAGE load")
    started = time.monotonic()
    try:
        m = factory(model, device=device, compute_type=compute_type, download_root=download_root, local_files_only=True)
        timing["load_s"] = time.monotonic() - started
        return m
    except FileNotFoundError:  # huggingface_hub LocalEntryNotFoundError: not downloaded yet
        pass
    timing["download_s"] = download(model, download_root, downloader)
    emit("STAGE load")
    started = time.monotonic()
    m = factory(model, device=device, compute_type=compute_type, download_root=download_root, local_files_only=True)
    timing["load_s"] = time.monotonic() - started
    return m


def verify_model(model: str, download_root: str | None) -> int:
    """Raise ModelIncomplete unless faster-whisper can find every model file offline; returns the size."""
    from callrec.asr.download import verify_snapshot

    return verify_snapshot(model, download_root or "")


def disable_onnxruntime_telemetry() -> None:
    """Windows builds of onnxruntime (Silero VAD) emit ETW telemetry events unless disabled."""
    try:
        import onnxruntime

        onnxruntime.disable_telemetry_events()
    except Exception:  # noqa: BLE001 - optional
        pass


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.device == "cuda":
        from callrec.asr.cuda import setup_cuda_dll_paths

        setup_cuda_dll_paths()
    from faster_whisper import WhisperModel

    disable_onnxruntime_telemetry()
    compute_type = resolve_compute_type(args.device, args.compute_type)
    timing: dict[str, float] = {"download_s": 0.0, "load_s": 0.0}
    model = load_model(WhisperModel, args.model, args.device, compute_type, args.download_root or None, timing=timing)
    emit(f"STAGE transcribe {timing['load_s']:.2f}")
    t0 = time.monotonic()
    language = None if args.language in ("", "auto") else args.language
    segments, info = model.transcribe(args.audio, language=language, **decoding_options(args))
    out_segments = []
    duration = float(info.duration or 0.0)
    for seg in segments:
        words = [
            {"start": float(w.start), "end": float(w.end), "text": w.word, "probability": float(w.probability)}
            for w in (seg.words or [])
        ]
        out_segments.append({"start": float(seg.start), "end": float(seg.end), "text": seg.text.strip(), "words": words,
                             "avg_logprob": round(float(seg.avg_logprob), 4),
                             "compression_ratio": round(float(seg.compression_ratio), 3),
                             "temperature": float(seg.temperature) if seg.temperature is not None else None})
        if duration > 0:
            print(f"PROGRESS {min(1.0, seg.end / duration):.4f}", flush=True)
    print("PROGRESS 1.0", flush=True)
    timing["transcribe_s"] = time.monotonic() - t0
    emit(f"TIMING {timing['download_s']:.2f} {timing['load_s']:.2f} {timing['transcribe_s']:.2f}")
    return {
        "language": info.language,
        "language_probability": round(float(info.language_probability or 0.0), 4),
        "multilingual": bool(args.multilingual),
        "duration": duration,
        "segments": out_segments,
        "model": args.model,
        "device": args.device,
        "compute_type": compute_type,
        "timing": timing,
    }


FALLBACK_TEMPERATURES = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)  # faster-whisper 1.2.1 default


def vad_parameters(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "threshold": args.vad_threshold,
        "min_silence_duration_ms": args.vad_min_silence_ms,
        "speech_pad_ms": args.vad_speech_pad_ms,
    }


def decoding_options(args: argparse.Namespace) -> dict[str, Any]:
    """Keyword arguments for WhisperModel.transcribe (names as in faster-whisper 1.2.1)."""
    return {
        "beam_size": args.beam_size,
        "vad_filter": args.vad,
        "vad_parameters": vad_parameters(args),
        "word_timestamps": True,
        "condition_on_previous_text": args.condition_on_previous_text,
        "initial_prompt": args.initial_prompt or None,
        "hotwords": args.hotwords or None,
        "hallucination_silence_threshold": args.hallucination_silence_threshold or None,
        "compression_ratio_threshold": args.compression_ratio_threshold or None,
        "log_prob_threshold": args.log_prob_threshold or None,
        "no_speech_threshold": args.no_speech_threshold or None,
        "temperature": list(FALLBACK_TEMPERATURES) if args.temperature_fallback else 0.0,
        "repetition_penalty": args.repetition_penalty,
        "no_repeat_ngram_size": args.no_repeat_ngram_size,
        "multilingual": args.multilingual,
    }


def _original_time(offset_samples: int, chunks: list[dict[str, Any]]) -> float:
    """Map a position in the concatenated speech back to the recording time (seconds at 16 kHz)."""
    pos = 0
    for c in chunks:
        length = int(c["end"]) - int(c["start"])
        if offset_samples < pos + length:
            return (int(c["start"]) + offset_samples - pos) / 16000.0
        pos += length
    return int(chunks[-1]["end"]) / 16000.0 if chunks else 0.0


def detect_track_language(model: Any, audio_path: str, windows: int, vad: dict[str, Any],
                          audio: Any = None) -> dict[str, Any]:
    """Language probabilities of up to ``windows`` 30-s speech windows spread over the track.

    Same building blocks as WhisperModel.detect_language in faster-whisper 1.2.1 (feature
    extractor, pad_or_trim, encode, ctranslate2 ``detect_language`` -> probabilities of every
    language), but on windows spread over the whole recording instead of only its beginning.
    """
    import numpy as np
    from faster_whisper.audio import decode_audio, pad_or_trim
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    fe = model.feature_extractor
    sr = int(fe.sampling_rate)
    samples = audio if audio is not None else decode_audio(audio_path, sampling_rate=sr)
    duration = len(samples) / sr
    chunks = get_speech_timestamps(samples, VadOptions(**vad))
    if not chunks:
        return {"duration": round(duration, 2), "speech_s": 0.0, "windows": []}
    speech = np.concatenate([samples[int(c["start"]):int(c["end"])] for c in chunks])
    n = int(fe.n_samples)
    count = max(1, min(windows, -(-len(speech) // n)))
    if len(speech) <= n:
        starts = [0]
    else:
        starts = sorted({int(round(x)) for x in np.linspace(0, len(speech) - n, count)})
    out = []
    for start in starts:
        seg = speech[start:start + n]
        features = fe(seg)
        enc = model.encode(pad_or_trim(features[..., : fe.nb_max_frames]))
        results = model.model.detect_language(enc)[0]
        probs = {tok[2:-2]: float(p) for tok, p in results}
        top = results[0][0][2:-2] if results else ""
        out.append({"start": round(_original_time(start, chunks), 1), "speech_s": round(len(seg) / sr, 1),
                    "top": top, "top_prob": round(probs.get(top, 0.0), 4),
                    "probs": {k: round(v, 5) for k, v in probs.items() if v >= 0.001}})
    return {"duration": round(duration, 2), "speech_s": round(len(speech) / sr, 1), "windows": out}


def run_detect(args: argparse.Namespace) -> dict[str, Any]:
    if args.device == "cuda":
        from callrec.asr.cuda import setup_cuda_dll_paths

        setup_cuda_dll_paths()
    from faster_whisper import WhisperModel

    disable_onnxruntime_telemetry()
    compute_type = resolve_compute_type(args.device, args.compute_type)
    timing: dict[str, float] = {"download_s": 0.0, "load_s": 0.0}
    model = load_model(WhisperModel, args.model, args.device, compute_type, args.download_root or None, timing=timing)
    emit("STAGE detect")
    t0 = time.monotonic()
    tracks: dict[str, Any] = {}
    multilingual = bool(getattr(model.model, "is_multilingual", True))
    items = [a.split("=", 1) for a in args.detect_audio]
    for i, (track, path) in enumerate(items):
        if multilingual:
            tracks[track] = detect_track_language(model, path, args.detect_windows, vad_parameters(args))
        print(f"PROGRESS {(i + 1) / len(items):.4f}", flush=True)
    return {"model": args.model, "device": args.device, "multilingual_model": multilingual,
            "tracks": tracks, "detect_s": round(time.monotonic() - t0, 2), "timing": timing}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="callrec.asr.worker")
    p.add_argument("--audio", default="")
    p.add_argument("--out", default="")
    p.add_argument("--model", default="large-v3")
    p.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    p.add_argument("--compute-type", default="auto")
    p.add_argument("--language", default="de")
    p.add_argument("--beam-size", type=int, default=5)
    p.add_argument("--vad", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--condition-on-previous-text", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--initial-prompt", default="")
    p.add_argument("--hotwords", default="")
    p.add_argument("--vad-threshold", type=float, default=0.5)
    p.add_argument("--vad-min-silence-ms", type=int, default=2000)
    p.add_argument("--vad-speech-pad-ms", type=int, default=400)
    p.add_argument("--hallucination-silence-threshold", type=float, default=0.0)
    p.add_argument("--compression-ratio-threshold", type=float, default=2.4)
    p.add_argument("--log-prob-threshold", type=float, default=-1.0)
    p.add_argument("--no-speech-threshold", type=float, default=0.6)
    p.add_argument("--temperature-fallback", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--repetition-penalty", type=float, default=1.0)
    p.add_argument("--no-repeat-ngram-size", type=int, default=0)
    p.add_argument("--multilingual", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--detect-language", action="store_true", help="nur Sprache messen")
    p.add_argument("--detect-audio", action="append", default=[], help="SPUR=PFAD (mehrfach)")
    p.add_argument("--detect-windows", type=int, default=6)
    p.add_argument("--download-root", default="")
    p.add_argument("--download-only", action="store_true", help="nur das Modell herunterladen")
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    if args.download_only:
        from callrec.asr.download import ModelIncomplete

        download(args.model, args.download_root or None)
        try:
            size = verify_model(args.model, args.download_root or None)
        except ModelIncomplete as exc:
            print(f"FEHLER: {exc}", file=sys.stderr, flush=True)
            emit(f"FAILED {exc}")
            return 3
        emit(f"DOWNLOADED {size}")
        emit("STAGE done")
        return 0
    if args.detect_language:
        if not args.detect_audio or not args.out:
            p.error("--detect-audio und --out sind erforderlich")
        result = run_detect(args)
    else:
        if not args.audio or not args.out:
            p.error("--audio und --out sind erforderlich")
        result = run(args)
    out = Path(args.out)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
