"""Application logic of the tray app, independent of the concrete UI toolkit."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from callrec.audio.capture import CaptureBackend, DualTrackRecorder
from callrec.calendar.service import CalendarService, Suggestion
from callrec.config import Config
from callrec.consent import ConsentError, ConsentRecord, active_summary_provider, require_consent
from callrec.detect import Prompt
from callrec.jobs import ProcessingQueue
from callrec.models import CalendarEvent, Person
from callrec.naming import create_recording_folder
from callrec.paths import cloud_sync_root_for
from callrec.pipeline import ALL_STEPS, STEP_PRESETS, ProcessResult, is_cancellation
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
        self._notified: set[str] = set()
        self._stopped_at = float("-inf")  # time.monotonic() of the last stop
        self._session = 0  # increases with every started recording (stale auto-stops are ignored)
        self._event_end: datetime | None = None
        # extra data for meta.json "recording.autostop" at the stop (set by the app: AutoStopRunner.info)
        self.stop_info: Callable[[int], dict[str, Any]] | None = None
        # what happens with a stopped recording (the app asks first; default: process right away)
        self.after_stop: Callable[[Recording, str], None] | None = None

    @property
    def is_recording(self) -> bool:
        return self.recorder is not None and self.recorder.is_recording

    # -- live state for the main window (cheap reads, any thread) -------
    def elapsed(self) -> float:
        r = self.recorder
        return r.elapsed if r is not None else 0.0

    def levels(self) -> dict[str, float]:
        r = self.recorder
        return r.levels() if r is not None else {"mic": 0.0, "remote": 0.0}

    @property
    def session_id(self) -> int:
        """Id of the running recording (0 = none)."""
        return self._session if self.recorder is not None else 0

    def silence_durations(self) -> dict[str, float]:
        r = self.recorder
        return r.silence_durations() if r is not None else {"mic": 0.0, "remote": 0.0}

    def current_event_end(self) -> datetime | None:
        return self._event_end if self.recorder is not None else None

    def current_title(self) -> str:
        rec = self.current
        try:
            return rec.title() if rec is not None else ""
        except (OSError, ValueError):
            return rec.folder.name if rec is not None else ""

    def is_busy(self, rec: Recording) -> bool:
        """Being recorded right now or waiting/running in the processing queue."""
        from callrec.paths import folder_key

        cur = self.current
        return (cur is not None and (cur.folder == rec.folder or folder_key(cur.folder) == folder_key(rec.folder))) \
            or self.queue.is_busy_with(rec.folder)

    # A double click on "stop" (window button, hotkey pressed twice) must not open the dialog of
    # a new recording right away: within this time after a stop a toggle does not start one.
    TOGGLE_GUARD_S = 1.5

    def just_stopped(self) -> bool:
        return time.monotonic() - self._stopped_at < self.TOGGLE_GUARD_S

    def toggle(self) -> None:
        if self.is_recording:
            self.stop()
        elif self.just_stopped():
            log.info("Start ignoriert: Aufnahme wurde gerade erst beendet (Doppelklick?)")
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
            consent = require_consent(decision.consent, self.clock(), active_summary_provider(self.cfg.summary))
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
            backend, folder, mic, loop, self.cfg.audio.gap_threshold_s, self.cfg.audio.frames_per_buffer,
            silence_dbfs=self.cfg.autostop.silence_dbfs,
        )
        try:
            recorder.start(consent)
        except Exception as exc:
            rec.update_meta(["recording", "status"], "failed")
            backend.close()
            self.ui.notify("Aufnahme fehlgeschlagen", str(exc))
            return False
        with self._lock:
            self._session += 1
            self.recorder, self.backend, self.current = recorder, backend, rec
            self._event_end = decision.event.end if decision.event is not None else None
        self._notified = set()
        self.ui.set_recording_state(True, decision.title)
        from callrec.speakers.threads import set_recording_active

        set_recording_active(True)  # background speaker separation uses fewer cores meanwhile
        self.ui.notify("Aufnahme läuft", f"{decision.title}\nMikrofon: {mic.name}\nGegenseite: {loop.name}")
        return True

    def stop(self, reason: str = "manual", *, session: int | None = None, by: str = "user",
             detail: str = "") -> Recording | None:
        """The one stop path (button, hotkey, tray, overlay, app exit and the automatic end).

        ``reason``: manual | call_ended | silence | max_duration | app_quit (stored in meta.json).
        ``session``: only stop if this recording is still the running one - an automatic stop that
        was decided for a recording the user stopped meanwhile does nothing (returns None)."""
        from callrec.autostop import REASON_TEXT

        with self._lock:
            recorder, rec, backend = self.recorder, self.current, self.backend
            if recorder is None or rec is None:
                return None
            if session is not None and session != self._session:
                return None
            stopped_session = self._session
            self.recorder = None
            self.current = None
            self.backend = None
            self._event_end = None
            self._stopped_at = time.monotonic()
        result = recorder.stop()
        if backend is not None:
            backend.close()
        stopped_at = self.clock().astimezone().isoformat()
        info: dict[str, Any] = {}
        if self.stop_info is not None:
            try:
                info = self.stop_info(stopped_session)
            except Exception:
                log.exception("Angaben zum automatischen Ende nicht ermittelbar")
        stop_entry = {"reason": reason, "reason_text": REASON_TEXT.get(reason, reason), "by": by, "at": stopped_at}
        if detail:
            stop_entry["detail"] = detail
        rec.update_meta(
            ["recording"],
            {
                **(rec.load_meta().get("recording") or {}),
                "status": "stopped",
                "stopped_at": stopped_at,
                "duration_s": round(result.duration_s, 3),
                "inserted_silence_s": {
                    "mic": round(result.mic.inserted_silence_s, 3),
                    "remote": round(result.remote.inserted_silence_s, 3),
                },
                "warnings": result.warnings,
                "stop": stop_entry,
                **({"autostop": info} if info else {}),
            },
        )
        log.info("Aufnahme beendet: %s (%s)%s", reason, by, f" - {detail}" if detail else "")
        self.ui.set_recording_state(False, "")
        from callrec.speakers.threads import set_recording_active

        set_recording_active(False)
        next_text = "Die Verarbeitung läuft im Hintergrund." if self.after_stop is None else "Die Aufnahme ist gespeichert."
        if by == "automatic":
            self.ui.notify("Aufnahme automatisch beendet", f"{REASON_TEXT.get(reason, reason)}. {detail} {next_text}".strip())
        else:
            self.ui.notify("Aufnahme beendet", next_text)
        if self.after_stop is not None:
            try:
                self.after_stop(rec, reason)
            except Exception:
                log.exception("Nach der Aufnahme: Verarbeitung konnte nicht angestoßen werden")
                self.process(rec)
        else:
            self.process(rec)
        self._stopped_at = time.monotonic()  # again: recorder.stop() can take a moment
        return rec

    def process(self, rec: Recording, steps: frozenset[str] = ALL_STEPS, language: str | None = None,
                preset: str | None = None, profile: str | None = None) -> None:
        kwargs: dict[str, Any] = {}
        if language:
            kwargs["language"] = language
        if preset:
            kwargs["preset"] = preset
        if profile:
            kwargs["profile"] = profile
        self.queue.submit(rec, steps, on_done=self._on_processed, **kwargs)

    # Named operations used by tray, main window and CLI alike
    def resummarize(self, rec: Recording) -> None:
        self.process(rec, STEP_PRESETS["resummarize"])

    def retranscribe(self, rec: Recording, language: str | None = None, preset: str | None = None,
                     profile: str | None = None) -> None:
        self.process(rec, STEP_PRESETS["retranscribe"], language=language, preset=preset, profile=profile)

    def reprocess(self, rec: Recording) -> None:
        self.process(rec, ALL_STEPS)

    def rename(self, rec: Recording, title: str) -> str:
        from callrec.library import LibraryError, rename_recording

        if self.is_busy(rec):
            raise LibraryError("Die Aufnahme wird gerade aufgenommen oder verarbeitet - bitte danach umbenennen.")
        title = rename_recording(rec, title)
        if rec.utterances_path.exists():
            self.process(rec, STEP_PRESETS["render"])  # new title in the transcript header (same version)
        return title

    def delete(self, rec: Recording, permanent: bool = False) -> str:
        """The whole recording ("recycled" / "deleted") - through :meth:`remove`."""
        from callrec.retention import SCOPE_RECORDING

        text = self.remove(rec, SCOPE_RECORDING, "permanent" if permanent else "recycle_bin")
        return "recycled" if "Papierkorb" in text else "deleted"

    def remove(self, rec: Recording, scope: str, mode: str, by: str = "user", reason: str = "") -> str:
        """The one delete path (manual deletion in both UIs, the CLI and the automatic retention):
        never while the recording is recorded, queued or processed."""
        from callrec.library import LibraryError
        from callrec.retention import remove

        if self.is_busy(rec):
            raise LibraryError("Die Aufnahme wird gerade aufgenommen oder verarbeitet - Löschen ist erst danach möglich.")
        return remove(rec, self.cfg.recordings_path(), scope, mode, by=by, reason=reason)

    def _on_processed(self, job: Any, result: ProcessResult | None, error: BaseException | None) -> None:
        rec: Recording = job.recording
        if error is not None:
            if is_cancellation(error) or getattr(job, "cancel_requested", False):
                self.ui.notify("Verarbeitung abgebrochen", rec.folder.name)
            else:
                self.ui.notify("Verarbeitung fehlgeschlagen", f"{rec.folder.name}: {error}")
            return
        assert result is not None
        msg = "Transkript erstellt."
        if result.summary_skipped:
            msg += f" Zusammenfassung übersprungen: {result.summary_skipped}"
            if result.summary_failed:
                msg += " Später nachholen: im Hauptfenster „Zusammenfassung neu erstellen“."
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
            key = w.split(" seit ")[0]
            if key not in self._notified:  # a tooltip alone is easily missed during a call
                self._notified.add(key)
                self.ui.notify("Problem bei der Aufnahme", w)
        return warnings

    def on_reminder(self, prompt: Prompt) -> None:
        """Ask (never auto-start); on 'yes' the normal consent flow follows."""
        if self.is_recording:
            return
        if self.ui.ask_record_prompt(prompt):
            sugg = None
            if prompt.event is not None:
                sugg = Suggestion(prompt.event, prompt.event.participants(), source="graph")
            try:
                self.start(sugg)
            finally:
                # a decision made in the prompt (mode "ask") is valid for exactly this start; if the start
                # was refused before asking (another start dialog open, already recording), it must not be
                # picked up by the next, unrelated start
                drop = getattr(self.ui, "drop_prepared_start", None)
                if callable(drop):
                    drop()
