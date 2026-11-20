"""Tk-based implementation of :class:`callrec.controller.UiPort` plus the recording overlay."""

from __future__ import annotations

import functools
import logging
import queue
import threading
import time
import tkinter as tk
from collections.abc import Callable
from typing import Any, TypeVar

from callrec.calendar.service import CalendarService, Suggestion
from callrec.controller import StartDecision
from callrec.detect import Prompt
from callrec.models import Person
from callrec.recording import Recording
from callrec.ui import dialogs

log = logging.getLogger(__name__)
T = TypeVar("T")


class RecordingOverlay:
    """Small always-on-top red bar: '● AUFNAHME 00:12:34 – Titel  [Stopp]'.

    With an automatic end pending, a second row shows the countdown with
    "Weiter aufnehmen" / "Jetzt beenden" (read from ``autostop``, an AutoStopRunner)."""

    def __init__(self, root: tk.Tk, on_stop: Callable[[], None], autostop: Any = None) -> None:
        self.root = root
        self.on_stop = on_stop
        self.autostop = autostop
        self.win: tk.Toplevel | None = None
        self._t0 = 0.0
        self._title = ""
        self._label: tk.Label | None = None
        self._drag = (0, 0)
        self._prompt: tk.Frame | None = None
        self._prompt_label: tk.Label | None = None
        self._keep_btn: tk.Button | None = None
        self._stop_btn: tk.Button | None = None
        self._prompt_shown = False

    def show(self, title: str) -> None:
        self.hide()
        self._t0 = time.monotonic()
        self._title = title
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="#c62828")
        row = tk.Frame(win, bg="#c62828")
        row.pack(side="top", fill="x")
        self._label = tk.Label(row, text="", fg="white", bg="#c62828", font=("Segoe UI", 10, "bold"), padx=10, pady=4)
        self._label.pack(side="left")
        tk.Button(row, text="Stopp", command=self.on_stop, relief="flat", bg="#ffffff", fg="#c62828", padx=8).pack(
            side="left", padx=6, pady=4
        )
        prompt = tk.Frame(win, bg="#ffb300")
        self._prompt_label = tk.Label(prompt, text="", fg="black", bg="#ffb300", font=("Segoe UI", 10, "bold"),
                                      padx=10, pady=4, justify="left")
        self._prompt_label.pack(side="left")
        self._keep_btn = tk.Button(prompt, text="Weiter aufnehmen", command=self._keep, relief="flat", padx=8)
        self._keep_btn.pack(side="left", padx=4, pady=4)
        self._stop_btn = tk.Button(prompt, text="Jetzt beenden", command=self._stop_now, relief="flat", padx=8)
        self._stop_btn.pack(side="left", padx=(0, 6), pady=4)
        self._prompt = prompt
        self._prompt_shown = False
        for w in (win, row, self._label):
            w.bind("<ButtonPress-1>", self._start_drag)
            w.bind("<B1-Motion>", self._do_drag)
        win.update_idletasks()
        win.geometry(f"+{win.winfo_screenwidth() - win.winfo_width() - 340}+8")
        self.win = win
        self._tick()

    def _start_drag(self, e: Any) -> None:
        self._drag = (e.x, e.y)

    def _do_drag(self, e: Any) -> None:
        if self.win is not None:
            self.win.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _tick(self) -> None:
        if self.win is None or self._label is None:
            return
        s = int(time.monotonic() - self._t0)
        title = self._title if len(self._title) <= 40 else self._title[:39] + "…"
        self._label.configure(text=f"● AUFNAHME  {s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}  –  {title}")
        self._update_prompt()
        self.win.after(500, self._tick)

    def _update_prompt(self) -> None:
        if self._prompt is None or self._prompt_label is None:
            return
        view = None
        if self.autostop is not None:
            try:
                view = self.autostop.view()
            except Exception:
                log.debug("Countdown nicht lesbar", exc_info=True)
        if view is None:
            if self._prompt_shown:
                self._prompt.pack_forget()
                self._prompt_shown = False
            return
        self._prompt_label.configure(text=view.headline)
        if self._keep_btn is not None:
            self._keep_btn.configure(text=view.keep_label)
        if not self._prompt_shown:
            self._prompt.pack(side="top", fill="x")
            self._prompt_shown = True
            if self.win is not None:
                self.win.lift()

    def _keep(self) -> None:
        if self.autostop is not None:
            self.autostop.keep_recording()
        self._update_prompt()

    def _stop_now(self) -> None:
        if self.autostop is not None:
            self.autostop.stop_now()

    def hide(self) -> None:
        if self.win is not None:
            self.win.destroy()
            self.win = None


