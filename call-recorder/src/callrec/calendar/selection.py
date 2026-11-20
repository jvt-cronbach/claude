"""Pick the calendar event that is "running now"."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, tzinfo
from typing import Any

from callrec.models import CalendarEvent

# show_as values that are not a meeting: out of office, working elsewhere, free time
_NOT_MEETING_SHOW_AS = ("free", "oof", "workingelsewhere")
BLOCKER_MIN_HOURS = 4.0  # a long entry without other people is a blocker ("Rückreise", "Fokuszeit")


def demotion(ev: CalendarEvent) -> str:
    """Why ``ev`` is probably not the call ("" = a real meeting). Such entries are listed last,
    marked, and never chosen automatically."""
    if ev.is_cancelled:
        return "abgesagt"
    if ev.declined:
        return "abgelehnt"
    if ev.is_all_day:
        return "ganztägig"
    hours = (ev.end - ev.start).total_seconds() / 3600
    if hours >= 20:
        return "mehrtägig"
    show = ev.show_as.lower()
    if show in _NOT_MEETING_SHOW_AS and not ev.is_online:
        return {"free": "als frei eingetragen", "oof": "abwesend"}.get(show, "woanders tätig")
    if hours >= BLOCKER_MIN_HOURS and not ev.is_online and not ev.attendees:
        return "Blocker (lang, ohne Teilnehmende)"
    return ""


def is_candidate(
    ev: CalendarEvent, now: datetime, tolerance_before: timedelta, tolerance_after: timedelta
) -> bool:
    """A real meeting (see :func:`demotion`) running now or within the tolerances."""
    if demotion(ev):
        return False
    return ev.start - tolerance_before <= now <= ev.end + tolerance_after


def _distance(ev: CalendarEvent, now: datetime) -> float:
    if ev.start <= now <= ev.end:
        return 0.0
    return min(abs((ev.start - now).total_seconds()), abs((now - ev.end).total_seconds()))


def _with_others(ev: CalendarEvent) -> bool:
    """Invites somebody (an entry only for yourself - "Rückreise", "Fokuszeit" - does not)."""
    return bool(ev.attendees)


def _rank(ev: CalendarEvent, now: datetime) -> tuple[int, int, int, float, float, int, str]:
    running = ev.start <= now <= ev.end
    # Distance between "now" and the event start: the meeting that started most
    # recently (or is about to start) is the one being joined.
    start_distance = abs((now - ev.start).total_seconds())
    duration = (ev.end - ev.start).total_seconds()
    return (
        0 if ev.is_online else 1,
        # a meeting with invitees beats a short personal entry that happens to be running
        0 if _with_others(ev) else 1,
        0 if running else 1,
        start_distance,
        duration,  # shorter (more specific) wins over long blockers
        -len(ev.attendees),
        ev.subject.lower(),
    )


def select_current_event(
    events: Iterable[CalendarEvent],
    now: datetime,
    tolerance_before: timedelta = timedelta(minutes=10),
    tolerance_after: timedelta = timedelta(minutes=10),
) -> CalendarEvent | None:
    """Return the most plausible current meeting (also names a new recording).

    Rules, in order:
      1. ignore cancelled, declined, all-day/multi-day, free/out-of-office and long blockers
         without participants (:func:`demotion`) - a blocker never wins over a real meeting
      2. event window extended by the tolerances must contain ``now``
      3. prefer online meetings (Teams), then meetings with invitees over entries only for you
      4. prefer meetings actually running over ones about to start / just ended
      5. prefer the one whose start is closest to ``now``
      6. prefer shorter meetings, then more attendees, then subject (stable)
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    candidates = [e for e in events if is_candidate(e, now, tolerance_before, tolerance_after)]
    if not candidates:
        return None
    return min(candidates, key=lambda e: _rank(e, now))


def _dedupe(events: Iterable[CalendarEvent]) -> list[CalendarEvent]:
    seen: set[str] = set()
    out = []
    for e in events:
        key = e.id or f"{e.subject}|{e.start.isoformat()}"
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def candidate_events(
    events: Iterable[CalendarEvent],
    now: datetime,
    tolerance_before: timedelta = timedelta(minutes=10),
    tolerance_after: timedelta = timedelta(minutes=10),
) -> list[CalendarEvent]:
    """ALL events passed in (normally: today's), best first, for the event picker.

    Order: real meetings running now or within the tolerances (as :func:`select_current_event`,
    so the first is the one it picks), then the other real meetings by distance to now (online
    first), then the demoted entries (all-day, blockers, cancelled, declined ...)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    def key(e: CalendarEvent) -> tuple[int, int, tuple[Any, ...]]:
        if demotion(e):
            return (2, 0, (_distance(e, now), e.subject.lower()))
        if is_candidate(e, now, tolerance_before, tolerance_after):
            return (0, 0, _rank(e, now))
        return (1, 0 if e.is_online else 1, (_distance(e, now), e.subject.lower()))

    return sorted(_dedupe(events), key=key)


def local_day(moment: datetime, tz: tzinfo | None = None) -> tuple[datetime, datetime]:
    """Local midnight to midnight of the day of ``moment``. With a ZoneInfo zone the offsets are
    those of each midnight, so DST days have 23/25 hours."""
    local = moment.astimezone(tz) if tz is not None else moment.astimezone()
    day = local.date()
    nxt = day + timedelta(days=1)
    if tz is None:
        # the PC's zone: naive local midnights, each converted with its own offset (astimezone()
        # alone yields the fixed offset of ``moment``, one hour off at midnight on DST days)
        return (datetime(day.year, day.month, day.day).astimezone(),
                datetime(nxt.year, nxt.month, nxt.day).astimezone())
    zone = local.tzinfo
    return datetime(day.year, day.month, day.day, tzinfo=zone), datetime(nxt.year, nxt.month, nxt.day, tzinfo=zone)


def nearest_event(events: Iterable[CalendarEvent], now: datetime) -> CalendarEvent | None:
    """Fallback for .ics imports: the event whose start is closest to now."""
    usable = [e for e in events if not e.is_cancelled]
    if not usable:
        return None
    return min(usable, key=lambda e: (abs((e.start - now).total_seconds()), e.subject))


def events_for_recording(
    events: Iterable[CalendarEvent], start: datetime, end: datetime, margin: timedelta = timedelta(minutes=15)
) -> list[CalendarEvent]:
    """Events for a recording made from ``start`` to ``end``, best first ("Termin zuordnen").

    ``events`` are normally all events of the recording's date(s). Ranked by how well event and
    recording coincide (overlap divided by the combined span, so a long blocker loses against the
    actual call), then online meetings, then the closest start; events without overlap follow by
    distance, and demoted entries (all-day, blockers, cancelled ...) come last."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start/end must be timezone-aware")
    lo, hi = start - margin, end + margin
    scored: list[tuple[int, float, int, float, str, CalendarEvent]] = []
    for e in _dedupe(events):
        overlap = max(0.0, (min(e.end, hi) - max(e.start, lo)).total_seconds())
        union = max(1.0, (max(e.end, end) - min(e.start, start)).total_seconds())
        group = 2 if demotion(e) else (0 if overlap > 0 else 1)
        dist = 0.0 if overlap > 0 else min(abs((e.start - end).total_seconds()), abs((start - e.end).total_seconds()))
        scored.append((group, -overlap / union if overlap > 0 else dist, 0 if e.is_online else 1,
                       abs((e.start - start).total_seconds()), e.subject.lower(), e))
    scored.sort(key=lambda x: x[:5])
    return [x[5] for x in scored]
