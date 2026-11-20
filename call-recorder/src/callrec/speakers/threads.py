"""Thread count for the CPU-bound speaker separation (sherpa-onnx / onnxruntime).

``diarization.num_threads = 0`` (default) chooses it from the *physical* core count:
hyper-threads add little for onnxruntime's matrix kernels, and using every core would make
the PC sluggish while it processes in the background. One core is left free; above 8 threads
the pyannote/wespeaker models scale poorly, so it is capped there. While a recording runs the
capture threads must never starve, so only about half of the cores (minus one) are used.
"""

from __future__ import annotations

import os
import threading

MAX_THREADS = 8
_recording = threading.Event()


def set_recording_active(active: bool) -> None:
    """Called by the controller at the start/stop of a recording."""
    if active:
        _recording.set()
    else:
        _recording.clear()


def recording_active() -> bool:
    return _recording.is_set()


def physical_cores() -> int:
    try:
        import psutil

        n = psutil.cpu_count(logical=False)
    except Exception:  # noqa: BLE001 - psutil missing or failing: fall back to the logical count
        n = None
    if not n:
        logical = os.cpu_count() or 2
        n = max(1, logical // 2) if logical > 2 else logical
    return int(n)


def auto_threads(cores: int | None = None, recording: bool | None = None) -> int:
    cores = physical_cores() if cores is None else max(1, int(cores))
    rec = recording_active() if recording is None else recording
    if rec:
        return max(1, min(MAX_THREADS // 2, cores // 2 - 1))
    return max(1, min(MAX_THREADS, cores - 1))


def effective_threads(configured: int, cores: int | None = None, recording: bool | None = None) -> int:
    """``configured`` > 0 is used as set (explicit override); 0 = automatic."""
    if configured and configured > 0:
        return int(configured)
    return auto_threads(cores, recording)


def describe(configured: int) -> str:
    cores = physical_cores()
    eff = effective_threads(configured)
    if configured > 0:
        return f"fest eingestellt: {configured} Threads ({cores} physische Kerne)"
    return (f"automatisch: {eff} Threads ({cores} physische Kerne"
            + (", Aufnahme läuft" if recording_active() else "") + ")")
