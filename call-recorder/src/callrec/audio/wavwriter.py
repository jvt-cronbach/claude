"""Crash-safe streaming 16-bit PCM WAV writer.

The RIFF header is rewritten with the current sizes on every checkpoint, and
the data is fsync'ed, so after a crash or power loss at most the last
checkpoint interval is lost. :func:`repair_wav` fixes headers of files whose
writer never got to the final checkpoint.
"""

from __future__ import annotations

import os
import struct
import time
from pathlib import Path
from typing import BinaryIO

import numpy as np

HEADER_SIZE = 44
MAX_DATA_BYTES = 0xFFFFFFFF - 36  # ~12 h of 48 kHz mono 16 bit


def _header(sample_rate: int, channels: int, data_bytes: int) -> bytes:
    block_align = channels * 2
    return b"".join(
        [
            b"RIFF",
            struct.pack("<I", 36 + data_bytes),
            b"WAVE",
            b"fmt ",
            struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, sample_rate * block_align, block_align, 16),
            b"data",
            struct.pack("<I", data_bytes),
        ]
    )


class StreamingWavWriter:
    def __init__(
        self,
        path: Path,
        sample_rate: int,
        channels: int = 1,
        checkpoint_interval_s: float = 2.0,
    ) -> None:
        self.path = path
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.checkpoint_interval_s = checkpoint_interval_s
        self._fh: BinaryIO | None = open(path, "w+b")  # noqa: SIM115 - long-lived handle
        self._fh.write(_header(self.sample_rate, self.channels, 0))
        self._data_bytes = 0
        self._last_checkpoint = time.monotonic()
        self.checkpoint()

    @property
    def frames_written(self) -> int:
        return self._data_bytes // (2 * self.channels)

    @property
    def seconds_written(self) -> float:
        return self.frames_written / self.sample_rate

    def write(self, samples: np.ndarray) -> None:
        """Append int16 samples (shape (n,) for mono or (n, channels))."""
        if self._fh is None:
            raise ValueError("Writer ist bereits geschlossen")
        arr = np.ascontiguousarray(samples, dtype="<i2")
        if arr.size == 0:
            return
        if arr.ndim == 2 and arr.shape[1] != self.channels:
            raise ValueError("Kanalanzahl passt nicht")
        if arr.ndim == 1 and self.channels != 1:
            raise ValueError("Mono-Daten für Mehrkanal-Datei")
        if self._data_bytes + arr.size * 2 > MAX_DATA_BYTES:
            raise OSError("WAV-Größenlimit (4 GB) erreicht")
        self._fh.seek(0, os.SEEK_END)
        self._fh.write(arr.tobytes())
        self._data_bytes += arr.size * 2
        if time.monotonic() - self._last_checkpoint >= self.checkpoint_interval_s:
            self.checkpoint()

    def write_silence(self, frames: int) -> None:
        remaining = int(frames)
        block = 48000 * 10  # bound memory for long gaps
        while remaining > 0:
            n = min(block, remaining)
            shape: tuple[int, ...] = (n,) if self.channels == 1 else (n, self.channels)
            self.write(np.zeros(shape, dtype="<i2"))
            remaining -= n

    def checkpoint(self) -> None:
        """Rewrite the header with current sizes and fsync."""
        if self._fh is None:
            return
        self._fh.flush()
        self._fh.seek(0)
        self._fh.write(_header(self.sample_rate, self.channels, self._data_bytes))
        self._fh.seek(0, os.SEEK_END)
        self._fh.flush()
        try:
            os.fsync(self._fh.fileno())
        except OSError:
            pass
        self._last_checkpoint = time.monotonic()

    def close(self) -> None:
        if self._fh is None:
            return
        self.checkpoint()
        self._fh.close()
        self._fh = None

    def __enter__(self) -> StreamingWavWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_wav_header(path: Path) -> tuple[int, int, int, int]:
    """Return (sample_rate, channels, bits, declared_data_bytes) of a canonical 44-byte header."""
    with open(path, "rb") as fh:
        head = fh.read(HEADER_SIZE)
    if len(head) < HEADER_SIZE or head[:4] != b"RIFF" or head[8:12] != b"WAVE" or head[36:40] != b"data":
        raise ValueError(f"{path} ist keine von callrec geschriebene WAV-Datei")
    _, _, channels, rate, _, _, bits = struct.unpack("<IHHIIHH", head[16:36])
    (data_bytes,) = struct.unpack("<I", head[40:44])
    return rate, channels, bits, data_bytes


def repair_wav(path: Path) -> bool:
    """Fix the size fields after a crash. Returns True if the header was changed."""
    rate, channels, bits, declared = read_wav_header(path)
    size = path.stat().st_size
    block_align = channels * bits // 8
    actual = size - HEADER_SIZE
    actual -= actual % block_align  # drop a partially written frame
    if actual == declared:
        return False
    with open(path, "r+b") as fh:
        fh.truncate(HEADER_SIZE + actual)
        fh.seek(0)
        fh.write(_header(rate, channels, actual))
        fh.flush()
        os.fsync(fh.fileno())
    return True
