"""The web UI's implementation of ``controller.UiPort`` plus the Python -> page event channel and
the two small companion windows (recording indicator, call-detected prompt).

Threads: UiPort methods are called on the app thread (or, for notify/set_status, on any thread).
Nothing here waits for the GUI main thread from the app thread: events are handed to an emitter
thread, which talks to the web views (``run_js`` blocks until WebView2 ran the script).
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from callrec import hints
from callrec.calendar.service import CalendarService, Suggestion
from callrec.controller import StartDecision
from callrec.detect import Prompt
from callrec.models import Person
from callrec.recording import Recording
from callrec.ui.apploop import AppLoop
from callrec.ui.webdialogs import DialogBroker

log = logging.getLogger(__name__)

# tray notifications that the window shows in a better form (same list as the classic window)
SKIP_IN_WINDOW = ("Verarbeitung gestartet", "Neu-Transkription gestartet", "Zusammenfassung wird neu erstellt",
                  "CallRecorder bereit")
ERROR_WORDS = ("nicht möglich", "fehlgeschlagen", "Problem", "ungültig", "Achtung", "Keine Übertragung",
               "Keine Aufnahme")


class Events:
    """Python -> page. ``emit`` never blocks; coalesces the frequent kinds (state, levels)."""

    COALESCE = {"state", "indicator", "status"}

    def __init__(self) -> None:
        self._q: queue.Queue[tuple[str, str, Any] | None] = queue.Queue()
        self._targets: dict[str, Any] = {}  # name -> pywebview window
        self._ready: set[str] = set()
        self._latest: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="ui-events", daemon=True)
        self.sent: list[tuple[str, str, Any]] = []  # tests (no window)
        self.keep_sent = False

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._q.put(None)

    def attach(self, name: str, window: Any) -> None:
        self._targets[name] = window

    def set_ready(self, name: str, ready: bool = True) -> None:
        with self._lock:
            (self._ready.add if ready else self._ready.discard)(name)

    def emit(self, name: str, payload: Any, target: str = "main") -> None:
        if self.keep_sent:
            self.sent.append((target, name, payload))
        if name in self.COALESCE:
            with self._lock:
                fresh = (target, name) not in self._latest
                self._latest[(target, name)] = payload
            if fresh:
                self._q.put((target, name, _LATEST))
            return
        self._q.put((target, name, payload))

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            target, name, payload = item
            if payload is _LATEST:
                with self._lock:
                    payload = self._latest.pop((target, name), None)
            with self._lock:
                ready = target in self._ready
            win = self._targets.get(target)
            if win is None or not ready:
                continue
            script = f"window.cr && window.cr.emit({json.dumps(name)}, {json.dumps(payload, default=str)})"
            try:
                win.run_js(script)
            except Exception:
                log.debug("Ereignis %s an %s nicht zugestellt", name, target, exc_info=True)


_LATEST = object()


class WebUi:
    """Implements ``controller.UiPort`` for the web UI (see module doc for the threads)."""

    def __init__(self, loop: AppLoop, events: Events, calendar: CalendarService) -> None:
        from callrec.ui.startflow import PreparedStart

        self.loop = loop
        self.events = events
        self.calendar = calendar
        self.broker = DialogBroker(loop, events.emit, self._show_for_dialog)
        self.root = None  # no Tk
        self.tray: Any = None
        self.show_overlay = True
        self.summary_provider: Callable[[], str] = lambda: ""
        self.consent_mode: Callable[[], str] = lambda: "confirm"
        self.me_provider: Callable[[], Person] = lambda: Person(name="Ich", role="self")
        self.review_handler: Callable[[Recording], None] = lambda rec: None
        self.prepared = PreparedStart()
        self.windows: Any = None  # WebWindows (set by the app); None in tests
        self.status_text = ""
        self.toast_future: Future[str] | None = None
        self.toast_data: dict[str, Any] = {}

    # -- helpers -----------------------------------------------------------
    def post(self, fn: Callable[[], object]) -> None:
        self.loop.post(fn)

    def call(self, fn: Callable[[], Any]) -> Any:
        return self.loop.call(fn)

    def after(self, ms: int, fn: Callable[[], object]) -> None:
        self.loop.after(ms, fn)

    def _show_for_dialog(self, activate: bool) -> None:
        if self.windows is not None:
            self.windows.show_main(activate=activate)

    def main_visible(self) -> bool:
        return bool(self.windows is not None and self.windows.main_visible)

    # -- UiPort ----------------------------------------------------------------
    def ask_start(self, suggestion: Suggestion, me: Person) -> StartDecision | None:
        prepared = self.prepared.take()
        if prepared is not None:  # "Aufnehmen" was clicked in the call-detected prompt (mode "ask")
            return prepared  # type: ignore[no-any-return]
        from callrec.consent import ConsentError
        from callrec.people import parse_participants_text
        from callrec.ui.startflow import build_record, start_texts

        provider = self.summary_provider()
        mode = self.consent_mode()
        texts = start_texts(mode, provider)
        import uuid

        ctx = {"suggestion": suggestion}

        def data(sugg: Suggestion, error: str = "", title: str | None = None, people: str | None = None) -> dict[str, Any]:
            return start_dialog_data(texts, me, sugg, error, title, people)

        payload = data(suggestion)
        while True:
            did = uuid.uuid4().hex  # a fresh id per question (also when asked again after an error)
            self.broker.context[did] = ctx
            res = self.broker.ask("start", payload, activate=True, dialog_id=did)
            if not isinstance(res, dict) or res.get("action") != "start":
                return None
            title = str(res.get("title") or "").strip()[:200] or "Anruf"
            people_text = str(res.get("participants_text") or "")[:20000]
            participants = parse_participants_text(people_text)
            sugg = ctx["suggestion"]
            try:
                # mode "ask": this answer IS the click on "Aufnehmen"; mode "confirm": the ticked box
                user_action = True if mode == "ask" else res.get("checked") is True
                consent = build_record(mode, user_action=user_action, me=me, participants=participants,
                                       provider=provider)
            except ConsentError as exc:
                payload = data(sugg, str(exc), title, people_text)
                continue
            return StartDecision(title, participants, sugg.event, consent)

    def ask_record_prompt(self, prompt: Prompt) -> bool:
        from callrec.consent import reminder_statement
        from callrec.ui.startflow import people_shown

        mode = self.consent_mode()
        provider = self.summary_provider()
        ev = prompt.event
        import uuid

        # the id ties a click to this prompt: the toast window is reused, so a click on content still
        # showing an earlier prompt (before the new one rendered) must not answer this one
        data = {"id": uuid.uuid4().hex, "mode": mode, "question": prompt.question(), "message": prompt.message(),
                "reminder": reminder_statement(provider) if mode == "ask" else "",
                "people": people_shown(list(ev.participants())) if ev else [],
                "timeout_s": 90}
        if self.tray is not None and not (self.windows is not None and self.windows.toast_ok):
            self.tray.notify("Gespräch aufnehmen?", prompt.message())
        choice = self.ask_toast(data)
        if mode == "ask" and choice == "record":
            from callrec.ui.startflow import decision_from_prompt

            self.prepared.put(decision_from_prompt(prompt, self.me_provider(), provider))
            return True
        return choice in ("record", "details")

    def ask_toast(self, data: dict[str, Any]) -> str:
        """Show the call-detected prompt (own window, no focus) and wait at most 90 s. Expiry = "".
        Without the toast window the same question appears in the main window, without focus."""
        fut: Future[str] = Future()
        self.toast_future, self.toast_data = fut, data
        try:
            if self.windows is not None and self.windows.toast_ok:
                self.events.emit("toast", data, target="toast")
                self.windows.show_toast(data)
                try:
                    return str(self.loop.wait(fut, timeout=float(data.get("timeout_s") or 90)) or "")
                except TimeoutError:
                    return ""
            res = self.broker.ask("record_prompt", data, activate=False, timeout=float(data.get("timeout_s") or 90))
            return str((res or {}).get("choice") or "") if isinstance(res, dict) else ""
        finally:
            self.toast_future = None
            if self.windows is not None:
                self.windows.hide_toast()

    def toast_answer(self, choice: str, toast_id: str = "") -> bool:
        """The click in the toast window - only for the prompt it showed (``toast_id``)."""
        fut = self.toast_future
        if fut is None or fut.done() or choice not in ("record", "details", "dismiss"):
            return False
        if not toast_id or toast_id != str(self.toast_data.get("id") or ""):
            log.info("Antwort im Hinweisfenster verworfen (gehört nicht zur aktuellen Frage)")
            return False
        fut.set_result(choice)
        return True

    def drop_prepared_start(self) -> None:
        """After the start that followed the call-detected prompt: an unused decision (start refused
        because another start dialog was open or a recording ran) must never start a later one."""
        self.prepared.take()

    def notify(self, title: str, message: str) -> None:
        log.info("Hinweis: %s - %s", title, message)
        visible = self.main_visible()
        if self.tray is not None and not visible:
            self.tray.notify(title, message)
        if title.startswith(SKIP_IN_WINDOW):
            return
        level = "error" if any(k in title for k in ERROR_WORDS) else "info"
        payload: dict[str, Any] = {"title": title, "message": message, "level": level}
        if level == "error":
            h = hints.explain(message)
            payload["hint"] = {"next": h.next_step, "action": h.action, "label": h.action_label}
        self.events.emit("notify", payload)

    def set_recording_state(self, recording: bool, title: str) -> None:
        if self.tray is not None:
            self.tray.set_recording(recording, title)
        self.events.emit("recording", {"on": recording, "title": title})
        if self.windows is not None:
            self.windows.set_indicator(recording and self.show_overlay)
            if sys.platform == "win32":
                from callrec.ui.appid import winforms_set_icon

                winforms_set_icon([self.windows.main], recording)  # red taskbar button while recording

    def set_status(self, text: str) -> None:
        self.status_text = text
        if self.tray is not None:
            self.tray.set_status(text)
        self.events.emit("status", {"text": text})

    def open_review(self, rec: Recording) -> None:
        self.post(lambda: self.review_handler(rec))


def start_dialog_data(texts: Any, me: Person, sugg: Suggestion, error: str = "", title: str | None = None,
                      people: str | None = None) -> dict[str, Any]:
    """What the start dialog shows (``texts``: :class:`callrec.ui.startflow.StartTexts`)."""
    from callrec.people import format_participants
    from callrec.ui.dto import iso

    ev = sugg.event
    return {
        "mode": texts.mode, "statement": texts.statement, "checkbox": texts.checkbox,
        "start_label": texts.start_label, "cancel_label": texts.cancel_label, "cloud_note": texts.cloud_note,
        "record_note": texts.record_note, "me": me.display,
        "title": title if title is not None else (ev.subject if ev else ""),
        "participants_text": people if people is not None else format_participants(sugg.participants),
        "info": suggestion_text(sugg),
        "event": {"subject": ev.subject, "start": iso(ev.start), "end": iso(ev.end)} if ev else None,
        "error": error,
    }


def suggestion_text(s: Suggestion) -> str:
    if s.event:
        ev = s.event
        src = {"graph": "Outlook-Kalender", "cache": "Kalender (zwischengespeichert)", "ics": ".ics-Datei"}.get(
            s.source, s.source)
        return f"Termin aus {src}: {ev.subject} ({ev.start.astimezone():%H:%M}–{ev.end.astimezone():%H:%M})"
    return s.message or "Kein Termin gefunden – Titel und Teilnehmende eingeben."


class WebWindows:
    """The pywebview windows (main, indicator, toast) and how they are shown without taking focus.

    Windows-only details (no-activate show, topmost) go through user32; elsewhere the plain
    pywebview calls are used."""

    INDICATOR_SIZE = (380, 64)
    INDICATOR_COUNTDOWN_H = 156
    TOAST_SIZE = (400, 252)

    def __init__(self, main: Any, indicator: Any = None, toast: Any = None) -> None:
        self.main = main
        self.indicator = indicator
        self.toast = toast
        self.main_visible = False
        self.quitting = False
        self.indicator_shown = False
        self.toast_ok = toast is not None
        self._lock = threading.Lock()
        self.on_show: Callable[[], None] = lambda: None

    # -- main ----------------------------------------------------------------
    def show_main(self, activate: bool = True) -> None:
        if self.quitting:
            return
        try:
            if activate:
                self.main.show()
                self.main.restore()
            else:
                _show_no_activate(self.main, topmost=False)
            self.main_visible = True
            self.on_show()
        except Exception:
            log.debug("Hauptfenster nicht anzeigbar", exc_info=True)

    def hide_main(self) -> None:
        try:
            self.main.hide()
        except Exception:
            log.debug("Hauptfenster nicht versteckbar", exc_info=True)
        self.main_visible = False

    # -- indicator -----------------------------------------------------------
    def set_indicator(self, on: bool, countdown: bool = False) -> None:
        win = self.indicator
        if win is None or self.quitting:
            return
        with self._lock:
            try:
                if on:
                    w, h = self.INDICATOR_SIZE
                    if countdown:
                        h = self.INDICATOR_COUNTDOWN_H
                    win.resize(w, h)
                    if not self.indicator_shown:
                        x, y = corner(w, h, "top-right")
                        win.move(x, y)
                    _show_no_activate(win, topmost=True)
                    self.indicator_shown = True
                elif self.indicator_shown:
                    win.hide()
                    self.indicator_shown = False
            except Exception:
                log.debug("Aufnahme-Anzeige nicht umschaltbar", exc_info=True)

    # -- toast ---------------------------------------------------------------
    def show_toast(self, data: dict[str, Any]) -> None:
        win = self.toast
        if win is None:
            return
        try:
            w, h = self.TOAST_SIZE
            x, y = corner(w, h, "bottom-right")
            win.move(x, y)
            _show_no_activate(win, topmost=True)
        except Exception:
            log.debug("„Call erkannt“ nicht anzeigbar", exc_info=True)

    def hide_toast(self) -> None:
        if self.toast is not None:
            try:
                self.toast.hide()
            except Exception:
                log.debug("Toast nicht versteckbar", exc_info=True)

    def destroy_all(self) -> None:
        self.quitting = True
        for w in (self.toast, self.indicator, self.main):
            if w is not None:
                try:
                    w.destroy()
                except Exception:
                    log.debug("Fenster nicht zerstörbar", exc_info=True)


def corner(w: int, h: int, where: str) -> tuple[int, int]:
    """Position in logical pixels on the primary screen (taskbar ≈ 56 px left free at the bottom)."""
    try:
        import webview

        s = webview.screens[0]
        sx, sy, sw, sh = s.x, s.y, s.width, s.height
    except Exception:
        sx, sy, sw, sh = 0, 0, 1366, 768
    if where == "top-right":
        return sx + sw - w - 24, sy + 12
    return sx + sw - w - 16, sy + sh - h - 64


def _show_no_activate(win: Any, topmost: bool) -> None:
    """Show without stealing the keyboard focus (Teams keeps it). pywebview's ``show()`` calls
    ``Form.Activate()``; on Windows the window is shown with SW_SHOWNOACTIVATE instead and, for the
    companion windows, pinned topmost with SWP_NOACTIVATE (also over Teams in full screen, which
    is a borderless window, not exclusive full screen)."""
    import sys

    native = getattr(win, "native", None)
    if sys.platform == "win32" and native is not None:
        try:

            hwnd = int(native.Handle.ToInt64())
            user32 = _user32()
            user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
            if topmost:
                # HWND_TOPMOST, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW
                user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040)
            return
        except Exception:
            log.debug("Anzeigen ohne Fokus nicht möglich", exc_info=True)
    win.show()


_USER32: Any = None


def _user32() -> Any:
    """Own WinDLL instance with 64-bit-safe signatures (HWND is pointer-sized; ctypes' default would pass
    an int as a 32-bit C int). Not ``ctypes.windll.user32``: that object is shared with pywebview."""
    global _USER32
    if _USER32 is None:
        import ctypes
        from ctypes import wintypes

        u = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.ShowWindow.restype = wintypes.BOOL
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_uint]
        u.SetWindowPos.restype = wintypes.BOOL
        _USER32 = u
    return _USER32


def throttle(interval_s: float) -> Callable[[], bool]:
    last = [0.0]

    def ok() -> bool:
        now = time.monotonic()
        if now - last[0] >= interval_s:
            last[0] = now
            return True
        return False

    return ok
