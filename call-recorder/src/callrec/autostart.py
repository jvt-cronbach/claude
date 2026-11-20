""""Mit Windows starten": a per-user autostart entry, created and removed by the app itself.

The entry is the value ``CallRecorder`` under
``HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` (no admin rights) with
``"<installation>\\.venv\\Scripts\\pythonw.exe" -m callrec tray --minimized`` - the same entry
``setup.ps1 -Autostart`` writes. CallRecorder then starts minimised to the tray.

If the program folder was moved, the entry points at a pythonw.exe that no longer exists (or at
another installation): such a *stale* entry is detected and repaired at every start. An autostart
shortcut of earlier versions in the Startup folder is converted into the registry entry (otherwise
CallRecorder would start twice).

Registry access is behind a small port so the logic is testable on any platform.
"""

from __future__ import annotations

import logging
import os
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "CallRecorder"
ARGS = "-m callrec tray --minimized"
LEGACY_SHORTCUT = "CallRecorder.lnk"

OFF, ON, STALE, UNAVAILABLE = "off", "on", "stale", "unavailable"


class RegistryPort(Protocol):
    def read(self) -> str | None: ...

    def write(self, command: str) -> None: ...

    def delete(self) -> None: ...


class WinRunKey:
    """The real HKCU Run value (Windows only)."""

    def read(self) -> str | None:
        import winreg  # type: ignore[import-not-found,unused-ignore]

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:  # type: ignore[attr-defined,unused-ignore]
                value, _type = winreg.QueryValueEx(k, VALUE_NAME)  # type: ignore[attr-defined,unused-ignore]
                return str(value)
        except FileNotFoundError:
            return None

    def write(self, command: str) -> None:
        import winreg  # type: ignore[import-not-found,unused-ignore]

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,  # type: ignore[attr-defined,unused-ignore]
                                winreg.KEY_SET_VALUE) as k:  # type: ignore[attr-defined,unused-ignore]
            winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, command)  # type: ignore[attr-defined,unused-ignore]

    def delete(self) -> None:
        import winreg  # type: ignore[import-not-found,unused-ignore]

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,  # type: ignore[attr-defined,unused-ignore]
                                winreg.KEY_SET_VALUE) as k:  # type: ignore[attr-defined,unused-ignore]
                winreg.DeleteValue(k, VALUE_NAME)  # type: ignore[attr-defined,unused-ignore]
        except FileNotFoundError:
            pass


def pythonw_for(executable: str | None = None) -> Path:
    """pythonw.exe next to the running interpreter (no console window), else the interpreter itself."""
    exe = Path(executable or sys.executable)
    w = exe.with_name("pythonw.exe")
    return w if w.exists() else exe


def desired_command(executable: str | None = None) -> str:
    return f'"{pythonw_for(executable)}" {ARGS}'


def command_executable(command: str) -> str:
    """The program path of a Run command (quoted or not)."""
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        return command[1:end] if end > 0 else command[1:]
    try:
        return shlex.split(command, posix=False)[0]
    except (ValueError, IndexError):
        return command.split(" ", 1)[0]


def settings_change(state: str, shown_checked: bool, want: bool) -> str:
    """What saving the settings does with the "Mit Windows starten" box: "enable", "disable" or ""
    (unchanged box: nothing - a stale entry is repaired at the start, a foreign one is left alone)."""
    if state == UNAVAILABLE or want == shown_checked:
        return ""
    return "enable" if want else "disable"


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a.strip())) == os.path.normcase(os.path.normpath(b.strip()))


@dataclass
class AutostartStatus:
    state: str  # off | on | stale | unavailable
    current: str = ""  # command in the registry
    desired: str = ""
    detail: str = ""  # German explanation
    legacy_shortcut: Path | None = None


def legacy_shortcut_path(startup_dir: Path | None = None) -> Path | None:
    if startup_dir is None:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        startup_dir = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    p = startup_dir / LEGACY_SHORTCUT
    return p if p.exists() else None


