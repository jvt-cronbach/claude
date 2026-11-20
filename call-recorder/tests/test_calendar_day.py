"""Event picker: all of today's events, blockers demoted, time zones, paging, midnight."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from callrec.calendar.graph import GraphClient, parse_graph_event
from callrec.calendar.selection import (
    candidate_events,
    demotion,
    events_for_recording,
    local_day,
    select_current_event,
)
from callrec.calendar.service import CalendarService
from callrec.models import Person
from tests.conftest import make_event

BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 19, 14, 5, tzinfo=BERLIN)
T = timedelta(minutes=10)


def _svc(tmp_path: Path, events, now=NOW, calls=None) -> CalendarService:
    class Src:
        def calendar_view(self, a, b):
            if calls is not None:
                calls.append((a, b))
            return [e for e in events if e.end >= a and e.start <= b]

        def get_me(self):
            raise NotImplementedError

    return CalendarService(Src(), tmp_path / "c.json", T, T, clock=lambda: now, local_tz=BERLIN)


def test_blocker_and_all_day_do_not_win_over_a_running_meeting(tmp_path: Path):
    travel = make_event("Rückreise", NOW.replace(hour=8, minute=0), minutes=10 * 60, attendees=[], organizer=None,
                        show_as="oof", id="travel")
    allday = make_event("Messe", NOW.replace(hour=0, minute=0), minutes=24 * 60, is_all_day=True, id="allday")
    meeting = make_event("Jour fixe", NOW.replace(hour=14, minute=0), minutes=30, is_online=True, id="jf")
    later = make_event("Kunde", NOW.replace(hour=16, minute=0), minutes=60, id="kunde")
    earlier = make_event("Morgenrunde", NOW.replace(hour=9, minute=0), minutes=30, id="früh")
    assert demotion(travel) and demotion(allday) and not demotion(meeting)
    events, msg, source = _svc(tmp_path, [travel, allday, later, earlier, meeting]).candidates()
    assert [e.id for e in events][:3] == ["jf", "kunde", "früh"] and {e.id for e in events[3:]} == {"travel", "allday"}
    assert select_current_event([travel, allday, meeting], NOW) is meeting
    assert select_current_event([travel, allday], NOW) is None  # a blocker is never chosen automatically
    assert _svc(tmp_path, [travel, meeting]).suggest().event is meeting


def test_several_meetings_listed_and_nothing_running(tmp_path: Path):
    evs = [make_event(f"M{h}", NOW.replace(hour=h, minute=0), minutes=45, id=f"m{h}") for h in (8, 10, 16, 18)]
    svc = _svc(tmp_path, evs)
    events, _msg, _src = svc.candidates()
    assert [e.id for e in events] == ["m16", "m10", "m18", "m8"]  # by distance to 14:05
    assert not svc.is_current(events[0])


def test_today_window_is_local_midnight_and_dst():
    lo, hi = local_day(datetime(2026, 10, 25, 12, 0, tzinfo=UTC), BERLIN)  # DST ends this night
    assert lo.isoformat() == "2026-10-25T00:00:00+02:00" and hi.isoformat() == "2026-10-26T00:00:00+01:00"
    assert hi.astimezone(UTC) - lo.astimezone(UTC) == timedelta(hours=25)
    # 23:30 UTC on 19.11. is already the 20th in Berlin
    lo, _hi = local_day(datetime(2026, 11, 19, 23, 30, tzinfo=UTC), BERLIN)
    assert lo.date().isoformat() == "2026-11-20"


def test_event_in_utc_shown_for_berlin_and_query_covers_the_local_day(tmp_path: Path):
    utc_event = make_event("Call", datetime(2026, 11, 19, 13, 0, tzinfo=UTC), minutes=60, is_online=True, id="u")
    calls: list = []
    events, _m, _s = _svc(tmp_path, [utc_event], calls=calls).candidates()
    assert events == [utc_event] and select_current_event(events, NOW) is utc_event  # 14:00-15:00 Berlin
    a, b = calls[-1]
    assert a == datetime(2026, 11, 19, 0, 0, tzinfo=BERLIN) and b == datetime(2026, 11, 20, 0, 0, tzinfo=BERLIN)


def test_graph_paging_follows_next_link():
    item = {"id": "x", "subject": "S", "start": {"dateTime": "2026-11-19T13:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-11-19T14:00:00", "timeZone": "UTC"}}
    pages = [{"value": [dict(item, id=f"a{i}") for i in range(50)], "@odata.nextLink": "https://graph/next"},
             {"value": [dict(item, id="b")]}]
    seen: list = []

    class Resp:
        status_code = 200

        def __init__(self, data):
            self._d = data
            self.text = ""

        def json(self):
            return self._d

    class Sess:
        def get(self, url, params=None, headers=None, timeout=None):
            seen.append((url, params, headers))
            return Resp(pages[len(seen) - 1])

    events = GraphClient(lambda: "t", session=Sess()).calendar_view(NOW - timedelta(hours=14), NOW)  # type: ignore[arg-type]
    assert len(events) == 51 and seen[1][0] == "https://graph/next" and seen[1][1] is None
    assert seen[0][2]["Prefer"] == 'outlook.timezone="UTC"'


def test_all_day_graph_event_is_the_local_date():
    item = {"id": "d", "subject": "Urlaub", "isAllDay": True,
            "start": {"dateTime": "2026-11-19T00:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-11-20T00:00:00.0000000", "timeZone": "UTC"}}
    ev = parse_graph_event(item)
    assert ev.is_all_day and ev.start.replace(tzinfo=None) == datetime(2026, 11, 19)  # local midnight
    assert demotion(ev) == "ganztägig"


def test_recording_across_midnight_offers_both_days(tmp_path: Path):
    start = datetime(2026, 11, 19, 23, 40, tzinfo=BERLIN)
    end = start + timedelta(minutes=50)
    late = make_event("Spätcall", datetime(2026, 11, 19, 23, 30, tzinfo=BERLIN), minutes=60, is_online=True, id="late")
    morning = make_event("Frühstück", datetime(2026, 11, 20, 8, 0, tzinfo=BERLIN), minutes=30, id="next")
    noon = make_event("Mittag", datetime(2026, 11, 19, 12, 0, tzinfo=BERLIN), minutes=30, id="noon")
    blocker = make_event("Nachtschicht", datetime(2026, 11, 19, 22, 0, tzinfo=BERLIN), minutes=8 * 60, attendees=[],
                         organizer=None, id="blk")
    calls: list = []
    svc = _svc(tmp_path, [noon, morning, blocker, late], calls=calls)
    found = svc.events_for_recording(start, end)
    assert [e.id for e in found] == ["late", "next", "noon", "blk"]
    a, b = calls[-1]
    assert a <= datetime(2026, 11, 19, 0, 0, tzinfo=BERLIN) and b >= datetime(2026, 11, 21, 0, 0, tzinfo=BERLIN)
    assert events_for_recording([blocker, late], start, end)[0] is late


def test_candidate_order_running_online_first():
    a = make_event("Offline läuft", NOW - timedelta(minutes=5), minutes=30, id="a")
    b = make_event("Teams läuft", NOW - timedelta(minutes=5), minutes=30, is_online=True, id="b")
    c = make_event("Teams später", NOW + timedelta(hours=2), minutes=30, is_online=True, id="c",
                   attendees=[Person("X")])
    assert [e.id for e in candidate_events([a, c, b], NOW)] == ["b", "a", "c"]
