"""Thread counts for the CPU-bound processing: speaker separation (sherpa-onnx / onnxruntime) and
Whisper (CTranslate2), plus the "PC is busy with a call" state both of them respect.

``diarization.num_threads = 0`` (default) chooses it from the *physical* core count:
hyper-threads add little for onnxruntime's matrix kernels, and using every core would make
the PC sluggish while it processes in the background. One core is left free; above 8 threads
the pyannote/wespeaker models scale poorly, so it is capped there. While a recording runs the
capture threads must never starve, so only about half of the cores (minus one) are used.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

MAX_THREADS = 8
_recording = threading.Event()
_call = threading.Event()


def set_recording_active(active: bool) -> None:
    """Called by the controller at the start/stop of a recording."""
    if active:
        _recording.set()
    else:
        _recording.clear()


def recording_active() -> bool:
    return _recording.is_set()


def set_call_active(active: bool) -> None:
    """Called by the call detection: the call app (Teams) holds the microphone."""
    if active:
        _call.set()
    else:
        _call.clear()


def call_active() -> bool:
    return _call.is_set()


def pc_busy() -> bool:
    """A recording or a call is running: processing leaves headroom for it."""
    return _recording.is_set() or _call.is_set()


def busy_reason() -> str:
    if _recording.is_set():
        return "Aufnahme läuft"
    return "Teams-Anruf läuft" if _call.is_set() else ""


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


# -- Whisper (CTranslate2) ---------------------------------------------------------------------
# CTranslate2 4.9.0 uses intra_threads = 0 -> OMP_NUM_THREADS or min(4, hardware threads) when the
# caller (faster-whisper ``cpu_threads=0``, the default) sets nothing: on a PC with 8 cores only 4
# were used. The encoder (most of the work) scales sub-linearly with threads, so two "lanes" with
# half the threads each (``num_workers=2``: two replicas sharing one copy of the weights) finish
# both tracks sooner than one lane with all threads (measured: 27 % less time on 4 cores).
MIN_THREADS_PER_LANE = 2


@dataclass(frozen=True)
class AsrThreads:
    lanes: int  # parallel transcriptions (CTranslate2 num_workers)
    threads: int  # threads per lane (CTranslate2 intra_threads / faster-whisper cpu_threads)
    cores: int
    busy: bool  # a recording/call was running when the plan was made
    source: str  # "auto" | "fest" | "Messung"
    busy_lanes: int = 1  # lanes that keep working while a recording/call runs

    @property
    def total(self) -> int:
        return self.lanes * self.threads

    def to_dict(self) -> dict[str, object]:
        return {"lanes": self.lanes, "threads_per_lane": self.threads, "total_threads": self.total,
                "physical_cores": self.cores, "busy_at_start": self.busy, "source": self.source,
                "busy_lanes": self.busy_lanes}

    def describe(self) -> str:
        lanes = f"{self.lanes} parallele Spuren à {self.threads} Threads" if self.lanes > 1 else f"{self.threads} Threads"
        src = {"fest": "fest eingestellt", "Messung": "nach Messung"}.get(self.source, "automatisch")
        return f"{lanes} ({src}, {self.cores} physische Kerne)"


def asr_threads(configured_threads: int = 0, configured_lanes: int = 0, cores: int | None = None,
                busy: bool | None = None, reserve: int = 0, tuned: dict[str, object] | None = None) -> AsrThreads:
    """Whisper threads for one transcription session.

    ``reserve``: threads another stage needs at the same time (parallel speaker separation).
    ``tuned``: the result of ``callrec benchmark`` on this PC ({"lanes", "threads"} for the full PC).
    The threads are fixed when the model is loaded; while a recording/call runs only one lane
    keeps working (the worker is told so), i.e. about half the cores."""
    cores = physical_cores() if cores is None else max(1, int(cores))
    busy = pc_busy() if busy is None else busy
    budget = max(1, cores - max(0, reserve))
    source = "auto"
    if configured_threads > 0:
        budget, source = configured_threads, "fest"
    lanes = configured_lanes if configured_lanes > 0 else 0
    if lanes and source == "auto":
        source = "fest"
    if not lanes and tuned and source == "auto" and int(tuned.get("cores", 0) or 0) == cores and not reserve:
        lanes = int(tuned.get("lanes", 0) or 0)
        t = int(tuned.get("threads", 0) or 0)
        if lanes > 0 and t > 0:
            return AsrThreads(lanes, t, cores, busy, "Messung")
    if not lanes:
        lanes = 2 if budget >= 2 * MIN_THREADS_PER_LANE else 1
    lanes = max(1, min(lanes, budget))
    threads = max(1, budget // lanes)
    if lanes == 1 and busy and source == "auto":
        threads = max(1, budget // 2)  # no second lane to switch off: start with half the cores
    return AsrThreads(lanes, threads, cores, busy, source)
