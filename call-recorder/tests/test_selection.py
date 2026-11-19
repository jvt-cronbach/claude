from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from callrec.calendar.selection import nearest_event, select_current_event
from tests.conftest import BERLIN, make_event

NOW = datetime(2026, 11, 19, 14, 5, tzinfo=BERLIN)


def at(h: int, m: int = 0):
    return datetime(2026, 11, 19, h, m, tzinfo=BERLIN)


def test_running_event_selected():
    ev = make_event("Jour fixe", at(14), 60)
    assert select_current_event([ev], NOW) is ev


def test_none_when_nothing_close():
    assert select_current_event([make_event("Später", at(16))], NOW) is None
    assert select_current_event([], NOW) is None


def test_tolerance_before_and_after():
    soon = make_event("Gleich", at(14, 12), 30)
    assert select_current_event([soon], NOW) is soon
    assert select_current_event([soon], NOW, tolerance_before=timedelta(minutes=5)) is None
    ended = make_event("Vorbei", at(13, 0), 60)
    assert select_current_event([ended], NOW) is ended  # ended 5 min ago
    assert select_current_event([ended], NOW, tolerance_after=timedelta(minutes=2)) is None


def test_prefer_online():
    offline = make_event("Büro", at(14), 60, is_online=False)
    online = make_event("Teams", at(14), 60, is_online=True)
    assert select_current_event([offline, online], NOW) is online


def test_prefer_running_over_upcoming():
    running = make_event("Läuft", at(13, 30), 60, is_online=True)
    upcoming = make_event("Gleich", at(14, 10), 30, is_online=True)
    assert select_current_event([upcoming, running], NOW) is running


def test_prefer_latest_start_then_shorter():
    long_block = make_event("Workshop-Tag", at(9), 9 * 60, is_online=True)
    meeting = make_event("Abstimmung", at(14), 30, is_online=True)
    assert select_current_event([long_block, meeting], NOW) is meeting
    a = make_event("Lang", at(14), 120, is_online=True)
    b = make_event("Kurz", at(14), 30, is_online=True)
    assert select_current_event([a, b], NOW) is b


def test_excluded_kinds():
    cancelled = make_event("Abgesagt", at(14), is_online=True, is_cancelled=True)
    declined = make_event("Abgelehnt", at(14), is_online=True, declined=True)
    allday = make_event("Urlaub", at(0), 24 * 60, is_all_day=True)
    free = make_event("Fokuszeit", at(14), 60, show_as="free")
    assert select_current_event([cancelled, declined, allday, free], NOW) is None


def test_tie_break_deterministic():
    a = make_event("B-Termin", at(14), 60, is_online=True)
    b = make_event("A-Termin", at(14), 60, is_online=True)
    assert select_current_event([a, b], NOW).subject == "A-Termin"
    assert select_current_event([b, a], NOW).subject == "A-Termin"


def test_naive_now_rejected():
    with pytest.raises(ValueError):
        select_current_event([], datetime(2026, 11, 19, 14))


def test_nearest():
    a = make_event("Morgen", at(14) + timedelta(days=1))
    b = make_event("Gestern", at(14) - timedelta(days=2))
    assert nearest_event([a, b], NOW) is a
