"""Pure helpers for matching Windows endpoint names to PortAudio devices."""

from __future__ import annotations

import re

from callrec.audio.capture import DeviceSpec

_LOOPBACK_SUFFIX = re.compile(r"\s*\[loopback\]\s*$", re.IGNORECASE)


def base_name(name: str) -> str:
    return _LOOPBACK_SUFFIX.sub("", name).strip()


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", base_name(name)).casefold()


def match_device(devices: list[DeviceSpec], wanted: str) -> DeviceSpec | None:
    """Find the device whose (loopback-suffix stripped) name matches ``wanted``.

    Order: exact match, then prefix match (PortAudio may truncate long names),
    then substring match. Ties are resolved by list order.
    """
    if not wanted:
        return None
    w = _norm(wanted)
    for d in devices:
        if _norm(d.name) == w:
            return d
    for d in devices:
        n = _norm(d.name)
        if n and (w.startswith(n) or n.startswith(w)):
            return d
    for d in devices:
        if w in _norm(d.name):
            return d
    return None


def describe(d: DeviceSpec) -> str:
    return f"{base_name(d.name)} ({d.sample_rate} Hz, {d.channels} Kanal/Kanäle)"
