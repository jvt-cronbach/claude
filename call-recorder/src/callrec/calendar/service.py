"""Combines Graph, an on-disk cache of recent events and .ics imports."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Protocol

from callrec.calendar.ics import load_ics_file
from callrec.calendar.selection import (
    candidate_events,
    demotion,
    events_for_recording,
    local_day,
    nearest_event,
    select_current_event,
)
from callrec.models import CalendarEvent, Person

log = logging.getLogger(__name__)


class CalendarSource(Protocol):
    def calendar_view(self, start: datetime, end: datetime) -> list[CalendarEvent]: ...

    def get_me(self) -> Person: ...


@dataclass
class Suggestion:
    event: CalendarEvent | None
    participants: list[Person] = field(default_factory=list)
    message: str = ""  # info for the UI, e.g. why Graph was not used
    source: str = "manual"  # graph | cache | ics | manual


class CalendarService:
    def __init__(
        self,
        source: CalendarSource | None,
        cache_file: Path,
        tolerance_before: timedelta,
        tolerance_after: timedelta,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        local_tz: tzinfo | None = None,
    ) -> None:
        self.local_tz = local_tz  # None = the PC's zone (tests pass Europe/Berlin)
        self.source = source
        self.cache_file = cache_file
        self.tol_before = tolerance_before
        self.tol_after = tolerance_after
        self.clock = clock

    # -- cache ---------------------------------------------------------
    def _save_cache(self, events: list[CalendarEvent]) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps([e.to_dict() for e in events], ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.cache_file)
        except OSError:
            log.exception("Kalender-Cache konnte nicht geschrieben werden")

    def _load_cache(self) -> list[CalendarEvent]:
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            return [CalendarEvent.from_dict(d) for d in data]
        except (OSError, ValueError, KeyError):
            return []

    # -- queries -------------------------------------------------------
    def fetch_window(self, hours_back: float = 4, hours_ahead: float = 12) -> list[CalendarEvent]:
        """Fetch events around now from Graph and refresh the cache (raises on failure)."""
        if self.source is None:
            raise RuntimeError("Kein Kalender konfiguriert")
        now = self.clock()
        events = self.source.calendar_view(now - timedelta(hours=hours_back), now + timedelta(hours=hours_ahead))
        self._save_cache(events)
        return events

    def fetch_day(self) -> list[CalendarEvent]:
        """All events of today (local midnight to midnight; around midnight widened by the
        tolerances), refreshes the cache (raises on failure)."""
        if self.source is None:
            raise RuntimeError("Kein Kalender konfiguriert")
        now = self.clock()
        lo, hi = local_day(now, self.local_tz)
        lo, hi = min(lo, now - self.tol_after), max(hi, now + self.tol_before)
        events = self.source.calendar_view(lo, hi)
        self._log_events(events, now)
        self._save_cache(events)
        return events

    def _log_events(self, events: list[CalendarEvent], now: datetime) -> None:
        log.info("Kalender: %d Termin(e) heute", len(events))
        for e in events:
            why = demotion(e)
            if why:
                log.info("Termin %r (%s–%s) nachrangig: %s", e.subject, e.start.astimezone(self.local_tz),
                         e.end.astimezone(self.local_tz), why)

    def suggest(self) -> Suggestion:
        now = self.clock()
        message = ""
        if self.source is not None:
            try:
                events = self.fetch_day()
                ev = select_current_event(events, now, self.tol_before, self.tol_after)
                if ev:
                    return Suggestion(ev, ev.participants(), source="graph")
                return Suggestion(None, [], "Kein laufender Termin im Kalender gefunden.", source="graph")
            except Exception as exc:  # offline, not signed in, ...
                log.info("Kalender nicht verfügbar: %s", exc)
                message = f"Kalender nicht verfügbar ({exc}). "
            cached = self._load_cache()
            ev = select_current_event(cached, now, self.tol_before, self.tol_after)
            if ev:
                return Suggestion(ev, ev.participants(), message + "Termin aus dem Zwischenspeicher.", "cache")
        return Suggestion(None, [], (message + "Bitte Titel und Teilnehmende eingeben oder .ics importieren.").strip())

    def candidates(self) -> tuple[list[CalendarEvent], str, str]:
        """(all of today's events best first, info text, source) for the event picker of the main window.

        Running/starting meetings first (the same choice as :meth:`suggest`), then the others of the
        day by distance, all-day entries and blockers last; falls back to the cache when offline."""
        now = self.clock()
        if self.source is None:
            return [], "Kein Microsoft-Kalender verbunden (optional) – Titel und Teilnehmende beim Start eingeben.", "manual"
        lo, hi = local_day(now, self.local_tz)
        try:
            events = self.fetch_day()
            found = candidate_events(events, now, self.tol_before, self.tol_after)
            return found, "" if found else "Heute stehen keine Termine im Kalender.", "graph"
        except Exception as exc:  # offline, not signed in, ...
            log.info("Kalender nicht verfügbar: %s", exc)
            cached = [e for e in self._load_cache() if e.end >= lo and e.start <= hi]
            found = candidate_events(cached, now, self.tol_before, self.tol_after)
            return found, f"Kalender nicht verfügbar ({exc})." + (" Termine aus dem Zwischenspeicher." if found else ""), \
                "cache"

    def is_current(self, ev: CalendarEvent) -> bool:
        """Running now / within the tolerances and a real meeting (may be pre-selected)."""
        from callrec.calendar.selection import is_candidate

        return is_candidate(ev, self.clock(), self.tol_before, self.tol_after)

    def suggest_from_ics(self, path: Path) -> Suggestion:
        now = self.clock()
        events = load_ics_file(path, around=now)
        ev = select_current_event(events, now, self.tol_before, self.tol_after) or nearest_event(events, now)
        if ev is None:
            return Suggestion(None, [], "Die .ics-Datei enthält keinen Termin.", "ics")
        return Suggestion(ev, ev.participants(), f"Termin aus {path.name}", "ics")

    def upcoming_online_meetings(self, horizon: timedelta = timedelta(hours=2)) -> list[CalendarEvent]:
        """Events for the calendar-based reminder; uses the cache when offline."""
        try:
            events = self.fetch_window(hours_back=1, hours_ahead=horizon.total_seconds() / 3600)
        except Exception:
            events = self._load_cache()
        return [e for e in events if e.is_online and not e.is_cancelled and not e.declined]

    def events_for_recording(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """Events around an (earlier) recording, best first - for "Termin zuordnen" (raises if offline)."""
        if self.source is None:
            raise RuntimeError("Kein Microsoft-Kalender verbunden – erst unter „Einrichtung“ anmelden.")
        margin = timedelta(minutes=15)
        # every event of the recording's date (and of the next day when it ran past midnight)
        lo, _ = local_day(start, self.local_tz)
        _, hi = local_day(end, self.local_tz)
        events = self.source.calendar_view(min(lo, start - margin), max(hi, end + margin))
        self._log_events(events, start)
        return events_for_recording(events, start, end, margin)

    def preview(self) -> tuple[CalendarEvent | None, str]:
        """(current or next event within 12 h, German one-line description) for the setup checklist."""
        events = self.fetch_window(hours_back=0.5, hours_ahead=12)
        now = self.clock()
        ev = select_current_event(events, now, self.tol_before, self.tol_after)
        label = "Aktueller Termin"
        if ev is None:
            future = sorted((e for e in events if e.start > now and not e.is_cancelled and not e.is_all_day
                             and not e.declined), key=lambda e: e.start)
            ev = future[0] if future else None
            label = "Nächster Termin"
        if ev is None:
            return None, "Verbindung zum Kalender klappt – in den nächsten 12 Stunden steht kein Termin an."
        people = [p.display for p in ev.participants()]
        shown = ", ".join(people[:6]) + (f" und {len(people) - 6} weitere" if len(people) > 6 else "")
        return ev, (f"{label}: {ev.subject} ({ev.start.astimezone():%d.%m. %H:%M}–{ev.end.astimezone():%H:%M})"
                    + (" – Teams" if ev.is_online else "") + f"\nTeilnehmende: {shown or '–'}")
