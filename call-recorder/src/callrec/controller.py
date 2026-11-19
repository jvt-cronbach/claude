"""Application logic of the tray app, independent of the concrete UI toolkit."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from callrec.audio.capture import CaptureBackend, DualTrackRecorder
from callrec.calendar.service import CalendarService, Suggestion
from callrec.config import Config
from callrec.consent import ConsentError, ConsentRecord, require_consent
from callrec.detect import Prompt
from callrec.jobs import ProcessingQueue
from callrec.models import CalendarEvent, Person
from callrec.naming import create_recording_folder
from callrec.paths import cloud_sync_root_for
from callrec.pipeline import ALL_STEPS, ProcessResult
from callrec.recording import Recording, new_meta

log = logging.getLogger(__name__)


@dataclass
class StartDecision:
    title: str
    participants: list[Person]
    event: CalendarEvent | None
    consent: ConsentRecord


class UiPort(Protocol):
    """Implemented by the Tk UI. Methods may be called from any thread."""

    def ask_start(self, suggestion: Suggestion, me: Person) -> StartDecision | None: ...

    def ask_record_prompt(self, prompt: Prompt) -> bool: ...

    def notify(self, title: str, message: str) -> None: ...

    def set_recording_state(self, recording: bool, title: str) -> None: ...

    def set_status(self, text: str) -> None: ...

    def open_review(self, rec: Recording) -> None: ...


class Controller:
    def __init__(
        self,
        cfg: Config,
        ui: UiPort,
        backend_factory: Callable[[], CaptureBackend],
        calendar: CalendarService,
        queue: ProcessingQueue,
        me_provider: Callable[[], Person],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.cfg = cfg
        self.ui = ui
        self.backend_factory = backend_factory
        self.calendar = calendar
        self.queue = queue
        self.me_provider = me_provider
        self.clock = clock
        self.recorder: DualTrackRecorder | None = None
        self.backend: CaptureBackend | None = None
        self.current: Recording | None = None
        self._lock = threading.RLock()
        self._starting = False

    @property
    def is_recording(self) -> bool:
        return self.recorder is not None and self.recorder.is_recording

    def toggle(self) -> None:
        if self.is_recording:
            self.stop()
        else:
            self.start()

    # ------------------------------------------------------------------
    def start(self, suggestion: Suggestion | None = None) -> bool:
        with self._lock:
            if self.is_recording or self._starting:
                return False
            self._starting = True
        try:
            return self._start(suggestion)
        finally:
            self._starting = False

    def _start(self, suggestion: Suggestion | None) -> bool:
        me = self.me_provider()
        if suggestion is None:
            suggestion = self.calendar.suggest()
        decision = self.ui.ask_start(suggestion, me)
        if decision is None:
            log.info("Aufnahme abgebrochen (keine Einwilligungsbestätigung)")
            return False
        try:
            consent = require_consent(decision.consent, self.clock())
        except ConsentError as exc:
            self.ui.notify("Keine Aufnahme", str(exc))
            return False

        root = self.cfg.recordings_path()
        if cloud_sync_root_for(root):
            self.ui.notify(
                "Achtung: Cloud-Ordner",
                f"Der Aufnahmeordner {root} liegt in OneDrive - Aufnahmen würden in die Cloud synchronisiert.",
            )
        try:
            backend = self.backend_factory()
            mic, loop = backend.resolve_devices(self.cfg.audio.mic_device, self.cfg.audio.loopback_device)
        except Exception as exc:
            log.exception("Audiogeräte nicht verfügbar")
            self.ui.notify("Aufnahme nicht möglich", f"Audiogeräte nicht verfügbar: {exc}")
            return False

        started = self.clock().astimezone()
        folder = create_recording_folder(root, started, decision.title)
        rec = Recording(folder)
        rec.save_meta(
            new_meta(
                title=decision.title,
                event=decision.event,
                participants=decision.participants,
                self_person=me,
                consent=consent,
                devices={"mic": mic.to_dict(), "loopback": loop.to_dict()},
                started_at=started,
            )
        )
        recorder = DualTrackRecorder(
            backend, folder, mic, loop, self.cfg.audio.gap_threshold_s, self.cfg.audio.frames_per_buffer
        )
        try:
            recorder.start(consent)
        except Exception as exc:
            rec.update_meta(["recording", "status"], "failed")
            backend.close()
            self.ui.notify("Aufnahme fehlgeschlagen", str(exc))
            return False
        self.recorder, self.backend, self.current = recorder, backend, rec
        self.ui.set_recording_state(True, decision.title)
        self.ui.notify("Aufnahme läuft", f"{decision.title}\nMikrofon: {mic.name}\nGegenseite: {loop.name}")
        return True

    def stop(self) -> Recording | None:
        with self._lock:
            recorder, rec, backend = self.recorder, self.current, self.backend
            if recorder is None or rec is None:
                return None
            self.recorder = None
            self.current = None
            self.backend = None
        result = recorder.stop()
        if backend is not None:
            backend.close()
        rec.update_meta(
            ["recording"],
            {
                **(rec.load_meta().get("recording") or {}),
                "status": "stopped",
                "stopped_at": self.clock().astimezone().isoformat(),
                "duration_s": round(result.duration_s, 3),
                "inserted_silence_s": {
                    "mic": round(result.mic.inserted_silence_s, 3),
                    "remote": round(result.remote.inserted_silence_s, 3),
                },
                "warnings": result.warnings,
            },
        )
        self.ui.set_recording_state(False, "")
        self.ui.notify("Aufnahme beendet", "Die Verarbeitung läuft im Hintergrund.")
        self.process(rec)
        return rec

    def process(self, rec: Recording, steps: frozenset[str] = ALL_STEPS) -> None:
        self.queue.submit(rec, steps, on_done=self._on_processed)

    def _on_processed(self, job: Any, result: ProcessResult | None, error: BaseException | None) -> None:
        rec: Recording = job.recording
        if error is not None:
            self.ui.notify("Verarbeitung fehlgeschlagen", f"{rec.folder.name}: {error}")
            return
        assert result is not None
        msg = "Transkript erstellt."
        if result.summary_skipped:
            msg += f" Zusammenfassung übersprungen: {result.summary_skipped}"
        elif "summarize" in result.steps_done:
            msg += " Zusammenfassung erstellt."
        self.ui.notify(f"Fertig: {rec.title()}", msg)
        if result.unassigned_speakers and self.cfg.ui.open_review_after_processing and "identify" in result.steps_done:
            self.ui.open_review(rec)

    def tick(self) -> list[str]:
        """Periodic health check while recording."""
        if self.recorder is None:
            return []
        warnings = self.recorder.health_warnings()
        for w in warnings:
            self.ui.set_status(w)
        return warnings

    def on_reminder(self, prompt: Prompt) -> None:
        """Ask (never auto-start); on 'yes' the normal consent flow follows."""
        if self.is_recording:
            return
        if self.ui.ask_record_prompt(prompt):
            sugg = None
            if prompt.event is not None:
                sugg = Suggestion(prompt.event, prompt.event.participants(), source="graph")
            self.start(sugg)
