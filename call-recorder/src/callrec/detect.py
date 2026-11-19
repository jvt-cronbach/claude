"""Reminder logic: notice that a Teams call (probably) started and ASK whether to record.

Nothing here ever starts a recording; it only yields prompts for the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from callrec.models import CalendarEvent


@dataclass
class Prompt:
    reason: str  # "teams" | "calendar"
    event: CalendarEvent | None = None

    def message(self) -> str:
        if self.reason == "calendar" and self.event:
            return f"Der Termin „{self.event.subject}“ beginnt. Möchten Sie das Gespräch aufnehmen?"
        return "Teams nutzt gerade das Mikrofon - beginnt ein Anruf? Möchten Sie das Gespräch aufnehmen?"


@dataclass
class CallDetector:
    cooldown: timedelta = timedelta(minutes=10)
    confirm_polls: int = 2  # Teams must hold the mic for this many polls in a row
    calendar_early: timedelta = timedelta(minutes=1)
    calendar_late: timedelta = timedelta(minutes=5)

    _active_count: int = 0
    _teams_was_active: bool = False
    _last_prompt: datetime | None = None
    _prompted_events: set[str] = field(default_factory=set)

    def update(
        self,
        now: datetime,
        teams_mic_active: bool,
        recording: bool,
        events: list[CalendarEvent] | None = None,
    ) -> Prompt | None:
        prompt: Prompt | None = None

        # Teams audio capture session (rising edge, debounced)
        if teams_mic_active:
            self._active_count += 1
        else:
            self._active_count = 0
            self._teams_was_active = False
        if self._active_count >= self.confirm_polls and not self._teams_was_active:
            self._teams_was_active = True
            prompt = Prompt("teams")

        # Online meeting from the calendar just starting
        for ev in events or []:
            if not ev.is_online or ev.is_cancelled or ev.declined or ev.id in self._prompted_events:
                continue
            if ev.start - self.calendar_early <= now <= ev.start + self.calendar_late:
                self._prompted_events.add(ev.id)
                if prompt is None:
                    prompt = Prompt("calendar", ev)

        if prompt is None or recording:
            return None
        if self._last_prompt is not None and now - self._last_prompt < self.cooldown:
            return None
        self._last_prompt = now
        return prompt

    def snooze(self, now: datetime) -> None:
        """User answered 'no' - stay quiet for the cooldown period."""
        self._last_prompt = now
