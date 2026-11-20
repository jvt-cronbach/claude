"""Speaker sample clips for "Anhören" / "Weiteres Beispiel" (shared by the Tk and the web dialog).

The passages come from :mod:`callrec.speakers.samples`; this writes the chosen one as a WAV into a
private temporary folder, cycles through the passages per speaker and removes the files again
(older clips while the dialog is open, everything when it closes)."""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KEEP_FILES = 4  # a clip may still be playing while the next one is written


@dataclass
class Clip:
    path: Path
    index: int  # 0-based passage
    count: int
    duration_s: float

    def caption(self, speech_s: float | None = None) -> str:
        from callrec.transcript import fmt_ts

        out = f"Beispiel {self.index + 1} von {self.count} · {self.duration_s:.0f} s"
        if speech_s is not None:
            out += f" · insgesamt {fmt_ts(speech_s, 'mm:ss')} Sprache"
        return out


STALE_S = 12 * 3600


def remove_stale_clip_folders(base: Path | None = None, now: float | None = None) -> int:
    """Clip folders left behind by a crash or a killed process (passages of conversations - also of
    recordings whose audio was deleted since): removed when the next dialog needs clips."""
    import time

    base = Path(base or tempfile.gettempdir())
    now = time.time() if now is None else now
    n = 0
    try:
        candidates = list(base.glob("callrec_clips_*"))
    except OSError:
        return 0
    for d in candidates:
        try:
            if d.is_dir() and not d.is_symlink() and now - d.stat().st_mtime > STALE_S:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except OSError:
            continue
    return n


class ClipStore:
    def __init__(self, on_remove: Callable[[Path], None] = lambda p: None) -> None:
        self._dir: Path | None = None
        self._files: list[Path] = []
        self._next: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()
        self.on_remove = on_remove

    @property
    def folder(self) -> Path:
        if self._dir is None:
            remove_stale_clip_folders()
            self._dir = Path(tempfile.mkdtemp(prefix="callrec_clips_"))
        return self._dir

    def make(self, folder: Path, track: str, samples: list[Any], key: tuple[str, str], step: int = 0) -> Clip:
        """The clip of ``key`` (recording, speaker): ``step`` 0 = the current passage again, 1 = the next."""
        from callrec.audio.finalize import find_audio, write_clip_pieces
        from callrec.speakers.samples import JOIN_GAP_S

        if not samples:
            raise ValueError("Für diese Stimme gibt es keine Hörprobe.")
        audio = find_audio(folder)
        if audio is None:
            raise ValueError("Für diese Aufnahme gibt es (noch) keine fertigen Audiodateien.")
        with self._lock:
            idx = (self._next.get(key, 0) + step) % len(samples) if key in self._next else 0
            self._next[key] = idx
        smp = samples[idx]
        dst = self.folder / f"{uuid.uuid4().hex}.wav"
        write_clip_pieces(getattr(audio, track), smp.pieces, dst, gap_s=JOIN_GAP_S)
        with self._lock:
            self._files.append(dst)
            old, self._files = self._files[:-KEEP_FILES], self._files[-KEEP_FILES:]
        for p in old:
            self._remove(p)
        return Clip(dst, idx, len(samples), smp.duration)

    def _remove(self, p: Path) -> None:
        try:
            self.on_remove(p)
        except Exception:
            log.debug("Hörprobe nicht abgemeldet", exc_info=True)
        try:
            p.unlink(missing_ok=True)
        except OSError:
            log.debug("Hörprobe %s nicht gelöscht (wird noch abgespielt?)", p, exc_info=True)

    def clear(self) -> None:
        """All clips of the dialog that just closed."""
        with self._lock:
            files, self._files = self._files, []
            self._next.clear()
        for p in files:
            self._remove(p)

    def close(self) -> None:
        self.clear()
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
