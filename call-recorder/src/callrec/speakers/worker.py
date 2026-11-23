"""Speaker separation in a child process: ``python -m callrec.speakers.worker --job JOB --out RESULT``.

Why a process of its own (0.4): sherpa-onnx's ``OfflineSpeakerDiarization.process`` keeps the
Python GIL for the whole run (measured: other threads of the app only got a turn at the progress
callbacks, up to 0.16 s apart, for minutes) - the window, the tray and a recording that starts
meanwhile all stalled. In a child process it runs with below-normal priority, does not block the
app and can run at the same time as the Whisper worker.

The child loads the remote track, separates the speakers, computes one voice embedding per
speaker (as before, :func:`callrec.speakers.diarize.speaker_embeddings`) and the loudness
envelope used by the track check, and writes them as JSON (+ ``.npy`` for the envelope).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np


def run_job(job: dict[str, Any], emit: Callable[[str], None] = lambda line: print(line, flush=True),
            diarizer: Any = None, embedder: Any = None) -> dict[str, Any]:
    from callrec.audio.finalize import load_mono
    from callrec.speakers import trackcheck
    from callrec.speakers.diarize import SherpaDiarizer, SherpaEmbedder, speaker_embeddings

    t0 = time.monotonic()
    threads = int(job.get("threads", 2))
    samples, sr = load_mono(Path(job["audio"]), 16000)
    timing: dict[str, Any] = {"audio_s": round(samples.size / sr, 1) if sr else 0.0, "num_threads": threads,
                              "load_audio_s": round(time.monotonic() - t0, 2)}
    env_path = Path(str(job["out"])).with_suffix(".envelope.npy")
    np.save(env_path, trackcheck.envelope(samples, sr))
    out: dict[str, Any] = {"turns": [], "embeddings": {}, "model_id": "", "silent": False,
                           "envelope": str(env_path), "timing": timing}
    if not samples.size or float(np.max(np.abs(samples))) <= 1e-4:
        out["silent"] = True
        return out
    t1 = time.monotonic()
    d = diarizer or SherpaDiarizer(Path(job["segmentation"]), Path(job["embedding"]),
                                   cluster_threshold=float(job.get("cluster_threshold", 0.4)),
                                   num_speakers=int(job.get("num_speakers", 0)),
                                   min_duration_on=float(job.get("min_duration_on", 0.3)),
                                   min_duration_off=float(job.get("min_duration_off", 0.5)), num_threads=threads)
    t2 = time.monotonic()
    turns = d.diarize(samples, sr, lambda f: emit(f"PROGRESS {0.9 * f:.4f}"))
    t3 = time.monotonic()
    timing.update(load_s=round(t2 - t1, 2), diarize_s=round(t3 - t2, 2))
    if turns:
        e = embedder or SherpaEmbedder(Path(job["embedding"]), str(job.get("embedding_id", "")), threads)
        emit("PROGRESS 0.92")
        embs = speaker_embeddings(turns, samples, sr, e)
        out["embeddings"] = {k: [float(x) for x in np.asarray(v).reshape(-1)] for k, v in embs.items()}
        out["model_id"] = str(getattr(e, "model_id", job.get("embedding_id", "")))
    timing["embed_s"] = round(time.monotonic() - t3, 2)
    out["turns"] = [{"start": t.start, "end": t.end, "speaker": t.speaker} for t in turns]
    emit("PROGRESS 1.0")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="callrec.speakers.worker")
    p.add_argument("--job", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    from callrec.asr.worker import lower_priority

    lower_priority()
    job = json.loads(Path(args.job).read_text(encoding="utf-8"))
    job["out"] = args.out
    result = run_job(job)
    out = Path(args.out)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)
    return 0


def run_in_child(job: dict[str, Any], work_dir: Path, progress: Callable[[float], None] = lambda f: None,
                 command_prefix: list[str] | None = None) -> dict[str, Any]:
    """Parent side: run the job in ``python -m callrec.speakers.worker`` and return its result."""
    from callrec.asr.transcriber import (
        _CANCEL,
        TranscriptionCancelled,
        TranscriptionError,
        console_python,
        run_child,
    )

    work_dir.mkdir(parents=True, exist_ok=True)
    job_path, out_path = work_dir / "diarize_job.json", work_dir / "diarize_result.json"
    if out_path.exists():
        out_path.unlink()
    job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    cmd = (command_prefix or [console_python(), "-m", "callrec.speakers.worker"]) + [
        "--job", str(job_path), "--out", str(out_path)]

    def feed(line: str) -> None:
        parts = line.split()
        if len(parts) == 2 and parts[0] == "PROGRESS":
            try:
                progress(float(parts[1]))
            except ValueError:
                pass

    rc = run_child(cmd, work_dir / "diarize.log", feed)
    if _CANCEL.is_set():
        raise TranscriptionCancelled("Verarbeitung abgebrochen.")
    if rc != 0 or not out_path.exists():
        raise TranscriptionError(f"Sprechertrennung fehlgeschlagen (Exit-Code {rc}), Details: {work_dir / 'diarize.log'}")
    return dict(json.loads(out_path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    sys.exit(main())
