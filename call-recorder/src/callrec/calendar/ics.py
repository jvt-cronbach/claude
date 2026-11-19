"""Import of meeting invitations from .ics files (offline fallback)."""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import icalendar
import recurring_ical_events

from callrec.models import CalendarEvent, Person

log = logging.getLogger(__name__)

_TEAMS_RE = re.compile(r"https://teams\.(microsoft|live)\.com/\S+", re.IGNORECASE)


def _to_aware(value: date | datetime, local_tz: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:  # "floating" time -> local time
            return value.replace(tzinfo=local_tz)
        return value
    return datetime.combine(value, time(0, 0), tzinfo=local_tz)


def _person(addr: Any, role: str) -> Person | None:
    if addr is None:
        return None
    email = str(addr)
    if email.lower().startswith("mailto:"):
        email = email[7:]
    params = getattr(addr, "params", {}) or {}
    name = str(params.get("CN", "")).strip().strip('"')
    if not name and not email:
        return None
    return Person(name=name or email, email=email.strip(), role=role)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _event_from_component(comp: Any, local_tz: Any, cancelled_calendar: bool) -> CalendarEvent | None:
    dtstart = comp.get("DTSTART")
    if dtstart is None:
        return None
    start_raw = dtstart.dt
    is_all_day = not isinstance(start_raw, datetime)
    start = _to_aware(start_raw, local_tz)
    dtend = comp.get("DTEND")
    if dtend is not None:
        end = _to_aware(dtend.dt, local_tz)
    elif comp.get("DURATION") is not None:
        end = start + comp.get("DURATION").dt
    else:
        end = start + (timedelta(days=1) if is_all_day else timedelta(hours=1))
    if str(comp.get("X-MICROSOFT-CDO-ALLDAYEVENT", "")).upper() == "TRUE":
        is_all_day = True

    organizer = _person(comp.get("ORGANIZER"), "organizer")
    attendees: list[Person] = []
    for a in _as_list(comp.get("ATTENDEE")):
        p = _person(a, "attendee")
        if p is not None:
            attendees.append(p)

    description = str(comp.get("DESCRIPTION", ""))
    location = str(comp.get("LOCATION", ""))
    join_url = str(comp.get("X-MICROSOFT-SKYPETEAMSMEETINGURL", "") or "")
    if not join_url:
        m = _TEAMS_RE.search(description) or _TEAMS_RE.search(location)
        join_url = m.group(0).rstrip(">") if m else ""
    is_online = bool(join_url) or "microsoft teams" in location.lower()

    status = str(comp.get("STATUS", "")).upper()
    recurrence_id = comp.get("RECURRENCE-ID")
    uid = str(comp.get("UID", ""))
    if recurrence_id is not None:
        uid = f"{uid}#{_to_aware(recurrence_id.dt, local_tz).isoformat()}"
    else:
        uid = f"{uid}#{start.isoformat()}"
    return CalendarEvent(
        id=uid,
        subject=str(comp.get("SUMMARY", "")).strip(),
        start=start,
        end=end,
        organizer=organizer,
        attendees=attendees,
        is_online=is_online,
        is_all_day=is_all_day,
        is_cancelled=status == "CANCELLED" or cancelled_calendar,
        show_as=str(comp.get("X-MICROSOFT-CDO-BUSYSTATUS", "")).lower(),
        source="ics",
        join_url=join_url,
    )


def parse_ics(
    data: bytes | str,
    around: datetime,
    window: timedelta = timedelta(days=1),
    local_tz: Any = None,
) -> list[CalendarEvent]:
    """Parse an .ics file and return event occurrences near ``around``.

    Recurring series are expanded. When no occurrence lies inside the window
    (e.g. a single invitation for another day), all plain events are returned
    so the caller can still pick the nearest one.
    """
    local_tz = local_tz or datetime.now().astimezone().tzinfo or UTC
    cal = icalendar.Calendar.from_ical(data)
    if isinstance(cal, list):  # multiple=True never set, but be defensive
        cal = cal[0]
    cancelled_calendar = str(cal.get("METHOD", "")).upper() == "CANCEL"

    out: list[CalendarEvent] = []
    try:
        occurrences = recurring_ical_events.of(cal, skip_bad_series=True).between(around - window, around + window)
    except Exception:  # malformed RRULEs etc. - fall back to plain events
        log.exception("Wiederholungen in .ics konnten nicht expandiert werden")
        occurrences = []
    for comp in occurrences:
        ev = _event_from_component(comp, local_tz, cancelled_calendar)
        if ev is not None:
            out.append(ev)
    if out:
        return out
    for comp in cal.walk("VEVENT"):
        ev = _event_from_component(comp, local_tz, cancelled_calendar)
        if ev is not None:
            out.append(ev)
    return out


def load_ics_file(path: Path, around: datetime, local_tz: Any = None) -> list[CalendarEvent]:
    return parse_ics(path.read_bytes(), around=around, local_tz=local_tz)
