"""Guided processing after a recording: one question, then one chain.

After a stop the user is asked once "Aufnahme beendet – jetzt vollständig verarbeiten?" with
the steps as checkboxes. "Vollständig verarbeiten" runs, as one chain:

    transcribe (+ speaker separation/recognition)
    -> IF a speaker with text is still unassigned: the assignment dialog (with "Überspringen")
    -> summary (so the names are in it)
    -> hand-over of the summary to SharePoint

The hand-over in this chain is authorised by the user's choice for this recording in that
question (or, explicitly, by ``processing.handoff_without_asking`` in mode "full") - so it runs
without the preview dialog. It still only ever emits the summary document, is recorded in
meta.json (``handoffs`` with ``authorized_by``) and is skipped with a message, never failing the
chain, when no SharePoint target is set up or there is no summary.

The chain state lives in meta.json (``processing.chain``): it is resumed after a restart and can
be cancelled (the "Abbrechen" of the processing panel cancels the running step and the chain).
"Später" marks the recording ``processing.status = "deferred"``; it stays in the list with
"Jetzt verarbeiten".
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from callrec.config import Config
from callrec.recording import Recording

log = logging.getLogger(__name__)

STAGE_PROCESS, STAGE_REVIEW, STAGE_SUMMARY, STAGE_HANDOFF = "process", "review", "summary", "handoff"
DONE, CANCELLED, FAILED = "done", "cancelled", "failed"
ACTIVE_STAGES = (STAGE_PROCESS, STAGE_REVIEW, STAGE_SUMMARY, STAGE_HANDOFF)
ORDER = ACTIVE_STAGES
DEFERRED = "deferred"


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


@dataclass
class Plan:
    transcript: bool = True
    speakers: bool = True
    summary: bool = True
    handoff: bool = False

    def normalised(self) -> Plan:
        """Without a transcript nothing else is possible; without a summary no hand-over."""
        t = self.transcript
        s = self.summary and t
        return Plan(t, self.speakers and t, s, self.handoff and s)

    def process_steps(self) -> frozenset[str]:
        steps = {"finalize"}
        if self.transcript:
            steps |= {"transcribe", "render"}
        if self.speakers:
            steps |= {"diarize", "identify"}
        return frozenset(steps)

    def describe(self) -> list[str]:
        """What will happen, in German (for the question and the log)."""
        p = self.normalised()
        out = ["Audio abschließen (FLAC/Opus)"]
        if p.transcript:
            out.append("Transkript erstellen (lokal)")
        if p.speakers:
            out.append("Sprecher erkennen; offene Stimmen im Dialog zuordnen (überspringbar)")
        if p.summary:
            out.append("Zusammenfassung erstellen")
        if p.handoff:
            out.append("Zusammenfassung (nur sie) an SharePoint übergeben – für den Claude-Assistenten")
        return out


def default_plan(cfg: Config) -> Plan:
    pc = cfg.processing
    return Plan(True, pc.default_speakers, pc.default_summary and cfg.summary.enabled, pc.default_handoff)


def chain_state(rec: Recording) -> dict[str, Any] | None:
    try:
        ch = (rec.load_meta().get("processing") or {}).get("chain")
    except (OSError, ValueError):
        return None
    return ch if isinstance(ch, dict) else None


def is_active(rec: Recording) -> bool:
    ch = chain_state(rec)
    return ch is not None and ch.get("stage") in ACTIVE_STAGES


class ProcessingChain:
    """Drives the chain. Thread rules: ``start``/``resume``/``cancel`` from any thread; the review
    dialog runs through ``post`` on the Tk thread; queue callbacks arrive on the processing thread.

    Ports (injected, so the chain is testable without Tk):
      ``submit(rec, steps, on_done)`` -> queue job; ``post(fn)`` -> Tk thread;
      ``review(rec) -> bool`` modal assignment dialog (False = skipped);
      ``handoff(rec, note) -> dict`` hand-over without preview; ``handoff_ready() -> (bool, why)``;
      ``notify(title, text)``; ``changed(rec)``; ``unassigned(rec) -> int`` (speakers with text);
      ``shutting_down() -> bool`` (a job cancelled by the app exit is resumed, not cancelled)."""

    def __init__(
        self,
        submit: Callable[[Recording, frozenset[str], Callable[..., None]], Any],
        post: Callable[[Callable[[], object]], None],
        review: Callable[[Recording], bool],
        handoff: Callable[[Recording, dict[str, Any]], dict[str, Any]],
        handoff_ready: Callable[[], tuple[bool, str]],
        notify: Callable[[str, str], None],
        changed: Callable[[Recording], None] = lambda rec: None,
        unassigned: Callable[[Recording], int] | None = None,
        shutting_down: Callable[[], bool] = lambda: False,
        run_in_background: Callable[[Callable[[], None]], None] | None = None,
        auto_handoff_allowed: Callable[[], bool] = lambda: True,
    ) -> None:
        self.submit = submit
        self.post = post
        self.review = review
        self.handoff = handoff
        self.handoff_ready = handoff_ready
        self.notify = notify
        self.changed = changed
        self.unassigned = unassigned or _unassigned_with_text
        self.shutting_down = shutting_down
        # mode "full" + handoff_without_asking still switched on? (checked again at the hand-over,
        # e.g. for a chain resumed after the setting was switched off)
        self.auto_handoff_allowed = auto_handoff_allowed
        self.run_in_background = run_in_background or (
            lambda fn: threading.Thread(target=fn, name="chain-handoff", daemon=True).start())
        self._lock = threading.Lock()
        self._cancelled: set[str] = set()

    # -- state in meta.json ------------------------------------------------
    def _save(self, rec: Recording, **changes: Any) -> dict[str, Any]:
        ch = dict(chain_state(rec) or {})
        ch.update(changes)
        rec.update_meta(["processing", "chain"], ch)
        return ch

    def _log(self, rec: Recording, text: str) -> None:
        ch = chain_state(rec) or {}
        entries = [*list(ch.get("log") or []), {"at": _now(), "text": text}]
        self._save(rec, log=entries)
        log.info("Verarbeitungskette %s: %s", rec.folder.name, text)

    def _plan(self, rec: Recording) -> Plan:
        p = (chain_state(rec) or {}).get("plan") or {}
        return Plan(bool(p.get("transcript", True)), bool(p.get("speakers", True)), bool(p.get("summary", True)),
                    bool(p.get("handoff", False)))

    # -- entry points -------------------------------------------------------
    def start(self, rec: Recording, plan: Plan, authorized_by: str) -> None:
        """``authorized_by``: "dialog" (the question after the recording), "setting" (mode "full")
        or "button" ("Jetzt verarbeiten", which never hands over)."""
        plan = plan.normalised()
        if plan.handoff and authorized_by not in ("dialog", "setting"):
            plan.handoff = False  # the hand-over needs the user's choice for this recording
        with self._lock:
            self._cancelled.discard(rec.folder.name)
        rec.update_meta(["processing", "chain"], {
            "plan": asdict(plan), "authorized_by": authorized_by, "chosen_at": _now(),
            "handoff_authorized": plan.handoff, "stage": STAGE_PROCESS, "log": [],
        })
        self._log(rec, "Gestartet: " + "; ".join(plan.describe()))
        self._run_stage(rec, STAGE_PROCESS)

    def resume(self, rec: Recording) -> bool:
        ch = chain_state(rec)
        if ch is None or ch.get("stage") not in ACTIVE_STAGES:
            return False
        self._log(rec, f"Nach dem Neustart fortgesetzt ({ch.get('stage')})")
        self._run_stage(rec, str(ch["stage"]))
        return True

    def cancel(self, rec: Recording) -> None:
        with self._lock:
            self._cancelled.add(rec.folder.name)
        if is_active(rec):
            self._save(rec, stage=CANCELLED)
            self._log(rec, "Abgebrochen")
            self.changed(rec)

    def on_queue_event(self, job: Any) -> None:
        """Queue listener: a waiting chain job removed by the user ("Wartende entfernen") gets no
        on_done callback - end the chain instead of resuming it at the next start."""
        from callrec.jobs import CANCELLED as JOB_CANCELLED

        if job.state != JOB_CANCELLED or self.shutting_down():
            return
        rec = job.recording
        ch = chain_state(rec)
        if ch is not None and ch.get("stage") in (STAGE_PROCESS, STAGE_SUMMARY):
            self.cancel(rec)

    def _is_cancelled(self, rec: Recording) -> bool:
        with self._lock:
            return rec.folder.name in self._cancelled

    # -- stages ---------------------------------------------------------------
    def _next_stage(self, rec: Recording, after: str) -> str | None:
        plan = self._plan(rec)
        for st in ORDER[ORDER.index(after) + 1:]:
            if st == STAGE_REVIEW and not plan.speakers:
                continue
            if st == STAGE_SUMMARY and not plan.summary:
                continue
            if st == STAGE_HANDOFF and not plan.handoff:
                continue
            return st
        return None

    def _next(self, rec: Recording, after: str) -> None:
        if self._is_cancelled(rec):
            return
        st = self._next_stage(rec, after)
        if st is None:
            self._finish(rec)
        else:
            self._run_stage(rec, st)

    def _run_stage(self, rec: Recording, stage: str) -> None:
        if self._is_cancelled(rec):
            return
        self._save(rec, stage=stage)
        plan = self._plan(rec)
        if stage == STAGE_PROCESS:
            self.submit(rec, plan.process_steps(), lambda job, res, err: self._job_done(rec, stage, res, err, job))
        elif stage == STAGE_REVIEW:
            n = self.unassigned(rec)
            if n <= 0:
                self._next(rec, STAGE_REVIEW)
                return
            self.post(lambda: self._do_review(rec))
        elif stage == STAGE_SUMMARY:
            self.submit(rec, frozenset({"summarize"}), lambda job, res, err: self._job_done(rec, stage, res, err, job))
        elif stage == STAGE_HANDOFF:
            self.run_in_background(lambda: self._do_handoff(rec))

    def _job_done(self, rec: Recording, stage: str, result: Any, error: BaseException | None,
                  job: Any = None) -> None:
        if error is None and bool(getattr(job, "cancel_requested", False)) and not self.shutting_down():
            # "Abbrechen" was clicked while the last step of the job ran: the step finished, but the
            # chain must stop here (no summary to the cloud, no hand-over after a cancel)
            from callrec.pipeline import ProcessingCancelled

            error = ProcessingCancelled()
        if error is not None:
            from callrec.pipeline import is_cancellation

            if self.shutting_down():
                self._log(rec, "Beim Beenden unterbrochen – wird beim nächsten Start fortgesetzt")
                return  # stage stays active -> resumed at the next start
            if is_cancellation(error) or self._is_cancelled(rec):
                self._save(rec, stage=CANCELLED)
                self._log(rec, "Abgebrochen")
                self.notify("Verarbeitung abgebrochen", rec.folder.name)
            else:
                self._save(rec, stage=FAILED, error=str(error))
                self._log(rec, f"Fehlgeschlagen: {error}")
                self.notify("Verarbeitung fehlgeschlagen", f"{rec.folder.name}: {error}")
            self.changed(rec)
            return
        skipped = str(getattr(result, "summary_skipped", "") or "") if result is not None else ""
        if stage == STAGE_SUMMARY and (skipped or bool(getattr(result, "summary_failed", False))):
            self._save(rec, summary_problem=str(getattr(result, "summary_skipped", "") or "fehlgeschlagen"))
            self._log(rec, f"Zusammenfassung nicht erstellt: {getattr(result, 'summary_skipped', '')}")
        self.changed(rec)
        if self.shutting_down():
            # the step finished during the app exit: record the progress, run the next stage
            # (dialog, cloud summary, hand-over) only at the next start
            nxt = self._next_stage(rec, stage)
            if nxt is None:
                self._finish(rec)
            else:
                self._save(rec, stage=nxt)
                self._log(rec, "Beim Beenden angehalten – wird beim nächsten Start fortgesetzt")
            return
        self._next(rec, stage)

    def _do_review(self, rec: Recording) -> None:
        """Tk thread: open the assignment dialog and wait; then continue (also when skipped)."""
        if self._is_cancelled(rec):
            return
        try:
            applied = self.review(rec)
        except Exception as exc:
            log.exception("Sprecherzuordnung in der Kette fehlgeschlagen")
            applied = False
            self.notify("Sprecher zuordnen", f"Dialog fehlgeschlagen: {exc} – die Verarbeitung geht weiter.")
        self._log(rec, "Sprecher zugeordnet" if applied else "Sprecherzuordnung übersprungen")
        self.changed(rec)
        self._next(rec, STAGE_REVIEW)

    def _do_handoff(self, rec: Recording) -> None:
        if self._is_cancelled(rec):
            return
        ch = chain_state(rec) or {}
        if not ch.get("handoff_authorized"):
            self._log(rec, "Übergabe übersprungen: nicht für diese Aufnahme gewählt")
            self._finish(rec)
            return
        why = ""
        if ch.get("authorized_by") == "setting" and not self.auto_handoff_allowed():
            why = "„ohne Rückfrage übergeben“ ist inzwischen ausgeschaltet"
        elif ch.get("summary_problem") or not rec.summary_path.exists():
            why = "es gibt keine Zusammenfassung" + (f" ({ch.get('summary_problem')})" if ch.get("summary_problem") else "")
        else:
            ready, reason = self.handoff_ready()
            if not ready:
                why = reason
        if why:
            self._save(rec, handoff_skipped=why)
            self._log(rec, f"Übergabe an SharePoint übersprungen: {why}")
            self.notify("Übergabe übersprungen", f"{rec.title()}: {why}")
            self._finish(rec)
            return
        note = {"authorized_by": "postcall_dialog" if ch.get("authorized_by") == "dialog" else "setting_handoff_without_asking",
                "preview_shown": False}
        try:
            entry = self.handoff(rec, note)
        except Exception as exc:
            log.warning("Übergabe in der Kette fehlgeschlagen", exc_info=True)
            self._save(rec, handoff_skipped=f"Fehler: {exc}")
            self._log(rec, f"Übergabe an SharePoint fehlgeschlagen: {exc}")
            self.notify("Übergabe fehlgeschlagen", f"{rec.title()}: {exc} – später im Hauptfenster erneut versuchen.")
            self._finish(rec)
            return
        self._log(rec, f"Zusammenfassung übergeben: {entry.get('location', '')}")
        self.notify("An SharePoint übergeben", f"{rec.title()}: {entry.get('location', '')}")
        self._finish(rec)

    def _finish(self, rec: Recording) -> None:
        self._save(rec, stage=DONE, finished_at=_now())
        self._log(rec, "Fertig")
        self.notify(f"Fertig: {rec.title()}", "Die Verarbeitung ist abgeschlossen.")
        self.changed(rec)


def _unassigned_with_text(rec: Recording) -> int:
    from callrec.library import read_info

    n = read_info(rec.folder).speakers_unassigned
    return int(n or 0)


def mark_deferred(rec: Recording) -> None:
    """ "Später": keep the recording unprocessed (not resumed automatically at the next start)."""
    rec.update_meta(["processing", "status"], DEFERRED)
    rec.update_meta(["processing", "deferred_at"], _now())


def startup_work(root: Any) -> list[tuple[Recording, str]]:
    """What to do with each recording at the app start, exactly once per recording:

    * "resume": a guided chain was interrupted (any stage) -> continue it;
    * "ask": stopped (or recovered after a crash) but never decided -> ask the question;
    * "process": an interrupted processing of an older version without chain -> process as before.
    Deferred recordings ("Später") are left alone."""
    from callrec.recording import list_recordings, recordings_to_resume

    out: list[tuple[Recording, str]] = []
    seen: set[Any] = set()
    for rec in list_recordings(root):
        if is_active(rec):
            out.append((rec, "resume"))
            seen.add(rec.folder)
    for rec, _interrupted in recordings_to_resume(root):
        if rec.folder in seen:
            continue
        seen.add(rec.folder)
        try:
            meta = rec.load_meta()
        except (OSError, ValueError):
            continue
        proc = meta.get("processing") or {}
        if proc.get("status") == "running":
            out.append((rec, "process"))
        elif proc.get("status") == DEFERRED or isinstance(proc.get("chain"), dict):
            # decided already ("Später", or a chain that was cancelled/failed): the unfinished WAV
            # parts alone must not bring the question back at every start
            continue
        else:
            out.append((rec, "ask"))
    return out
