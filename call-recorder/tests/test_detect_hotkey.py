from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from callrec.detect import CallDetector
from callrec.hotkey import MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, MOD_SHIFT, MOD_WIN, HotkeyError, parse_hotkey
from tests.conftest import make_event

T0 = datetime(2026, 11, 19, 13, 0, tzinfo=UTC)


def t(sec: float) -> datetime:
    return T0 + timedelta(seconds=sec)


def test_teams_rising_edge_debounced_and_cooldown():
    d = CallDetector(cooldown=timedelta(minutes=10), confirm_polls=2)
    assert d.update(t(0), True, False) is None  # first poll: not yet confirmed
    p = d.update(t(3), True, False)
    assert p is not None and p.reason == "teams" and "Teams" in p.message()
    assert d.update(t(6), True, False) is None  # still the same call
    d.update(t(9), False, False)
    d.update(t(12), True, False)
    assert d.update(t(15), True, False) is None  # new call but within cooldown
    d.update(t(700), False, False)
    d.update(t(703), True, False)
    assert d.update(t(706), True, False) is not None


def test_never_prompts_while_recording():
    d = CallDetector(confirm_polls=1)
    assert d.update(t(0), True, recording=True) is None
    assert d.update(t(3), True, recording=False) is None  # same call continues after recording


def test_calendar_prompt_once_per_event():
    ev = make_event("Teams-Termin", T0 + timedelta(seconds=30), is_online=True, id="ev1")
    offline = make_event("Vor Ort", T0, is_online=False, id="ev2")
    d = CallDetector(confirm_polls=2)
    assert d.update(t(-300), False, False, [ev, offline]) is None  # too early
    p = d.update(t(0), False, False, [ev, offline])
    assert p is not None and p.reason == "calendar" and "Teams-Termin" in p.message()
    d2 = CallDetector(cooldown=timedelta(0))
    d2.update(t(0), False, False, [ev])
    assert d2.update(t(10), False, False, [ev]) is None  # already prompted for this event


def test_snooze():
    d = CallDetector(confirm_polls=1, cooldown=timedelta(minutes=5))
    d.snooze(t(0))
    assert d.update(t(10), True, False) is None


@pytest.mark.parametrize(
    "text, mods, vk",
    [
        ("ctrl+alt+shift+r", MOD_CONTROL | MOD_ALT | MOD_SHIFT, ord("R")),
        ("Strg+Umschalt+F9", MOD_CONTROL | MOD_SHIFT, 0x78),
        ("win + alt + 1", MOD_WIN | MOD_ALT, ord("1")),
        ("F13", 0, 0x7C),
        ("ctrl+pause", MOD_CONTROL, 0x13),
    ],
)
def test_parse_hotkey(text, mods, vk):
    hk = parse_hotkey(text)
    assert hk.modifiers == mods | MOD_NOREPEAT and hk.vk == vk


@pytest.mark.parametrize("text", ["", "r", "ctrl+", "hyper+r", "ctrl+ä", "ctrl+f25"])
def test_parse_hotkey_invalid(text):
    with pytest.raises(HotkeyError):
        parse_hotkey(text)
