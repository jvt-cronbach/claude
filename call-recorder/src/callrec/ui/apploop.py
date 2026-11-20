"""The app thread of the web UI: one queue, one thread - the same single-thread guarantee the Tk
thread gave (controller, AppActions, processing chain and dialogs all run here).

Interface identical to ``TkUi.post/call`` plus ``after`` (like Tk's ``root.after``) and ``wait``,
which keeps working off posted calls while it waits for a future (what Tk's nested event loop
did while a modal dialog was open).
"""

from __future__ import annotations

import heapq
import itertools
import logging
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")


class AppLoop:
    TICK_S = 0.05

    def __init__(self, name: str = "app") -> None:
        self._q: queue.Queue[Callable[[], object] | None] = queue.Queue()
        self._timers: list[tuple[float, int, Callable[[], object]]] = []
        self._seq = itertools.count()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._stopped = threading.Event()
        self._depth = 0  # nesting of wait() on the app thread

    # -- lifecycle ------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._q.put(None)

    def join(self, timeout: float | None = None) -> None:
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout)

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    def on_thread(self) -> bool:
        return threading.current_thread() is self._thread

    # -- scheduling -----------------------------------------------------
    def post(self, fn: Callable[[], object]) -> None:
        """Run ``fn`` on the app thread (fire and forget)."""
        self._q.put(fn)

    def after(self, ms: int, fn: Callable[[], object]) -> None:
        """Run ``fn`` on the app thread after ``ms`` milliseconds (like Tk's ``after``)."""
        with self._lock:
            heapq.heappush(self._timers, (time.monotonic() + ms / 1000.0, next(self._seq), fn))
        self._q.put(lambda: None)  # wake the loop so it recomputes its timeout

    def call(self, fn: Callable[[], T], timeout: float | None = None) -> T:
        """Run ``fn`` on the app thread and wait for its result (directly when already there)."""
        if self.on_thread():
            return fn()
        fut: Future[T] = Future()

        def wrapper() -> None:
            if not fut.set_running_or_notify_cancel():
                return
            try:
                fut.set_result(fn())
            except BaseException as exc:  # noqa: BLE001 - handed to the caller
                fut.set_exception(exc)

        self.post(wrapper)
        return fut.result(timeout)

    def wait(self, fut: Future[T], timeout: float | None = None) -> T:
        """Wait for ``fut``. On the app thread other posted calls keep running meanwhile (nested
        loop); elsewhere this is a plain blocking wait."""
        if not self.on_thread():
            return fut.result(timeout)
        deadline = None if timeout is None else time.monotonic() + timeout
        self._depth += 1
        try:
            while not fut.done():
                if self._stopped.is_set():
                    fut.cancel()
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError
                self._run_once(self.TICK_S)
        finally:
            self._depth -= 1
        return fut.result(0)

    # -- loop -----------------------------------------------------------
    def _due(self) -> list[Callable[[], object]]:
        now = time.monotonic()
        out = []
        with self._lock:
            while self._timers and self._timers[0][0] <= now:
                out.append(heapq.heappop(self._timers)[2])
        return out

    def _next_timeout(self, default: float) -> float:
        with self._lock:
            if not self._timers:
                return default
            return max(0.0, min(default, self._timers[0][0] - time.monotonic()))

    def _run_once(self, max_wait: float) -> bool:
        for fn in self._due():
            self._safe(fn)
        try:
            item = self._q.get(timeout=self._next_timeout(max_wait))
        except queue.Empty:
            return True
        if item is None:
            return False
        self._safe(item)
        return True

    @staticmethod
    def _safe(fn: Callable[[], object]) -> None:
        try:
            fn()
        except Exception:
            log.exception("Fehler in einem App-Aufruf")

    def _run(self) -> None:
        while not self._stopped.is_set():
            if not self._run_once(0.5):
                break

    def run_pending(self, max_items: int = 1000) -> int:
        """Tests: run what is due/queued right now on the calling thread."""
        n = 0
        for fn in self._due():
            self._safe(fn)
            n += 1
        while n < max_items:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                self._safe(item)
                n += 1
        return n


def make_future() -> Future[Any]:
    return Future()
