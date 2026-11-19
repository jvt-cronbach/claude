from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from callrec.calendar.ics import load_ics_file, parse_ics
from callrec.calendar.selection import select_current_event
from callrec.calendar.service import CalendarService

# Shape of an invitation exported from Outlook (Windows time zone name + VTIMEZONE).
OUTLOOK_ICS = """BEGIN:VCALENDAR
METHOD:REQUEST
PRODID:Microsoft Exchange Server 2010
VERSION:2.0
BEGIN:VTIMEZONE
TZID:W. Europe Standard Time
BEGIN:STANDARD
DTSTART:16010101T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=10
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:16010101T020000
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=-1SU;BYMONTH=3
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
ORGANIZER;CN=Olga Organisatorin:mailto:olga@firma.de
ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE;CN=Anna Beispiel:mailto:anna@firma.de
ATTENDEE;ROLE=OPT-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE;CN="Müller, Bernd":mailto:bernd.mueller@firma.de
DESCRIPTION;LANGUAGE=de-DE:Microsoft Teams-Besprechung\\nJetzt teilnehmen <https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context=x>
UID:040000008200E00074C5B7101A82E0080000000012345
SUMMARY;LANGUAGE=de-DE:Projekt Jour fixe
DTSTART;TZID=W. Europe Standard Time:20261119T140000
DTEND;TZID=W. Europe Standard Time:20261119T150000
LOCATION;LANGUAGE=de-DE:Microsoft Teams-Besprechung
X-MICROSOFT-CDO-BUSYSTATUS:TENTATIVE
END:VEVENT
END:VCALENDAR
"""

WEEKLY_ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:weekly-1
SUMMARY:Weekly Sync
DTSTART:20261001T130000Z
DTEND:20261001T133000Z
RRULE:FREQ=WEEKLY;BYDAY=TH
ORGANIZER;CN=Olga:mailto:olga@firma.de
ATTENDEE;CN=Anna:mailto:anna@firma.de
END:VEVENT
END:VCALENDAR
"""

CANCEL_ICS = """BEGIN:VCALENDAR
VERSION:2.0
METHOD:CANCEL
BEGIN:VEVENT
UID:x
SUMMARY:Abgesagt: Termin
DTSTART:20261119T130000Z
DTEND:20261119T140000Z
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""

NOW = datetime(2026, 11, 19, 13, 5, tzinfo=UTC)  # 14:05 in Berlin


def test_outlook_invitation():
    events = parse_ics(OUTLOOK_ICS, around=NOW)
    assert len(events) == 1
    ev = events[0]
    assert ev.subject == "Projekt Jour fixe"
    assert ev.start == datetime(2026, 11, 19, 13, 0, tzinfo=UTC)
    assert ev.end - ev.start == timedelta(hours=1)
    assert ev.is_online
    assert ev.join_url.startswith("https://teams.microsoft.com/l/meetup-join/")
    assert ev.organizer is not None and ev.organizer.email == "olga@firma.de"
    names = [p.name for p in ev.participants()]
    assert names == ["Olga Organisatorin", "Anna Beispiel", "Müller, Bernd"]
    assert ev.source == "ics"
    assert select_current_event(events, NOW) is ev


def test_recurring_series_expanded():
    events = parse_ics(WEEKLY_ICS, around=NOW)
    assert [e.start for e in events] == [datetime(2026, 11, 19, 13, 0, tzinfo=UTC)]
    assert select_current_event(events, NOW) is not None


def test_far_away_event_still_returned_for_nearest():
    far = NOW + timedelta(days=30)
    events = parse_ics(OUTLOOK_ICS, around=far)
    assert len(events) == 1  # fallback to plain events


def test_cancelled():
    events = parse_ics(CANCEL_ICS, around=NOW)
    assert events and all(e.is_cancelled for e in events)
    assert select_current_event(events, NOW) is None


def test_service_ics_fallback(tmp_path: Path):
    p = tmp_path / "einladung.ics"
    p.write_text(OUTLOOK_ICS, encoding="utf-8")
    assert load_ics_file(p, NOW)[0].subject == "Projekt Jour fixe"
    svc = CalendarService(None, tmp_path / "cache.json", timedelta(minutes=10), timedelta(minutes=10), clock=lambda: NOW + timedelta(days=3))
    s = svc.suggest_from_ics(p)
    assert s.event is not None and s.source == "ics"
    assert len(s.participants) == 3
