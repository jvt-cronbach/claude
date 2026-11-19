from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from callrec.audio.capture import DeviceSpec
from callrec.calendar.service import CalendarService, Suggestion
from callrec.config import Config
from callrec.consent import confirm_consent
from callrec.controller import Controller, StartDecision
from callrec.detect import Prompt
from callrec.models import Person
from tests.conftest import make_event


class FakeStream:
    def __init__(self, cb, dev):
        self.cb, self.dev, self.active = cb, dev, False

    def start(self):
        self.active = True
        self.cb(np.zeros(self.dev.sample_rate // 10 * self.dev.channels, dtype=np.int16).tobytes(), 0.0)

    def stop(self):
        self.active = False

    def is_active(self):
        return self.active


class FakeBackend:
    instances: list[FakeBackend] = []

    def __init__(self):
        self.opened = 0
        self.closed = False
        FakeBackend.instances.append(self)

    def resolve_devices(self, mic, loop):
        return DeviceSpec("1", "Headset Mikrofon", 16000, 1, "mic"), DeviceSpec("2", "Headset [Loopback]", 48000, 2, "loopback")

    def open_stream(self, device, on_data, fpb):
        self.opened += 1
        return FakeStream(on_data, device)

    def close(self):
        self.closed = True


class FakeUi:
    def __init__(self, decision_factory=None, prompt_answer=False):
        self.decision_factory = decision_factory
        self.prompt_answer = prompt_answer
        self.notes: list[tuple[str, str]] = []
        self.states: list[bool] = []
        self.asked: list[Suggestion] = []
        self.reviews = []

    def ask_start(self, suggestion, me):
        self.asked.append(suggestion)
        return self.decision_factory(suggestion) if self.decision_factory else None

    def ask_record_prompt(self, prompt):
        return self.prompt_answer

    def notify(self, title, message):
        self.notes.append((title, message))

    def set_recording_state(self, recording, title):
        self.states.append(recording)

    def set_status(self, text):
        pass

    def open_review(self, rec):
        self.reviews.append(rec)


class FakeQueue:
    def __init__(self):
        self.jobs = []

    def submit(self, rec, steps, on_done=None):
        self.jobs.append((rec, steps, on_done))


def _decision(s: Suggestion) -> StartDecision:
    people = s.participants or [Person("Anna Beispiel", "anna@firma.de")]
    consent = confirm_consent(user_checked_box=True, confirmed_by="Ich", participants=people, method="dialog")
    return StartDecision(s.event.subject if s.event else "Spontan", people, s.event, consent)


def _controller(ui, tmp_path: Path, events=()):
    cfg = Config()
    cfg.general.recordings_dir = str(tmp_path / "recs")

    class Src:
        def calendar_view(self, a, b):
            return list(events)

        def get_me(self):
            raise NotImplementedError

    now = datetime(2026, 11, 19, 13, 5, tzinfo=UTC)
    cal = CalendarService(Src(), tmp_path / "c.json", timedelta(minutes=10), timedelta(minutes=10), clock=lambda: now)
    FakeBackend.instances.clear()
    q = FakeQueue()
    ctl = Controller(cfg, ui, FakeBackend, cal, q, lambda: Person("Ich Selbst", "ich@firma.de", "self"))  # type: ignore[arg-type]
    return ctl, q


def test_cancelled_dialog_records_nothing(tmp_path: Path):
    ui = FakeUi(decision_factory=None)
    ctl, q = _controller(ui, tmp_path)
    assert ctl.start() is False
    assert not ctl.is_recording and FakeBackend.instances == []
    assert not (tmp_path / "recs").exists()


def test_stale_or_unconfirmed_consent_rejected(tmp_path: Path):
    def stale(s):
        d = _decision(s)
        return dataclasses.replace(d, consent=dataclasses.replace(d.consent, confirmed_at=d.consent.confirmed_at - timedelta(hours=2)))

    ui = FakeUi(stale)
    ctl, _ = _controller(ui, tmp_path)
    # controller clock is fixed at 13:05 UTC; consent "now" is real time -> also out of window
    assert ctl.start() is False
    assert FakeBackend.instances == []
    assert ui.notes and ui.notes[-1][0] == "Keine Aufnahme"


def test_full_start_stop_cycle(tmp_path: Path, monkeypatch):
    ev = make_event("Projekt Jour fixe", datetime(2026, 11, 19, 13, 0, tzinfo=UTC), is_online=True)
    ui = FakeUi(_decision)
    ctl, q = _controller(ui, tmp_path, [ev])
    # make the controller clock agree with the real consent timestamp
    ctl.clock = lambda: datetime.now(UTC)
    assert ctl.start() is True
    assert ui.asked[0].event is ev and ui.asked[0].source == "graph"
    assert ctl.is_recording and ui.states == [True]
    be = FakeBackend.instances[0]
    assert be.opened == 2
    rec = ctl.current
    assert rec is not None
    assert rec.folder.name.endswith("_Projekt-Jour-fixe")
    meta = rec.load_meta()
    assert meta["recording"]["status"] == "recording"
    assert meta["consent"]["confirmed"] is True and meta["consent"]["method"] == "dialog"
    assert meta["devices"]["mic"]["name"] == "Headset Mikrofon"
    assert meta["event"]["subject"] == "Projekt Jour fixe"
    assert ctl.start() is False  # already recording
    stopped = ctl.stop()
    assert stopped is rec and not ctl.is_recording and be.closed
    meta = rec.load_meta()
    assert meta["recording"]["status"] == "stopped" and meta["recording"]["duration_s"] >= 0
    assert (rec.folder / "audio" / "mic.part.wav").exists()
    assert q.jobs and q.jobs[0][0] is rec
    assert ctl.stop() is None


def test_reminder_never_autostarts(tmp_path: Path):
    ui = FakeUi(decision_factory=None, prompt_answer=False)
    ctl, _ = _controller(ui, tmp_path)
    ctl.on_reminder(Prompt("teams"))
    assert ui.asked == [] and FakeBackend.instances == []
    ui.prompt_answer = True
    ctl.on_reminder(Prompt("teams"))  # yes -> consent dialog, which is cancelled
    assert len(ui.asked) == 1 and not ctl.is_recording


def test_processing_done_opens_review(tmp_path: Path):
    from callrec.pipeline import ProcessResult
    from tests.conftest import make_recording

    ui = FakeUi()
    ctl, _ = _controller(ui, tmp_path)
    rec = make_recording(tmp_path / "x", with_parts=False)

    class J:
        recording = rec

    ctl._on_processed(J(), ProcessResult(steps_done=["identify", "summarize"], unassigned_speakers=1), None)
    assert ui.reviews == [rec]
    ctl._on_processed(J(), ProcessResult(steps_done=["summarize"], summary_skipped="Ollama aus"), None)
    assert "Ollama aus" in ui.notes[-1][1]
    ctl._on_processed(J(), None, RuntimeError("kaputt"))
    assert ui.notes[-1][0] == "Verarbeitung fehlgeschlagen"


def test_tick_notifies_each_recording_problem_once(tmp_path: Path):
    ui = FakeUi(_decision)
    ctl, _ = _controller(ui, tmp_path)
    ctl.clock = lambda: datetime.now(UTC)
    assert ctl.start(Suggestion(None)) is True
    import time

    ctl.recorder._writers["mic"].last_data_t = time.monotonic()  # type: ignore[union-attr]  # fake stream uses t=0
    assert ctl.tick() == []
    ctl.recorder._streams[0].active = False  # type: ignore[union-attr]
    ctl.tick()
    ctl.tick()
    problems = [n for n in ui.notes if n[0] == "Problem bei der Aufnahme"]
    assert len(problems) == 1 and "Mikrofon" in problems[0][1]
    ctl.stop()