class TkUi:
    """Owns the hidden Tk root. Thread-safe: calls from other threads are marshalled."""

    def __init__(self, calendar: CalendarService, show_overlay: bool = True) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("CallRecorder")
        from callrec.ui.appid import tk_set_icon

        tk_set_icon(self.root)  # the app icon instead of Tk's feather / Python (taskbar, title bar)
        self.calendar = calendar
        self._calls: queue.Queue[Callable[[], object]] = queue.Queue()
        self._ui_thread = threading.get_ident()
        self.overlay: RecordingOverlay | None = None
        self.show_overlay = show_overlay
        self.tray: Any = None  # set by the app (TrayIcon)
        self.autostop: Any = None  # AutoStopRunner (countdown in the overlay), set by the app
        self.on_stop_clicked: Callable[[], None] = lambda: None
        self.review_handler: Callable[[Recording], None] = lambda rec: None
        # provider that will receive the transcript (decides the consent text), set by the app
        self.summary_provider: Callable[[], str] = lambda: ""
        # main window hook: called on the Tk thread with (title, message) of every notification
        self.notify_listeners: list[Callable[[str, str], None]] = []
        # consent mode ("ask" | "confirm") and the user's own name, set by the app
        self.consent_mode: Callable[[], str] = lambda: "confirm"
        self.me_provider: Callable[[], Person] = lambda: Person(name="Ich", role="self")
        from callrec.ui.startflow import PreparedStart

        self.prepared = PreparedStart()
        self.root.after(100, self._pump)

    # -- threading helpers ---------------------------------------------
    def _pump(self) -> None:
        # Re-arm first: a posted call may open a modal dialog (nested event loop);
        # other posted calls (hotkey, notifications) must keep running meanwhile.
        self.root.after(100, self._pump)
        try:
            while True:
                fn = self._calls.get_nowait()
                try:
                    fn()
                except Exception:
                    log.exception("Fehler in UI-Aufruf")
        except queue.Empty:
            pass

    def post(self, fn: Callable[[], object]) -> None:
        """Run ``fn`` on the Tk thread (fire and forget)."""
        self._calls.put(fn)

    def call(self, fn: Callable[[], T]) -> T:
        """Run ``fn`` on the Tk thread and wait for its result."""
        if threading.get_ident() == self._ui_thread:
            return fn()
        done = threading.Event()
        box: dict[str, Any] = {}

        def wrapper() -> None:
            try:
                box["v"] = fn()
            except BaseException as exc:  # noqa: BLE001
                box["e"] = exc
            finally:
                done.set()

        self.post(wrapper)
        done.wait()
        if "e" in box:
            raise box["e"]
        return box["v"]  # type: ignore[no-any-return]

    def mainloop(self) -> None:
        self.root.mainloop()

    def quit(self) -> None:
        self.post(self.root.quit)

    # -- UiPort ----------------------------------------------------------
    def ask_start(self, suggestion: Suggestion, me: Person) -> StartDecision | None:
        prepared = self.prepared.take()
        if prepared is not None:  # "Aufnehmen" was clicked in the call-detected prompt (mode "ask")
            return prepared  # type: ignore[no-any-return]
        provider = self.summary_provider()
        mode = self.consent_mode()
        return self.call(lambda: dialogs.ask_start(self.root, suggestion, me, self.calendar, summary_provider=provider,
                                                   mode=mode))

    def ask_record_prompt(self, prompt: Prompt) -> bool:
        if self.tray is not None:
            self.tray.notify("Gespräch aufnehmen?", prompt.message())
        if self.consent_mode() != "ask":
            return self.call(lambda: dialogs.ask_record_prompt(self.root, prompt))
        provider = self.summary_provider()
        choice = self.call(lambda: dialogs.ask_record_prompt_choice(self.root, prompt, provider))
        if choice == "record":
            from callrec.ui.startflow import decision_from_prompt

            self.prepared.put(decision_from_prompt(prompt, self.me_provider(), provider))
        return choice in ("record", "details")

    def drop_prepared_start(self) -> None:
        """See WebUi.drop_prepared_start: an unused decision from the prompt never starts a later recording."""
        self.prepared.take()

    def notify(self, title: str, message: str) -> None:
        log.info("Hinweis: %s - %s", title, message)
        if self.tray is not None:
            self.tray.notify(title, message)
        for fn in list(self.notify_listeners):
            self.post(functools.partial(fn, title, message))

    def set_recording_state(self, recording: bool, title: str) -> None:
        def apply() -> None:
            from callrec.ui.appid import tk_set_icon

            if self.tray is not None:
                self.tray.set_recording(recording, title)
            tk_set_icon(self.root, recording)  # red window/taskbar icon while recording
            if not self.show_overlay:
                return
            if recording:
                if self.overlay is None:
                    self.overlay = RecordingOverlay(self.root, self.on_stop_clicked, self.autostop)
                self.overlay.show(title)
            elif self.overlay is not None:
                self.overlay.hide()

        self.post(apply)

    def set_status(self, text: str) -> None:
        if self.tray is not None:
            self.tray.set_status(text)

    def open_review(self, rec: Recording) -> None:
        self.post(lambda: self.review_handler(rec))