class Autostart:
    def __init__(self, registry: RegistryPort | None, executable: str | None = None,
                 exists: Callable[[str], bool] = os.path.exists, startup_dir: Path | None = None) -> None:
        self.registry = registry
        self.executable = executable
        self.exists = exists
        self.startup_dir = startup_dir

    @classmethod
    def for_this_pc(cls) -> Autostart:
        return cls(WinRunKey() if sys.platform == "win32" else None)

    def desired(self) -> str:
        return desired_command(self.executable)

    def status(self) -> AutostartStatus:
        if self.registry is None:
            return AutostartStatus(UNAVAILABLE, detail="Nur unter Windows verfügbar.")
        desired = self.desired()
        legacy = legacy_shortcut_path(self.startup_dir) if (self.startup_dir or sys.platform == "win32") else None
        try:
            current = self.registry.read()
        except OSError as exc:
            return AutostartStatus(UNAVAILABLE, desired=desired, detail=f"Autostart-Eintrag nicht lesbar: {exc}")
        if not current:
            if legacy is not None:
                return AutostartStatus(STALE, "", desired, "Autostart über eine Verknüpfung einer älteren Version "
                                       f"({legacy}) – wird in den Autostart-Eintrag umgewandelt.", legacy)
            return AutostartStatus(OFF, "", desired, "CallRecorder startet nicht mit Windows.")
        exe = command_executable(current)
        if not self.exists(exe):
            return AutostartStatus(STALE, current, desired, f"Der Autostart-Eintrag zeigt auf {exe}, das es nicht mehr "
                                   "gibt (Programmordner verschoben?) – wird auf diese Installation korrigiert.", legacy)
        if not _same(current, desired):
            return AutostartStatus(STALE, current, desired, "Der Autostart-Eintrag gehört zu einer anderen Installation "
                                   f"oder Version ({current}) – wird auf diese Installation korrigiert.", legacy)
        return AutostartStatus(ON, current, desired, "CallRecorder startet mit Windows (im Infobereich).", legacy)

    def enable(self) -> AutostartStatus:
        if self.registry is None:
            raise OSError("Autostart ist nur unter Windows verfügbar.")
        target = command_executable(self.desired())
        if not self.exists(target):
            raise OSError(f"{target} gibt es nicht – Autostart nicht eingerichtet.")
        self.registry.write(self.desired())
        self._remove_legacy()
        log.info("Autostart eingerichtet: %s", self.desired())
        return self.status()

    def disable(self) -> AutostartStatus:
        if self.registry is None:
            raise OSError("Autostart ist nur unter Windows verfügbar.")
        self.registry.delete()
        self._remove_legacy()
        log.info("Autostart entfernt")
        return self.status()

    def repair_if_stale(self) -> str:
        """At every start: fix a stale entry (moved folder, old shortcut, other arguments of this
        same program). Returns a message or "".

        An entry pointing at another *existing* program (a second installation, or this one started
        from a different interpreter for a test) is left alone: silently re-pointing it could make
        Windows start the wrong copy. The settings show it; the user decides there."""
        st = self.status()
        if st.state != STALE:
            return ""
        if st.current:
            exe = command_executable(st.current)
            if self.exists(exe) and not _same(exe, command_executable(st.desired)):
                log.info("Autostart-Eintrag gehört zu einem anderen Programm (%s) - nicht verändert", st.current)
                return ""
        try:
            self.enable()
        except OSError as exc:
            log.warning("Autostart-Eintrag nicht reparierbar: %s", exc)
            return f"Autostart-Eintrag ist veraltet und konnte nicht korrigiert werden: {exc}"
        return "Autostart-Eintrag korrigiert: " + st.detail.split(" – ")[0]

    def _remove_legacy(self) -> None:
        p = legacy_shortcut_path(self.startup_dir) if (self.startup_dir or sys.platform == "win32") else None
        if p is not None:
            try:
                p.unlink()
                log.info("Alte Autostart-Verknüpfung entfernt: %s", p)
            except OSError:
                log.warning("Alte Autostart-Verknüpfung %s nicht entfernbar", p, exc_info=True)
