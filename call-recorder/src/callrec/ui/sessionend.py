"""Windows shutdown / logoff while CallRecorder runs (both UIs).

Closing the main window only hides it (close-to-tray). At the end of the Windows session that must
not happen: a running recording is stopped and finalised through the normal ``Controller.stop``
path (reason ``system_shutdown``), then the app quits, and the session end is not blocked.

How the session end is detected:

* **Web UI (pywebview 6.2.1, WinForms):** Windows sends ``WM_QUERYENDSESSION`` to every top-level
  window, hidden ones included. WinForms turns it into ``Form.FormClosing`` with
  ``CloseReason.WindowsShutDown`` (Form.WmClose); the result of that message is "allowed" unless a
  handler set ``Cancel``. pywebview's own FormClosing handler (winforms.py ``BrowserView.on_closing``)
  calls ``window.events.closing`` *without* the event args, so the close-to-tray handler cannot see
  the reason and cancels. :func:`install_winforms` therefore adds a second FormClosing handler to
  every window's form (``window.native``, a ``System.Windows.Forms.Form``); it runs after
  pywebview's, reads ``CloseReason`` and lifts the cancellation for a session end.
* **Classic UI (Tk 8.6):** Tk synthesises the ``WM_SAVE_YOURSELF`` window-manager protocol from
  ``WM_QUERYENDSESSION`` (tkWinWm.c, WmProc) and runs the handler synchronously before it answers.

Both handlers run on the GUI thread and must answer within the few seconds Windows waits. The stop
runs on the app thread; :func:`run_bounded` waits for it at most ``STOP_TIMEOUT_S`` while pumping
GUI messages (the stop may need the GUI thread, e.g. to hide the recording indicator).
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

STOP_TIMEOUT_S = 8.0
REASON = "system_shutdown"
BLOCK_REASON_TEXT = "CallRecorder speichert die laufende Aufnahme."


def run_bounded(work: Callable[[], object], pump: Callable[[], None] = lambda: None,
                timeout_s: float = STOP_TIMEOUT_S) -> bool:
    """Run ``work`` in a helper thread; keep calling ``pump`` until it is done or the time is up.
    True = finished in time."""
    done = threading.Event()

    def run() -> None:
        try:
            work()
        except Exception:
            log.exception("Beenden zum Sitzungsende fehlgeschlagen")
        finally:
            done.set()

    threading.Thread(target=run, name="session-end", daemon=True).start()
    deadline = time.monotonic() + timeout_s
    while not done.is_set() and time.monotonic() < deadline:
        pump()
        done.wait(0.02)
    return done.is_set()


class SessionEnd:
    """Shared by both UIs: ``stop`` finalises the recording (on the app thread), ``quit`` ends the app."""

    def __init__(self, stop: Callable[[], object], quit: Callable[[], object],
                 pump: Callable[[], None] = lambda: None, inline: bool = False) -> None:
        """``inline``: run ``stop`` directly (the caller is already on the app thread - Tk)."""
        self.stop = stop
        self.quit = quit
        self.pump = pump
        self.inline = inline
        self.ending = False

    def handle(self) -> bool:
        """The session ends. Returns True when the recording was finalised in time."""
        if self.ending:
            return True
        self.ending = True
        log.info("Windows wird beendet oder abgemeldet - laufende Aufnahme wird gespeichert")
        if self.inline:
            try:
                self.stop()
                ok = True
            except Exception:
                log.exception("Aufnahme beim Sitzungsende nicht gespeichert")
                ok = False
        else:
            ok = run_bounded(self.stop, self.pump)
        if not ok:
            log.warning("Aufnahme beim Sitzungsende nicht rechtzeitig gespeichert")
        try:
            self.quit()
        except Exception:
            log.exception("Beenden zum Sitzungsende fehlgeschlagen")
        return ok


def is_session_end(close_reason: Any) -> bool:
    """``System.Windows.Forms.CloseReason.WindowsShutDown`` (also accepts its name/number)."""
    return str(close_reason) in ("WindowsShutDown", "CloseReason.WindowsShutDown", "1")


def install_winforms(windows: list[Any], session: SessionEnd) -> int:
    """Add the session-end handler to the WinForms form of each pywebview window (``window.native``,
    set by pywebview once the form exists). Returns how many forms got it."""
    n = 0
    for w in windows:
        form = getattr(w, "native", None)
        if form is None or getattr(form, "_callrec_session_end", False):
            continue

        def on_form_closing(sender: Any, args: Any) -> None:
            if not is_session_end(getattr(args, "CloseReason", None)):
                return
            args.Cancel = False  # pywebview's close-to-tray handler cancelled it
            session.handle()

        try:
            form.FormClosing += on_form_closing
            form._callrec_session_end = True
            n += 1
        except Exception:
            log.warning("Sitzungsende-Erkennung für ein Fenster nicht eingerichtet", exc_info=True)
    return n


def winforms_pump() -> Callable[[], None]:
    """``Application.DoEvents`` (only on the WinForms GUI thread)."""
    try:
        import System.Windows.Forms as WinForms  # type: ignore[import-not-found]
    except Exception:
        return lambda: None
    return WinForms.Application.DoEvents  # type: ignore[no-any-return]


def install_tk(root: Any, session: SessionEnd) -> None:
    """Tk: ``WM_SAVE_YOURSELF`` comes from ``WM_QUERYENDSESSION`` (Windows only)."""
    if sys.platform != "win32":
        return
    root.protocol("WM_SAVE_YOURSELF", session.handle)
