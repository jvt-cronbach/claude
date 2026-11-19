"""Background processing queue (one job at a time - the GPU is shared)."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from callrec.pipeline import ALL_STEPS, Pipeline, ProcessResult
from callrec.recording import Recording

log = logging.getLogger(__name__)


@dataclass
class Job:
    recording: Recording
    steps: frozenset[str] = ALL_STEPS
    on_done: Callable[[Job, ProcessResult | None, BaseException | None], None] | None = None
    result: ProcessResult | None = field(default=None, init=False)
    error: BaseException | None = field(default=None, init=False)


StatusFn = Callable[[str], None]

STEP_LABELS = {
    "finalize": "Audio abschließen",
    "transcribe": "Transkription",
    "diarize": "Sprechertrennung",
    "identify": "Sprechererkennung",
    "render": "Transkript erstellen",
    "summarize": "Zusammenfassung",
}


class ProcessingQueue:
    def __init__(self, pipeline_factory: Callable[[], Pipeline], status: StatusFn = lambda s: None) -> None:
        self._pipeline_factory = pipeline_factory
        self._status = status
        self._q: queue.Queue[Job | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="processing", daemon=True)
        self._started = False
        self.busy = False

    def start(self) -> None:
        if not self._started:
            self._thread.start()
            self._started = True

    def submit(self, rec: Recording, steps: Iterable[str] = ALL_STEPS, on_done: Callable[..., None] | None = None) -> Job:
        job = Job(rec, frozenset(steps), on_done)
        self._q.put(job)
        self.start()
        return job

    def stop(self) -> None:
        self._q.put(None)

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def pending(self) -> int:
        return self._q.qsize()

    def _run(self) -> None:
        while True:
            job = self._q.get()
            if job is None:
                break
            self.busy = True
            title = job.recording.folder.name

            def progress(step: str, frac: float, msg: str, title: str = title) -> None:
                self._status(f"{STEP_LABELS.get(step, step)} {int(frac * 100)} % – {title}")

            try:
                job.result = self._pipeline_factory().run(job.recording, job.steps, progress)
                self._status("")
            except BaseException as exc:  # noqa: BLE001 - report everything to the UI
                log.exception("Verarbeitung von %s fehlgeschlagen", title)
                job.error = exc
                self._status(f"Fehler bei der Verarbeitung von {title}")
            finally:
                self.busy = False
            if job.on_done:
                try:
                    job.on_done(job, job.result, job.error)
                except Exception:
                    log.exception("Fehler im Abschluss-Callback")
            self._q.task_done()
