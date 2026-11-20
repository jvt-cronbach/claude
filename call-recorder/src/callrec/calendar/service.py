"""Combines Graph, an on-disk cache of recent events and .ics imports."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from callrec.calendar.ics import load_ics_file
from callrec.calendar.selection import (
    candidate_events,
    events_for_recording,
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
    ) -> None:
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

    def suggest(self) -> Suggestion:
        now = self.clock()
        message = ""
        if self.source is not None:
            try:
                events = self.fetch_window()
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
        """(current meetings best first, info text, source) for the event picker of the main window.

        Same rules and tolerances as :meth:`suggest`; falls back to the cache when offline."""
        now = self.clock()
        if self.source is None:
            return [], "Kein Microsoft-Kalender verbunden (optional) – Titel und Teilnehmende beim Start eingeben.", "manual"
        try:
            events = self.fetch_window()
            found = candidate_events(events, now, self.tol_before, self.tol_after)
            return found, "" if found else "Kein laufender Termin im Kalender gefunden.", "graph"
        except Exception as exc:  # offline, not signed in, ...
            log.info("Kalender nicht verfügbar: %s", exc)
            found = candidate_events(self._load_cache(), now, self.tol_before, self.tol_after)
            return found, f"Kalender nicht verfügbar ({exc})." + (" Termine aus dem Zwischenspeicher." if found else ""), \
                "cache"

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
        events = self.source.calendar_view(start - timedelta(hours=1), end + timedelta(hours=1))
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
