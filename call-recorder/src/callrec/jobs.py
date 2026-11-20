"""Background processing queue (one job at a time - the GPU is shared).

Besides running jobs, the queue keeps a small, thread-safe picture of what it is doing
(:meth:`ProcessingQueue.snapshot`): the running job with its current step, fraction and
message, the waiting jobs and the most recently finished ones. The tray tooltip, the main
window and the console all read from here; nothing else tracks processing state.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from callrec.pipeline import ALL_STEPS, Pipeline, ProcessResult, _accepts, is_cancellation
from callrec.recording import Recording

log = logging.getLogger(__name__)
_ids = itertools.count(1)

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"


@dataclass
class Job:
    recording: Recording
    steps: frozenset[str] = ALL_STEPS
    on_done: Callable[[Job, ProcessResult | None, BaseException | None], None] | None = None
    language: str | None = None  # one-off transcription language ("auto" = detect again)
    preset: str | None = None  # one-off preset for this job (None = current settings)
    result: ProcessResult | None = field(default=None, init=False)
    error: BaseException | None = field(default=None, init=False)
    id: int = field(default_factory=lambda: next(_ids), init=False)
    state: str = field(default=QUEUED, init=False)
    step: str = field(default="", init=False)
    fraction: float = field(default=-1.0, init=False)  # < 0: unknown (indeterminate)
    message: str = field(default="", init=False)
    cancel_requested: bool = field(default=False, init=False)
    submitted_at: float = field(default_factory=time.time, init=False)
    finished_at: float | None = field(default=None, init=False)

    @property
    def title(self) -> str:
        return self.recording.folder.name

    def status_line(self) -> str:
        if self.state == QUEUED:
            return f"Wartet: {self.title}"
        if not self.step:
            return f"Startet … – {self.title}"
        return status_text(self.step, self.fraction, self.message, self.title)


StatusFn = Callable[[str], None]

STEP_LABELS = {
    "finalize": "Audio abschließen",
    "transcribe": "Transkription",
    "diarize": "Sprechertrennung",
    "identify": "Sprechererkennung",
    "render": "Transkript erstellen",
    "summarize": "Zusammenfassung",
}
# steps whose running work can be stopped immediately (the Whisper worker process)
INTERRUPTIBLE_STEPS = frozenset({"transcribe"})


def status_text(step: str, frac: float, msg: str, title: str = "") -> str:
    """Tray/console status: a percentage, or the phase text while the fraction is unknown (< 0)."""
    label = STEP_LABELS.get(step, step)
    text = f"{label}: {msg}" if frac < 0 else f"{label} {int(frac * 100)} %" + (f" ({msg})" if msg else "")
    return f"{text} – {title}" if title else text


@dataclass(frozen=True)
class QueueSnapshot:
    current: Job | None
    pending: list[Job]
    recent: list[Job]  # newest first


class ProcessingQueue:
    HISTORY = 30

    def __init__(self, pipeline_factory: Callable[..., Pipeline], status: StatusFn = lambda s: None) -> None:
        """``pipeline_factory()`` builds a pipeline with the current settings; with a job preset it is
        called as ``pipeline_factory(preset="Name")``."""
        self._pipeline_factory = pipeline_factory
        self._status = status
        self._q: queue.Queue[Job | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="processing", daemon=True)
        self._started = False
        self._lock = threading.Lock()
        self._pending: list[Job] = []
        self._current: Job | None = None
        self._recent: deque[Job] = deque(maxlen=self.HISTORY)
        self._listeners: list[Callable[[Job], None]] = []
        self.busy = False
        self._shutting_down = False

    def start(self) -> None:
        if not self._started:
            self._thread.start()
            self._started = True

    def add_listener(self, fn: Callable[[Job], None]) -> None:
        """``fn(job)`` is called (on the processing thread) whenever a job finishes or is cancelled."""
        self._listeners.append(fn)

    def submit(self, rec: Recording, steps: Iterable[str] = ALL_STEPS, on_done: Callable[..., None] | None = None,
               language: str | None = None, preset: str | None = None) -> Job:
        job = Job(rec, frozenset(steps), on_done, language, preset or None)
        with self._lock:
            self._pending.append(job)
        self._q.put(job)
        self.start()
        return job

    def stop(self) -> None:
        self._q.put(None)

    def shutdown(self) -> None:
        """App exit: stop the running job like a cancellation, but leave a recording whose first (full)
        processing was running marked as interrupted (processing.status "running"), so the next start
        resumes it - unlike a job the user cancelled, which stays "cancelled" until started again by
        hand. Waiting jobs are not started any more; they keep their state and are found again at the
        next start as before."""
        self._shutting_down = True
        with self._lock:
            cur = self._current
        if cur is not None:
            cur.cancel_requested = True
        from callrec.asr.transcriber import cancel_running_workers

        cancel_running_workers()
        self.stop()

    def join(self, timeout: float | None = None) -> None:
        if self._started:
            self._thread.join(timeout)

    def pending(self) -> int:
        with self._lock:
            return len(self._pending)

    def snapshot(self) -> QueueSnapshot:
        """Copies (safe to read on any thread while the job goes on)."""
        with self._lock:
            cur = self._copy(self._current) if self._current is not None else None
            pend = [self._copy(j) for j in self._pending]
            rec = [self._copy(j) for j in reversed(self._recent)]
        return QueueSnapshot(cur, pend, rec)

    @staticmethod
    def _copy(job: Job) -> Job:
        c = replace(job)
        c.id, c.state, c.step, c.fraction, c.message = job.id, job.state, job.step, job.fraction, job.message
        c.cancel_requested, c.submitted_at, c.finished_at = job.cancel_requested, job.submitted_at, job.finished_at
        c.result, c.error = job.result, job.error
        return c

    def is_busy_with(self, folder: Path) -> bool:
        """True while a job for this recording is waiting or running."""
        from callrec.paths import folder_key

        folder = Path(folder)
        key = folder_key(folder)  # the same folder however it is spelled (web UI: resolved)
        with self._lock:
            jobs = [*self._pending, *([self._current] if self._current else [])]
        return any(j.recording.folder == folder or folder_key(j.recording.folder) == key for j in jobs)

    def cancel(self, job_id: int | None = None) -> str:
        """Cancel a waiting job, or the running one (``None`` = the running job).

        Returns a German sentence for the user describing what happens."""
        with self._lock:
            for j in self._pending:
                if j.id == job_id:
                    self._pending.remove(j)
                    j.cancel_requested, j.state, j.finished_at = True, CANCELLED, time.time()
                    self._recent.append(j)
                    waiting = j
                    break
            else:
                waiting = None
            cur = self._current
        if waiting is not None:
            self._notify(waiting)
            return f"Wartende Verarbeitung von {waiting.title} entfernt."
        if cur is None or (job_id is not None and cur.id != job_id):
            return "Gerade läuft keine Verarbeitung."
        cur.cancel_requested = True
        from callrec.asr.transcriber import cancel_running_workers

        if cancel_running_workers() > 0:
            return "Die laufende Transkription bzw. der Modell-Download wird beendet. Ein erneuter Start setzt den Download fort."
        label = STEP_LABELS.get(cur.step, cur.step or "Schritt")
        return f"Abbruch angefordert: Der laufende Schritt ({label}) wird noch beendet, danach stoppt die Verarbeitung."

    def cancel_current(self) -> bool:
        """Abort the running job's transcription worker (model download included)."""
        from callrec.asr.transcriber import cancel_running_workers

        with self._lock:
            cur = self._current
        if cur is not None:
            cur.cancel_requested = True
        return bool(self.busy) and cancel_running_workers() > 0

    @staticmethod
    def _mark_interrupted(job: Job) -> None:
        try:
            job.recording.update_meta(["processing", "status"], "running")
            log.info("Verarbeitung von %s beim Beenden unterbrochen - wird beim nächsten Start fortgesetzt", job.title)
        except (OSError, ValueError):
            log.warning("Status von %s nicht gespeichert", job.title, exc_info=True)

    def _notify(self, job: Job) -> None:
        for fn in list(self._listeners):
            try:
                fn(job)
            except Exception:
                log.exception("Fehler im Warteschlangen-Beobachter")

    def _run(self) -> None:
        while True:
            job = self._q.get()
            if job is None:
                break
            with self._lock:
                if job.state == CANCELLED or self._shutting_down:  # removed while waiting / app exit
                    self._q.task_done()
                    continue
                if job in self._pending:
                    self._pending.remove(job)
                self._current = job
                job.state = RUNNING
            self.busy = True
            title = job.title

            def progress(step: str, frac: float, msg: str, title: str = title, job: Job = job) -> None:
                job.step, job.fraction, job.message = step, frac, msg
                self._status(status_text(step, frac, msg, title))

            try:
                pipeline = self._pipeline_factory(preset=job.preset) if job.preset else self._pipeline_factory()
                kwargs: dict[str, object] = {}
                if job.language:
                    kwargs["language"] = job.language
                if _accepts(pipeline.run, "should_cancel"):
                    kwargs["should_cancel"] = lambda job=job: job.cancel_requested
                job.result = pipeline.run(job.recording, job.steps, progress, **kwargs)  # type: ignore[arg-type]
                job.state = DONE
                self._status("")
            except BaseException as exc:  # noqa: BLE001 - report everything to the UI
                job.error = exc
                if is_cancellation(exc) or job.cancel_requested:
                    job.state = CANCELLED
                    log.info("Verarbeitung von %s abgebrochen", title)
                    self._status("")
                    if self._shutting_down and "finalize" in job.steps:
                        # first/full processing: resumed at the next start. A partial job (re-summarise,
                        # re-transcribe) stays "cancelled" - resuming means a full re-processing.
                        self._mark_interrupted(job)
                else:
                    job.state = FAILED
                    log.exception("Verarbeitung von %s fehlgeschlagen", title)
                    self._status(f"Fehler bei der Verarbeitung von {title}")
            finally:
                self.busy = False
                job.finished_at = time.time()
                with self._lock:
                    self._current = None
                    self._recent.append(job)
            if job.on_done:
                try:
                    job.on_done(job, job.result, job.error)
                except Exception:
                    log.exception("Fehler im Abschluss-Callback")
            self._notify(job)
            self._q.task_done()
