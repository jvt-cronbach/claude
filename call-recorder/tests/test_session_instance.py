"""Windows session end and the second start (0.3.1) - with fakes for the Windows parts."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

from callrec.ui import app as app_mod
from callrec.ui.sessionend import SessionEnd, install_winforms, is_session_end, run_bounded


class Handlers(list):
    def __iadd__(self, fn: Any) -> Handlers:
        self.append(fn)
        return self


class FakeForm:
    def __init__(self) -> None:
        self.FormClosing = Handlers()

    def close(self, reason: str) -> bool:
        """Like WinForms: every handler in order; True = the close/session end is allowed."""
        args = SimpleNamespace(CloseReason=reason, Cancel=False)
        args.Cancel = True  # pywebview's on_closing: close-to-tray cancelled it
        for h in self.FormClosing:
            h(self, args)
        return not args.Cancel


def test_session_end_lifts_close_to_tray_and_stops_once():
    calls: list[str] = []
    session = SessionEnd(lambda: calls.append("stop"), lambda: calls.append("quit"))
    forms = [FakeForm(), FakeForm()]
    wins = [SimpleNamespace(native=f) for f in forms] + [SimpleNamespace(native=None)]
    assert install_winforms(wins, session) == 2
    assert install_winforms(wins, session) == 0  # not twice
    assert forms[0].close("UserClosing") is False and calls == []  # the X button: still to the tray
    assert forms[0].close("WindowsShutDown") is True
    assert forms[1].close("WindowsShutDown") is True
    assert calls == ["stop", "quit"]  # once, although every window was asked
    assert is_session_end("WindowsShutDown") and not is_session_end("TaskManagerClosing")


def test_run_bounded_pumps_and_gives_up_in_time():
    pumped: list[int] = []
    gate = threading.Event()
    assert run_bounded(lambda: gate.wait(1), lambda: (pumped.append(1), gate.set())) is True and pumped
    t0 = time.monotonic()
    assert run_bounded(lambda: time.sleep(2), timeout_s=0.2) is False
    assert time.monotonic() - t0 < 1.5


def test_end_session_stops_recording_with_reason():
    from callrec.ui.actions import AppActions

    stops: list[tuple[str, str]] = []
    ctl = SimpleNamespace(is_recording=True, stop=lambda reason, by="user": stops.append((reason, by)) or object())
    fake = SimpleNamespace(autostop=SimpleNamespace(shutdown=lambda: stops.append(("autostop", ""))), controller=ctl)
    assert AppActions.end_session(fake) is True  # type: ignore[arg-type]
    assert stops == [("autostop", ""), ("system_shutdown", "system")]
    ctl.is_recording = False
    assert AppActions.end_session(fake) is False  # type: ignore[arg-type]


def test_after_recording_at_session_end_asks_at_next_start():
    from callrec.ui.actions import AppActions

    posted: list[Any] = []
    fake = SimpleNamespace(ui=SimpleNamespace(post=posted.append), cfg=None)
    AppActions.after_recording(fake, object(), "system_shutdown")  # type: ignore[arg-type]
    assert posted == []


class FakeKernel32:
    def __init__(self) -> None:
        self.events: dict[str, threading.Event] = {}
        self.handles: dict[int, str] = {}

    def CreateEventW(self, _sa: Any, _manual: bool, _initial: bool, name: str) -> int:  # noqa: N802
        self.events.setdefault(name, threading.Event())
        h = len(self.handles) + 1
        self.handles[h] = name
        return h

    def OpenEventW(self, _access: int, _inherit: bool, name: str) -> int:  # noqa: N802
        return self.CreateEventW(None, False, False, name) if name in self.events else 0

    def SetEvent(self, h: int) -> bool:  # noqa: N802
        self.events[self.handles[h]].set()
        return True

    def WaitForSingleObject(self, h: int, ms: int) -> int:  # noqa: N802
        ev = self.events[self.handles[h]]
        if ev.wait(ms / 1000):
            ev.clear()  # auto-reset
            return 0
        return 258  # WAIT_TIMEOUT

    def CloseHandle(self, h: int) -> bool:  # noqa: N802
        return True


def test_second_start_signals_the_running_instance():
    k = FakeKernel32()
    user32 = SimpleNamespace(AllowSetForegroundWindow=lambda x: True)
    assert app_mod.signal_running_instance(k, user32) is False  # nobody listening yet
    shown = threading.Event()
    listener = app_mod.ShowRequests(shown.set, k32=k)
    assert listener.start()
    assert app_mod.signal_running_instance(k, user32) is True
    assert shown.wait(2)
    listener.stop()


def test_run_tray_app_second_start_exits_cleanly(monkeypatch):
    monkeypatch.setattr(app_mod, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(app_mod, "acquire_single_instance", lambda: False)
    monkeypatch.setattr(app_mod, "signal_running_instance", lambda: True)
    assert app_mod.run_tray_app() == 0
    monkeypatch.setattr(app_mod, "signal_running_instance", lambda: False)
    assert app_mod.run_tray_app() == 1


def test_tk_session_end_handler_is_on_the_main_window_too(monkeypatch):
    """Tk runs the WM_SAVE_YOURSELF handler of the toplevel whose wrapper got WM_QUERYENDSESSION -
    the withdrawn root may have none, so the main window (a Toplevel) needs the handler as well."""
    from callrec.ui import sessionend

    class W:
        def __init__(self) -> None:
            self.protocols: dict[str, object] = {}

        def protocol(self, name: str, fn: object) -> None:
            self.protocols[name] = fn

    monkeypatch.setattr(sessionend.sys, "platform", "win32")
    root, main = W(), W()
    s = sessionend.SessionEnd(lambda: None, lambda: None, inline=True)
    sessionend.install_tk(root, s, main)
    assert root.protocols["WM_SAVE_YOURSELF"] == s.handle and main.protocols["WM_SAVE_YOURSELF"] == s.handle
