"""Guided processing after a recording (question -> chain), with fakes for queue, dialog and hand-over."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from callrec.config import Config, ConfigError, config_from_dict
from callrec.postcall import (
    CANCELLED,
    DONE,
    FAILED,
    STAGE_PROCESS,
    STAGE_REVIEW,
    Plan,
    ProcessingChain,
    chain_state,
    default_plan,
    is_active,
    mark_deferred,
    startup_work,
)
from callrec.recording import find_unprocessed
from tests.conftest import make_recording


class World:
    def __init__(self, unassigned: int = 0, ready: tuple[bool, str] = (True, ""), summary: bool = True) -> None:
        self.jobs: list[tuple] = []
        self.posted: list = []
        self.reviews: list = []
        self.handoffs: list = []
        self.notes: list = []
        self.unassigned_n = unassigned
        self.ready = ready
        self.summary = summary
        self.shutdown = False
        self.review_answer = True
        self.chain = ProcessingChain(
            submit=lambda rec, steps, on_done: self.jobs.append((rec, steps, on_done)),
            post=self.posted.append,
            review=self._review,
            handoff=self._handoff,
            handoff_ready=lambda: self.ready,
            notify=lambda t, m: self.notes.append((t, m)),
            unassigned=lambda rec: self.unassigned_n,
            shutting_down=lambda: self.shutdown,
            run_in_background=lambda fn: fn(),
        )

    def _review(self, rec):
        self.reviews.append(rec)
        self.unassigned_n = 0
        return self.review_answer

    def _handoff(self, rec, note):
        self.handoffs.append(note)
        return {"location": "SharePoint/x.md"}

    def finish_job(self, result=None, error=None):
        rec, steps, on_done = self.jobs[-1]
        if result is None and error is None:
            result = SimpleNamespace(summary_skipped="" if self.summary else "Ollama aus", summary_failed=not self.summary)
            if "summarize" in steps and self.summary:
                rec.summary_path.write_text("# Zusammenfassung\n\nInhalt", encoding="utf-8")
        on_done(SimpleNamespace(), result, error)

    def run_posted(self):
        while self.posted:
            self.posted.pop(0)()


@pytest.fixture
def rec(tmp_path: Path):
    return make_recording(tmp_path / "recs", with_parts=False)


def test_full_chain_with_review_summary_and_handoff(rec):
    w = World(unassigned=2)
    w.chain.start(rec, Plan(True, True, True, True), "dialog")
    assert chain_state(rec)["stage"] == STAGE_PROCESS and is_active(rec)
    assert w.jobs[0][1] == frozenset({"finalize", "transcribe", "render", "diarize", "identify"})  # no summary yet
    w.finish_job()
    assert chain_state(rec)["stage"] == STAGE_REVIEW and w.reviews == [] and len(w.posted) == 1
    w.run_posted()  # Tk thread: dialog opens and is answered
    assert w.reviews == [rec]
    assert w.jobs[-1][1] == frozenset({"summarize"})  # summary only after the names are assigned
    w.finish_job()
    assert w.handoffs == [{"authorized_by": "postcall_dialog", "preview_shown": False}]
    ch = chain_state(rec)
    assert ch["stage"] == DONE and ch["handoff_authorized"] is True and ch["authorized_by"] == "dialog"
    texts = [e["text"] for e in ch["log"]]
    assert texts[0].startswith("Gestartet") and "Sprecher zugeordnet" in texts and texts[-1] == "Fertig"


def test_review_skipped_when_all_assigned_or_skipped_by_user(rec):
    w = World(unassigned=0)
    w.chain.start(rec, Plan(True, True, True, False), "dialog")
    w.finish_job()
    assert w.posted == [] and w.jobs[-1][1] == frozenset({"summarize"})
    w2 = World(unassigned=1)
    w2.review_answer = False  # "Überspringen"
    w2.chain.start(rec, Plan(True, True, True, False), "dialog")
    w2.finish_job()
    w2.run_posted()
    assert "Sprecherzuordnung übersprungen" in [e["text"] for e in chain_state(rec)["log"]]
    w2.finish_job()
    assert chain_state(rec)["stage"] == DONE and w2.handoffs == []


def test_handoff_skipped_with_message_when_not_configured_or_no_summary(rec):
    w = World(ready=(False, "Es ist noch kein SharePoint-Übergabeordner eingestellt."))
    w.chain.start(rec, Plan(True, False, True, True), "dialog")
    assert w.jobs[0][1] == frozenset({"finalize", "transcribe", "render"})
    w.finish_job()
    w.finish_job()
    ch = chain_state(rec)
    assert w.handoffs == [] and ch["stage"] == DONE and "Übergabeordner" in ch["handoff_skipped"]
    assert any(t == "Übergabe übersprungen" for t, _ in w.notes)

    w2 = World(summary=False)
    w2.chain.start(rec, Plan(True, False, True, True), "dialog")
    w2.finish_job()
    w2.finish_job()
    ch = chain_state(rec)
    assert w2.handoffs == [] and ch["stage"] == DONE and "keine Zusammenfassung" in ch["handoff_skipped"]


def test_handoff_needs_the_users_choice(rec):
    w = World()
    w.chain.start(rec, Plan(True, False, True, True), "button")  # "Jetzt verarbeiten" never hands over
    assert chain_state(rec)["handoff_authorized"] is False
    w.finish_job()
    w.finish_job()
    assert w.handoffs == [] and chain_state(rec)["stage"] == DONE
    # without a summary there is no hand-over either (normalised plan)
    assert Plan(True, True, False, True).normalised().handoff is False
    assert Plan(False, True, True, True).normalised() == Plan(False, False, False, False)


def test_handoff_failure_does_not_fail_the_chain(rec):
    w = World()

    def broken(rec, note):
        raise OSError("OneDrive pausiert")

    w.chain.handoff = broken
    w.chain.start(rec, Plan(True, False, True, True), "setting")
    w.finish_job()
    w.finish_job()
    ch = chain_state(rec)
    assert ch["stage"] == DONE and "OneDrive" in ch["handoff_skipped"]


def test_cancel_and_failure(rec):
    from callrec.pipeline import ProcessingCancelled

    w = World()
    w.chain.start(rec, Plan(), "dialog")
    w.finish_job(error=ProcessingCancelled())
    assert chain_state(rec)["stage"] == CANCELLED and not is_active(rec)
    w.chain.start(rec, Plan(), "dialog")
    w.finish_job(error=RuntimeError("Whisper kaputt"))
    assert chain_state(rec)["stage"] == FAILED and "Whisper" in chain_state(rec)["error"]
    # cancelled between two stages (e.g. while the review dialog waits)
    w = World(unassigned=1)
    w.chain.start(rec, Plan(True, True, True, False), "dialog")
    w.finish_job()
    w.chain.cancel(rec)
    w.run_posted()
    assert w.reviews == [] and chain_state(rec)["stage"] == CANCELLED


def test_waiting_job_removed_from_queue_ends_the_chain(rec):
    from callrec.jobs import CANCELLED as JOB_CANCELLED

    w = World()
    w.chain.start(rec, Plan(), "dialog")
    w.chain.on_queue_event(SimpleNamespace(state=JOB_CANCELLED, recording=rec))
    assert chain_state(rec)["stage"] == CANCELLED


def test_app_exit_keeps_the_chain_resumable(rec, tmp_path: Path):
    from callrec.pipeline import ProcessingCancelled

    w = World(unassigned=1)
    w.chain.start(rec, Plan(True, True, True, True), "dialog")
    w.shutdown = True
    w.finish_job(error=ProcessingCancelled())
    assert chain_state(rec)["stage"] == STAGE_PROCESS and is_active(rec)
    assert [k for _, k in startup_work(tmp_path / "recs")] == ["resume"]
    w2 = World(unassigned=1)
    assert w2.chain.resume(rec) is True
    assert w2.jobs[0][1] == Plan(True, True, True, True).process_steps()
    w2.finish_job()
    w2.run_posted()
    w2.finish_job()
    assert len(w2.handoffs) == 1 and chain_state(rec)["stage"] == DONE  # authorisation kept from the question
    assert w2.chain.resume(rec) is False


def test_startup_work_and_deferred(tmp_path: Path):
    root = tmp_path / "recs"
    rec = make_recording(root, with_parts=False)
    rec.update_meta(["processing"], {})
    assert [k for _, k in startup_work(root)] == ["ask"]  # stopped, never decided
    mark_deferred(rec)
    assert startup_work(root) == [] and find_unprocessed(root) == []  # "Später": left alone
    rec.update_meta(["processing", "status"], "running")
    assert [k for _, k in startup_work(root)] == ["process"]  # older version's interrupted processing


def test_default_plan_and_config():
    cfg = Config()
    assert cfg.processing.after_recording == "ask" and not cfg.processing.handoff_without_asking
    assert default_plan(cfg) == Plan(True, True, True, False)
    cfg.summary.enabled = False
    assert default_plan(cfg).summary is False
    with pytest.raises(ConfigError):
        config_from_dict({"processing": {"after_recording": "immer"}})
    assert "Übergabe" not in " ".join(Plan(True, True, True, False).describe())
    assert any("SharePoint" in x for x in Plan(True, True, True, True).describe())


def test_preset_cannot_enable_the_handoff(tmp_path: Path):
    from callrec.config import apply_preset

    cfg = apply_preset(Config(), {"processing": {"after_recording": "full", "handoff_without_asking": True}}, "X")
    assert cfg.processing.after_recording == "ask" and cfg.processing.handoff_without_asking is False
    assert any("[processing]" in w for w in cfg.warnings)


def test_handoff_entry_records_the_authorisation(rec, tmp_path: Path):
    from callrec.handoff import FolderTarget, handoff_summary

    rec.summary_path.write_text("# Zusammenfassung\n\nInhalt", encoding="utf-8")
    out = tmp_path / "sp"
    out.mkdir()
    entry = handoff_summary(rec, FolderTarget(out), note={"authorized_by": "postcall_dialog", "preview_shown": False,
                                                          "sha256": "fake"})
    stored = rec.load_meta()["handoffs"][-1]
    assert stored["authorized_by"] == "postcall_dialog" and stored["preview_shown"] is False
    assert entry["sha256"] != "fake"  # a note can never overwrite the real fields
    assert [p.name for p in out.iterdir()] == [entry["file"]]


def test_library_status_and_next_action(tmp_path: Path):
    from callrec.library import read_info
    from callrec.ui.viewmodel import next_action

    root = tmp_path / "recs"
    rec = make_recording(root, with_parts=False)
    mark_deferred(rec)
    info = read_info(rec.folder)
    from callrec.library import status_description

    assert info.needs_processing and "Später" in status_description(info)
    assert next_action(info) == ("nicht verarbeitet", "Jetzt verarbeiten", "process_now")
    rec.update_meta(["processing", "chain"], {"stage": "review"})
    info = read_info(rec.folder)
    assert not info.needs_processing and next_action(info)[2] == "review"
    rec.update_meta(["processing", "chain"], {"stage": "done"})
    rec.transcript_path.write_text("x", encoding="utf-8")
    rec.update_meta(["processing", "status"], "done")
    rec.update_meta(["processing", "speakers"], {"spoken": 2, "unassigned": 1})
    assert next_action(read_info(rec.folder))[1] == "Sprecher zuordnen"
    rec.update_meta(["processing", "speakers"], {"spoken": 2, "unassigned": 0})
    assert next_action(read_info(rec.folder))[2] == "resummarize"
    rec.summary_path.write_text("# Zusammenfassung\n\nInhalt", encoding="utf-8")
    assert next_action(read_info(rec.folder))[1] == "Übergeben"
    assert next_action(read_info(rec.folder), busy=True)[1] == ""


def test_cancel_clicked_while_the_last_step_finishes_stops_the_chain(rec):
    # "Abbrechen" during the summary request: the request still finishes (job without error), but
    # the chain must not go on to the hand-over
    w = World()
    w.chain.start(rec, Plan(True, False, True, True), "dialog")
    w.finish_job()
    rec_, steps, on_done = w.jobs[-1]
    assert steps == frozenset({"summarize"})
    rec.summary_path.write_text("# Zusammenfassung\n\nInhalt", encoding="utf-8")
    on_done(SimpleNamespace(cancel_requested=True), SimpleNamespace(summary_skipped="", summary_failed=False), None)
    assert w.handoffs == [] and chain_state(rec)["stage"] == CANCELLED
    # same after the transcript: no summary job is started
    w2 = World()
    w2.chain.start(rec, Plan(True, False, True, False), "dialog")
    _r, _s, on_done = w2.jobs[-1]
    on_done(SimpleNamespace(cancel_requested=True), SimpleNamespace(summary_skipped="", summary_failed=False), None)
    assert len(w2.jobs) == 1 and chain_state(rec)["stage"] == CANCELLED


def test_step_finished_during_app_exit_waits_for_the_next_start(rec, tmp_path: Path):
    w = World()
    w.chain.start(rec, Plan(True, False, True, True), "dialog")
    w.shutdown = True
    _r, _s, on_done = w.jobs[-1]
    on_done(SimpleNamespace(cancel_requested=True), SimpleNamespace(summary_skipped="", summary_failed=False), None)
    assert len(w.jobs) == 1 and w.handoffs == []  # no cloud summary / hand-over while exiting
    assert chain_state(rec)["stage"] == "summary"
    assert [k for _, k in startup_work(tmp_path / "recs")] == ["resume"]


def test_setting_handoff_is_checked_again_at_the_handoff(rec):
    w = World()
    w.chain.auto_handoff_allowed = lambda: False  # switched off after the chain was started / before resume
    w.chain.start(rec, Plan(True, False, True, True), "setting")
    w.finish_job()
    w.finish_job()
    ch = chain_state(rec)
    assert w.handoffs == [] and ch["stage"] == DONE and "ausgeschaltet" in ch["handoff_skipped"]
    # the user's choice in the question is not affected by that setting
    w2 = World()
    w2.chain.auto_handoff_allowed = lambda: False
    w2.chain.start(rec, Plan(True, False, True, True), "dialog")
    w2.finish_job()
    w2.finish_job()
    assert len(w2.handoffs) == 1


def test_deferred_recording_with_unfinished_parts_is_not_asked_again(tmp_path: Path):
    from callrec.library import read_info

    root = tmp_path / "recs"
    rec = make_recording(root, with_parts=True)  # real case: "Später" leaves the .part.wav files
    assert [k for _, k in startup_work(root)] == ["ask"]
    mark_deferred(rec)
    assert startup_work(root) == []
    # a cancelled / failed chain is a decision, too
    rec.update_meta(["processing"], {"status": "cancelled", "chain": {"stage": CANCELLED}})
    assert startup_work(root) == []
    # interrupted by a crash (still "recording"), then "Später": "Jetzt verarbeiten" stays offered
    rec.update_meta(["processing"], {})
    rec.update_meta(["recording", "status"], "recording")
    assert [k for _, k in startup_work(root)] == ["ask"]
    mark_deferred(rec)
    assert startup_work(root) == [] and read_info(rec.folder).needs_processing
