"""Child-process entry point: ``python -m callrec.asr.worker ...``.

Running Whisper in a separate process isolates hard crashes (e.g. a missing
cuDNN DLL aborts the whole process instead of raising) so the parent can fall
back to the CPU. Progress is reported as ``PROGRESS <0..1>`` lines on stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def resolve_compute_type(device: str, compute_type: str) -> str:
    if compute_type and compute_type != "auto":
        return compute_type
    return "float16" if device == "cuda" else "int8"


def load_model(factory: Any, model: str, device: str, compute_type: str, download_root: str | None) -> Any:
    """Load from the local cache first.

    Without ``local_files_only`` faster-whisper/huggingface_hub contact huggingface.co on
    *every* load to resolve the revision, i.e. once per transcribed track. The network is
    only used when the model is not downloaded yet.
    """
    try:
        return factory(model, device=device, compute_type=compute_type, download_root=download_root, local_files_only=True)
    except FileNotFoundError:  # huggingface_hub LocalEntryNotFoundError: not downloaded yet
        print(f"Modell {model} nicht im lokalen Cache - wird heruntergeladen", file=sys.stderr, flush=True)
    return factory(model, device=device, compute_type=compute_type, download_root=download_root)


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
    model = load_model(WhisperModel, args.model, args.device, compute_type, args.download_root or None)
    language = None if args.language in ("", "auto") else args.language
    segments, info = model.transcribe(
        args.audio,
        language=language,
        beam_size=args.beam_size,
        vad_filter=args.vad,
        vad_parameters={
            "threshold": args.vad_threshold,
            "min_silence_duration_ms": args.vad_min_silence_ms,
            "speech_pad_ms": args.vad_speech_pad_ms,
        },
        word_timestamps=True,
        condition_on_previous_text=args.condition_on_previous_text,
        initial_prompt=args.initial_prompt or None,
        hotwords=args.hotwords or None,
        hallucination_silence_threshold=args.hallucination_silence_threshold or None,
    )
    out_segments = []
    duration = float(info.duration or 0.0)
    for seg in segments:
        words = [
            {"start": float(w.start), "end": float(w.end), "text": w.word, "probability": float(w.probability)}
            for w in (seg.words or [])
        ]
        out_segments.append({"start": float(seg.start), "end": float(seg.end), "text": seg.text.strip(), "words": words})
        if duration > 0:
            print(f"PROGRESS {min(1.0, seg.end / duration):.4f}", flush=True)
    print("PROGRESS 1.0", flush=True)
    return {
        "language": info.language,
        "duration": duration,
        "segments": out_segments,
        "model": args.model,
        "device": args.device,
        "compute_type": compute_type,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="callrec.asr.worker")
    p.add_argument("--audio", required=True)
    p.add_argument("--out", required=True)
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
    p.add_argument("--download-root", default="")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run(args)
    out = Path(args.out)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
