"""Backend-independent dual-track recorder.

The audio callback only enqueues raw bytes with a monotonic timestamp; a writer
thread per track down-mixes to mono, fills gaps with silence (WASAPI loopback
delivers *no* packets while nothing is played) and streams to a crash-safe WAV.
Both tracks share the same time origin, so they stay aligned for merging.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from callrec.audio.wavwriter import StreamingWavWriter
from callrec.consent import ConsentRecord, require_consent

log = logging.getLogger(__name__)

MIC_PART = "mic.part.wav"
REMOTE_PART = "remote.part.wav"


@dataclass(frozen=True)
class DeviceSpec:
    id: str  # backend specific identifier (PortAudio index as text)
    name: str
    sample_rate: int
    channels: int
    kind: str  # "mic" | "loopback"
    endpoint_id: str = ""  # Windows endpoint id, if known

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CaptureStream(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def is_active(self) -> bool: ...


DataCallback = Callable[[bytes, float], None]


class CaptureBackend(Protocol):
    def resolve_devices(self, mic_pref: str, loopback_pref: str) -> tuple[DeviceSpec, DeviceSpec]: ...

    def open_stream(self, device: DeviceSpec, on_data: DataCallback, frames_per_buffer: int) -> CaptureStream: ...

    def close(self) -> None: ...


def downmix_int16(data: bytes, channels: int) -> np.ndarray:
    frames = np.frombuffer(data, dtype="<i2")
    if channels <= 1:
        return frames.copy()
    usable = frames.size - frames.size % channels
    multi = frames[:usable].reshape(-1, channels).astype(np.int32)
    return (multi.sum(axis=1) // channels).astype(np.int16)


def peak_level(mono: np.ndarray) -> float:
    """Peak of an int16 block as 0..1 (cheap: one pass over a block that is written anyway)."""
    if mono.size == 0:
        return 0.0
    return min(1.0, float(np.max(np.abs(mono.astype(np.int32)))) / 32768.0)


@dataclass
class TrackStats:
    frames: int = 0
    chunks: int = 0
    inserted_silence_s: float = 0.0
    gaps: int = 0
    errors: list[str] = field(default_factory=list)


class TrackWriter:
    """Consumes (bytes, timestamp) chunks for one device and writes a mono WAV."""

    def __init__(
        self,
        path: Path,
        device: DeviceSpec,
        t0: float,
        gap_threshold_s: float = 0.5,
        checkpoint_interval_s: float = 2.0,
    ) -> None:
        self.path = path
        self.device = device
        self.t0 = t0
        self.gap_threshold_s = gap_threshold_s
        self.writer = StreamingWavWriter(path, device.sample_rate, 1, checkpoint_interval_s)
        self.stats = TrackStats()
        self.last_data_t: float | None = None  # timestamp of the last chunk delivered by the device
        self.level = 0.0  # peak of the last written chunk, 0..1 (for the level meter; read from the UI)
        self._queue: queue.Queue[tuple[bytes, float] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=f"writer-{device.kind}", daemon=True)
        self._lock = threading.Lock()

    # called from the audio thread: must be cheap
    def on_data(self, data: bytes, t: float) -> None:
        self.last_data_t = t
        self._queue.put((data, t))

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            try:
                self.process_chunk(*item)
            except Exception as exc:  # keep recording the other chunks
                log.exception("Fehler beim Schreiben (%s)", self.device.kind)
                self.stats.errors.append(str(exc))

    def process_chunk(self, data: bytes, t: float) -> None:
        mono = downmix_int16(data, self.device.channels)
        n = int(mono.size)
        if n == 0:
            return
        self.level = peak_level(mono)
        rate = self.device.sample_rate
        with self._lock:
            expected_start = (t - self.t0) - n / rate
            lag = expected_start - self.writer.seconds_written
            if lag > self.gap_threshold_s:
                silence = int(round(lag * rate))
                self.writer.write_silence(silence)
                self.stats.inserted_silence_s += silence / rate
                self.stats.gaps += 1
            self.writer.write(mono)
            self.stats.frames = self.writer.frames_written
            self.stats.chunks += 1

    def finish(self, total_seconds: float) -> TrackStats:
        """Drain the queue, pad to ``total_seconds`` and close the file."""
        self._queue.put(None)
        if self._thread.is_alive():
            self._thread.join(timeout=30)
        with self._lock:
            missing = int(round(total_seconds * self.device.sample_rate)) - self.writer.frames_written
            if missing > 0:
                self.writer.write_silence(missing)
                self.stats.inserted_silence_s += missing / self.device.sample_rate
            self.stats.frames = self.writer.frames_written
            self.writer.close()
        return self.stats


@dataclass
class RecordingResult:
    duration_s: float
    mic: TrackStats
    remote: TrackStats
    warnings: list[str]


class DualTrackRecorder:
    def __init__(
        self,
        backend: CaptureBackend,
        folder: Path,
        mic: DeviceSpec,
        loopback: DeviceSpec,
        gap_threshold_s: float = 0.5,
        frames_per_buffer: int = 2048,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.backend = backend
        self.folder = folder
        self.mic = mic
        self.loopback = loopback
        self.gap_threshold_s = gap_threshold_s
        self.frames_per_buffer = frames_per_buffer
        self.clock = clock
        self.t0: float | None = None
        self._streams: list[CaptureStream] = []
        self._writers: dict[str, TrackWriter] = {}
        self.consent: ConsentRecord | None = None
        self._warnings: list[str] = []

    @property
    def is_recording(self) -> bool:
        return self.t0 is not None

    @property
    def elapsed(self) -> float:
        return 0.0 if self.t0 is None else self.clock() - self.t0

    def start(self, consent: ConsentRecord | None) -> None:
        # Hard gate: never open an audio stream without a valid confirmation.
        self.consent = require_consent(consent)
        if self.t0 is not None:
            raise RuntimeError("Aufnahme läuft bereits")
        audio_dir = self.folder / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        t0 = self.clock()
        mic_w = TrackWriter(audio_dir / MIC_PART, self.mic, t0, self.gap_threshold_s)
        rem_w = TrackWriter(audio_dir / REMOTE_PART, self.loopback, t0, self.gap_threshold_s)
        mic_w.start()
        rem_w.start()
        self._writers = {"mic": mic_w, "remote": rem_w}
        self.t0 = t0
        try:
            for dev, w in ((self.mic, mic_w), (self.loopback, rem_w)):
                stream = self.backend.open_stream(dev, w.on_data, self.frames_per_buffer)
                self._streams.append(stream)
            for s in self._streams:
                s.start()
        except Exception:
            log.exception("Audiogeräte konnten nicht geöffnet werden")
            self.stop()
            raise
        log.info("Aufnahme gestartet: Mikrofon=%s, Loopback=%s", self.mic.name, self.loopback.name)

    LEVEL_HOLD_S = 0.5

    def levels(self) -> dict[str, float]:
        """Current peak level per track (0..1); 0 when a track delivered nothing recently
        (the loopback delivers no packets at all while nothing is played)."""
        out = {"mic": 0.0, "remote": 0.0}
        if self.t0 is None:
            return out
        now = self.clock()
        for key, w in self._writers.items():
            if w.last_data_t is not None and now - w.last_data_t <= self.LEVEL_HOLD_S:
                out[key] = w.level
        return out

    # A microphone delivers packets continuously (also during silence); the loopback
    # legitimately delivers nothing while nothing is played, so only the mic is watched.
    MIC_STALL_S = 5.0

    def health_warnings(self) -> list[str]:
        """Called periodically while recording; reports dead streams, stalled mic, write errors."""
        out = []
        for name, s in zip(("Mikrofon", "Lautsprecher-Loopback"), self._streams, strict=False):
            try:
                active = s.is_active()
            except Exception:
                active = False
            if not active:
                out.append(f"{name}-Stream ist nicht mehr aktiv (Gerät getrennt?)")
        mic_w = self._writers.get("mic")
        if mic_w is not None and self.t0 is not None:
            now = self.clock()
            last = mic_w.last_data_t if mic_w.last_data_t is not None else self.t0
            if now - last > self.MIC_STALL_S:
                out.append(
                    f"Mikrofon liefert seit {int(now - last)} s keine Daten (Gerät getrennt oder Bluetooth-Profil gewechselt?)"
                )
        for key, w in self._writers.items():
            if w.stats.errors:
                out.append(f"Schreibfehler in der Spur {key}: {w.stats.errors[-1]} (Datenträger voll?)")
        for msg in out:
            base = msg.split(" seit ")[0]
            if not any(m.split(" seit ")[0] == base for m in self._warnings):
                self._warnings.append(msg)
        return out

    def stop(self) -> RecordingResult:
        if self.t0 is None:
            raise RuntimeError("Keine Aufnahme aktiv")
        for s in self._streams:
            try:
                s.stop()
            except Exception:
                log.exception("Stream konnte nicht gestoppt werden")
        duration = self.clock() - self.t0
        stats = {k: w.finish(duration) for k, w in self._writers.items()}
        self._streams = []
        self.t0 = None
        log.info("Aufnahme beendet nach %.1f s", duration)
        return RecordingResult(
            duration_s=duration,
            mic=stats.get("mic", TrackStats()),
            remote=stats.get("remote", TrackStats()),
            warnings=list(self._warnings),
        )
