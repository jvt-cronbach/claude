"""Global hotkey via Win32 RegisterHotKey (no keyboard hook, no extra dependency)."""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

_MODS = {
    "ctrl": MOD_CONTROL, "strg": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT, "umschalt": MOD_SHIFT,
    "win": MOD_WIN,
}
_NAMED_KEYS = {
    "space": 0x20, "leertaste": 0x20, "enter": 0x0D, "pause": 0x13, "insert": 0x2D, "einfg": 0x2D,
    "home": 0x24, "pos1": 0x24, "end": 0x23, "ende": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "scrolllock": 0x91, "rollen": 0x91, "printscreen": 0x2C, "druck": 0x2C,
}


class HotkeyError(ValueError):
    pass


@dataclass(frozen=True)
class Hotkey:
    modifiers: int
    vk: int
    text: str


def parse_hotkey(text: str) -> Hotkey:
    parts = [p.strip().lower() for p in text.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise HotkeyError("Leere Tastenkombination")
    mods = 0
    key = parts[-1]
    for p in parts[:-1]:
        if p not in _MODS:
            raise HotkeyError(f"Unbekannte Zusatztaste: {p!r}")
        mods |= _MODS[p]
    if len(key) == 1 and (key.isascii() and key.isalnum()):
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    elif key in _NAMED_KEYS:
        vk = _NAMED_KEYS[key]
    else:
        raise HotkeyError(f"Unbekannte Taste: {key!r}")
    if mods == 0 and not (0x70 <= vk <= 0x87):
        raise HotkeyError("Bitte mindestens eine Zusatztaste (Strg/Alt/Umschalt/Win) verwenden")
    return Hotkey(mods | MOD_NOREPEAT, vk, text)


class GlobalHotkey:
    """Registers the hotkey in a dedicated thread with its own message loop."""

    HOTKEY_ID = 0x0C0D  # applications must use 0x0000-0xBFFF (0xC000+ is for shared DLLs)

    def __init__(self, hotkey: Hotkey, callback: Callable[[], None]) -> None:
        self.hotkey = hotkey
        self.callback = callback
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self.error: str = ""

    def start(self) -> bool:
        if sys.platform != "win32":
            self.error = "Globale Tastenkürzel nur unter Windows"
            return False
        self._thread = threading.Thread(target=self._run, name="hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return not self.error

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        self._thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, self.HOTKEY_ID, self.hotkey.modifiers, self.hotkey.vk):
            self.error = (
                f"Tastenkürzel {self.hotkey.text} konnte nicht registriert werden "
                "(wird vermutlich schon von einem anderen Programm verwendet)."
            )
            log.error(self.error)
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == self.HOTKEY_ID:
                    try:
                        self.callback()
                    except Exception:
                        log.exception("Fehler im Hotkey-Callback")
        finally:
            user32.UnregisterHotKey(None, self.HOTKEY_ID)

    def stop(self) -> None:
        if sys.platform == "win32" and self._thread_id:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)  # type: ignore[attr-defined]
