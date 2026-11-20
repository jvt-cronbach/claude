"""Status output on the console without flooding it.

- Terminal (TTY): one line is updated in place (``\\r`` + text cut to the window width, so a long
  status never wraps - a wrapped line cannot be overwritten and every update would look like a new
  line). A new line starts only when the stage changes or something else is printed.
- Not a terminal (redirected to a file, run by a scheduler): a line is written when the stage or
  the percentage changes, but at most every ``min_interval_s`` (30 s) - except a stage change,
  which is always written at once. Heartbeats that only change the elapsed seconds are dropped
  until the interval has passed.
"""

from __future__ import annotations

import re
import shutil
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, TextIO

_STAGE_RE = re.compile(r"^([^:0-9(]+)")
_PERCENT_RE = re.compile(r"(\d+) %")


def stage_of(text: str) -> str:
    m = _STAGE_RE.match(text)
    return m.group(1).strip() if m else text.strip()


def percent_of(text: str) -> str:
    m = _PERCENT_RE.search(text)
    return m.group(1) if m else ""


class ConsoleStatus:
    def __init__(self, stream: TextIO | None = None, is_tty: bool | None = None, min_interval_s: float = 30.0,
                 clock: Callable[[], float] = time.monotonic, width: Callable[[], int] | None = None) -> None:
        self.stream = stream or sys.stdout
        if is_tty is None:
            try:
                is_tty = bool(self.stream.isatty())
            except (AttributeError, ValueError):
                is_tty = False
        self.is_tty = is_tty
        self.min_interval_s = min_interval_s
        self.clock = clock
        self.width = width or (lambda: shutil.get_terminal_size((100, 20)).columns)
        self._lock = threading.Lock()
        self._line_len = 0  # characters of the in-place line currently shown (TTY)
        self._stage = ""
        self._key: tuple[str, str] = ("", "")
        self._last_print = -1e18
        self._pending = ""

    def update(self, text: str) -> None:
        """Show a status text ("" clears the in-place line)."""
        with self._lock:
            if self.is_tty:
                self._tty(text)
            else:
                self._plain(text)

    def message(self, text: str) -> None:
        """A normal line (finishes an in-place status line first)."""
        with self._lock:
            self._end_line()
            self.stream.write(text + "\n")
            self.stream.flush()

    def finish(self) -> None:
        with self._lock:
            if self.is_tty:
                self._end_line()
            elif self._pending:
                self.stream.write(self._pending + "\n")
                self.stream.flush()
                self._pending = ""

    # -- internals ---------------------------------------------------------
    def _end_line(self) -> None:
        if self._line_len:
            self.stream.write("\n")
            self._line_len = 0

    def _tty(self, text: str) -> None:
        stage = stage_of(text) if text else ""
        if stage != self._stage and self._line_len:
            self.stream.write("\n")  # keep the finished stage visible
            self._line_len = 0
        self._stage = stage
        maxw = max(20, self.width() - 1)
        shown = text if len(text) <= maxw else text[: maxw - 1] + "…"
        pad = max(0, self._line_len - len(shown))
        self.stream.write("\r" + shown + " " * pad + ("\r" + shown if pad else ""))
        self.stream.flush()
        self._line_len = len(shown)

    def _plain(self, text: str) -> None:
        if not text:
            return
        now = self.clock()
        stage = stage_of(text)
        key = (stage, percent_of(text))
        if stage != self._key[0]:
            due = True
        elif key != self._key or text != self._pending:
            due = now - self._last_print >= self.min_interval_s
        else:
            due = False
        if due:
            self.stream.write(text + "\n")
            self.stream.flush()
            self._last_print = now
            self._key = key
            self._pending = ""
        else:
            self._pending = text


_default: dict[str, Any] = {}


def default_status() -> ConsoleStatus:
    if "s" not in _default or _default["s"].stream is not sys.stdout:
        _default["s"] = ConsoleStatus()
    return _default["s"]  # type: ignore[no-any-return]
