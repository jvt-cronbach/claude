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
from callrec.calendar.selection import nearest_event, select_current_event
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
