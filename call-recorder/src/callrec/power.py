"""Battery and energy-saver state: both can make the processing much slower (Windows throttles the CPU).

Windows: ``GetSystemPowerStatus`` (ACLineStatus 0 = on battery; SystemStatusFlag 1 = energy saver
on). Elsewhere psutil's battery sensor (no energy-saver information).
"""

from __future__ import annotations

import ctypes
import sys
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class PowerState:
    on_battery: bool | None = None  # None = unknown / no battery
    saver: bool | None = None
    percent: int | None = None

    @property
    def slow(self) -> bool:
        return bool(self.on_battery or self.saver)

    def to_dict(self) -> dict[str, object]:
        return {"on_battery": self.on_battery, "saver": self.saver, "percent": self.percent}

    def warning(self) -> str:
        """German hint for the UI ("" when nothing slows the PC down)."""
        if self.saver:
            return ("Energiesparmodus ist an – die Verarbeitung kann etwa halb so schnell sein. Ausschalten: "
                    "Windows-Einstellungen › System › Strom und Akku › Energiesparmodus.")
        if self.on_battery:
            return ("Akkubetrieb – Windows drosselt den Prozessor, die Verarbeitung kann deutlich langsamer sein. "
                    "Mit Netzteil geht es schneller.")
        return ""


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def from_windows_status(ac_line: int, battery_flag: int, percent: int, status_flag: int) -> PowerState:
    """Pure mapping of SYSTEM_POWER_STATUS (testable without Windows)."""
    no_battery = battery_flag == 128 or battery_flag == 255
    on_battery = None if ac_line == 255 or no_battery else ac_line == 0
    return PowerState(on_battery, status_flag == 1, None if percent == 255 or no_battery else int(percent))


def _windows() -> PowerState:
    st = _SystemPowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):  # type: ignore[attr-defined]
        return PowerState()
    return from_windows_status(st.ACLineStatus, st.BatteryFlag, st.BatteryLifePercent, st.SystemStatusFlag)


def _psutil() -> PowerState:
    import psutil

    b = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if b is None:
        return PowerState()
    return PowerState(None if b.power_plugged is None else not b.power_plugged, None, int(b.percent))


def power_state(reader: Callable[[], PowerState] | None = None) -> PowerState:
    try:
        if reader is not None:
            return reader()
        return _windows() if sys.platform == "win32" else _psutil()
    except Exception:  # noqa: BLE001 - only a hint, never an error
        return PowerState()
