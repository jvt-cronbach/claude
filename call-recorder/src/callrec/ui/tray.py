"""System tray icon (pystray). Menu callbacks run on pystray's thread and are posted to Tk.

pystray runs its own message loop on a separate thread (``run_detached``); nothing here touches
Tk directly - every menu action is handed to the Tk thread with ``post``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

IDLE = "#607d8b"
RECORDING = "#d32f2f"
BUSY = "#f57c00"


def app_icon(state: str = "idle", size: int = 64) -> Image.Image | None:
    """The app artwork (callrec/ui/assets): idle = indigo, recording = red, busy = amber."""
    from callrec.ui.appid import png_path

    variant = {"recording": "callrec-recording", "busy": "callrec-busy"}.get(state, "callrec")
    try:
        return Image.open(png_path(size, variant)).convert("RGBA")
    except OSError:
        return None


def make_icon(color: str, recording: bool = False, size: int = 64) -> Image.Image:
    state = "recording" if recording or color == RECORDING else ("busy" if color == BUSY else "idle")
    art = app_icon(state, size)
    if art is not None:
        return art
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # microphone-like shape: filled circle with a ring when recording
    d.ellipse((6, 6, size - 6, size - 6), fill=color)
    if recording:
        d.ellipse((20, 20, size - 20, size - 20), fill="white")
    else:
        d.rounded_rectangle((size // 2 - 7, 16, size // 2 + 7, 40), radius=7, fill="white")
        d.rectangle((size // 2 - 2, 40, size // 2 + 2, 48), fill="white")
    return img


class TrayIcon:
    READY_TIMEOUT_S = 5.0

    def __init__(self, actions: dict[str, Callable[[], None]], post: Callable[[Callable[[], None]], None]) -> None:
        import pystray

        self._pystray = pystray
        self.actions = actions
        self.post = post
        self.recording = False
        self.title = ""
        self.status = ""
        self._lock = threading.Lock()
        self._ready = threading.Event()

        def act(name: str) -> Callable[[Any, Any], None]:
            return lambda icon, item: self.post(self.actions[name])

        item = pystray.MenuItem
        # Short menu now that the main window exists. Left click (and double click) on the icon
        # runs the default item = open the main window.
        entries = [
            item("CallRecorder öffnen", act("show_window"), default=True),
            item(lambda i: "Aufnahme beenden" if self.recording else "Aufnahme starten …", act("toggle")),
            pystray.Menu.SEPARATOR,
            item("Letzte Aufnahme: Zusammenfassung an SharePoint übergeben \u2026", act("handoff_last")),
            item("Laufende Verarbeitung abbrechen", act("cancel_processing"),
                 visible=lambda i: bool(self.status) and not self.recording),
            pystray.Menu.SEPARATOR,
            item("Einstellungen …", act("settings")),
            item("Beenden", act("quit")),
        ]
        self.icon = pystray.Icon("CallRecorder", make_icon(IDLE), "CallRecorder – bereit", menu=pystray.Menu(*entries))

    def start(self) -> None:
        # On Windows run_detached() starts pystray's own message-loop thread; the setup
        # callback runs once that loop (and its window) exists.
        def setup(icon: Any) -> None:
            icon.visible = True
            self._ready.set()

        self.icon.run_detached(setup)

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            log.debug("Tray stop", exc_info=True)

    def _refresh(self) -> None:
        if self.recording:
            text = f"● Aufnahme läuft – {self.title}"
        elif self.status:
            text = self.status
        else:
            text = "CallRecorder – bereit"
        self.icon.title = text[:120]  # Windows tooltip limit is 127 characters
        color = RECORDING if self.recording else (BUSY if self.status else IDLE)
        self.icon.icon = make_icon(color, self.recording)
        self.icon.update_menu()

    def set_recording(self, recording: bool, title: str) -> None:
        with self._lock:
            self.recording = recording
            self.title = title
            self._refresh()

    def set_status(self, text: str) -> None:
        with self._lock:
            self.status = text
            self._refresh()

    def notify(self, title: str, message: str) -> None:
        # Before the icon is visible Shell_NotifyIcon has no window/icon to attach the
        # balloon to and the message is silently lost (e.g. "hotkey could not be registered"
        # or "interrupted recording" right at startup).
        if not self._ready.wait(self.READY_TIMEOUT_S):
            log.info("Benachrichtigung ohne Tray-Symbol: %s - %s", title, message)
            return
        try:
            if self.icon.HAS_NOTIFICATION:
                self.icon.notify(message[:250], title[:60])
        except Exception:
            log.debug("Benachrichtigung fehlgeschlagen", exc_info=True)
