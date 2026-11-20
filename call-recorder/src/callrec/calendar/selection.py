"""Pick the calendar event that is "running now"."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from callrec.models import CalendarEvent


def is_candidate(
    ev: CalendarEvent, now: datetime, tolerance_before: timedelta, tolerance_after: timedelta
) -> bool:
    if ev.is_cancelled or ev.is_all_day or ev.declined:
        return False
    if ev.show_as.lower() == "free" and not ev.is_online:
        # "free" blockers (e.g. focus time reminders) are rarely the call
        return False
    return ev.start - tolerance_before <= now <= ev.end + tolerance_after


def _rank(ev: CalendarEvent, now: datetime) -> tuple[int, int, float, float, int, str]:
    running = ev.start <= now <= ev.end
    # Distance between "now" and the event start: the meeting that started most
    # recently (or is about to start) is the one being joined.
    start_distance = abs((now - ev.start).total_seconds())
    duration = (ev.end - ev.start).total_seconds()
    return (
        0 if ev.is_online else 1,
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
    """Return the most plausible current meeting.

    Rules, in order:
      1. ignore cancelled, declined, all-day and "free" non-online entries
      2. event window extended by the tolerances must contain ``now``
      3. prefer online meetings (Teams)
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


def candidate_events(
    events: Iterable[CalendarEvent],
    now: datetime,
    tolerance_before: timedelta = timedelta(minutes=10),
    tolerance_after: timedelta = timedelta(minutes=10),
) -> list[CalendarEvent]:
    """All plausible current meetings, best first (the first is what select_current_event picks)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    seen: set[str] = set()
    out = []
    for e in sorted((e for e in events if is_candidate(e, now, tolerance_before, tolerance_after)),
                    key=lambda e: _rank(e, now)):
        if e.id in seen:
            continue
        seen.add(e.id)
        out.append(e)
    return out


def nearest_event(events: Iterable[CalendarEvent], now: datetime) -> CalendarEvent | None:
    """Fallback for .ics imports: the event whose start is closest to now."""
    usable = [e for e in events if not e.is_cancelled]
    if not usable:
        return None
    return min(usable, key=lambda e: (abs((e.start - now).total_seconds()), e.subject))
