"""Regression tests for defects found in the review of the main-window / language-detection range."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from callrec.jobs import CANCELLED, ProcessingQueue
from callrec.pipeline import ProcessingCancelled
from tests.test_language import DetectingTranscriber, _pipeline_with, w
from tests.test_pipeline import profiles, recording_with_voices  # noqa: F401


# -- language detection never fails the pipeline ---------------------------------------
class BrokenDetector(DetectingTranscriber):
    def __init__(self, result=None, exc: BaseException | None = None) -> None:
        super().__init__({})
        self.result, self.exc = result, exc

    def detect_languages(self, tracks, out_json, progress=lambda f: None):
        self.detected += 1
        if self.exc is not None:
            raise self.exc
        return self.result


@pytest.mark.parametrize("tr", [
    BrokenDetector(exc=RuntimeError("worker crashed in an unexpected way")),
    BrokenDetector(exc=KeyError("x")),
    BrokenDetector(result=["not", "a", "dict"]),  # garbage output
    BrokenDetector(result={"tracks": {"mic": {"windows": [{"probs": "garbage", "speech_s": 30}]}}}),
])
def test_failed_language_detection_falls_back_to_prior(tmp_path: Path, profiles, tr):  # noqa: F811
    rec = recording_with_voices(tmp_path / "r", 330, 550)
    pipeline = _pipeline_with(profiles, tr)
    result = pipeline.run(rec, {"finalize", "transcribe"})
    assert result.steps_done == ["finalize", "transcribe"]
    assert tr.languages == ["de", "de"]
    d = rec.load_meta()["processing"]["transcription"]["language_detection"]
    assert d["mode"] == "prior" and d["language"] == "de"


# -- cancellation inside the transcription step ------------------------------------------
def test_cancel_after_detection_does_not_start_the_track_workers(tmp_path: Path, profiles):  # noqa: F811
    english = [w(0, 0.05, 0.95)]
    flag = {"cancel": False}

    class CancelDuringDetection(DetectingTranscriber):
        def detect_languages(self, tracks, out_json, progress=lambda f: None):
            flag["cancel"] = True  # user clicks "Abbrechen" while the detection worker runs/ends
            return super().detect_languages(tracks, out_json, progress)

    tr = CancelDuringDetection({"mic": english, "remote": english})
    rec = recording_with_voices(tmp_path / "r", 330, 550)
    pipeline = _pipeline_with(profiles, tr)
    pipeline.run(rec, {"finalize"})
    with pytest.raises(ProcessingCancelled):
        pipeline.run(rec, {"transcribe"}, should_cancel=lambda: flag["cancel"])
    assert tr.detected == 1 and tr.languages == []  # no (long) transcription started after the cancel
    assert rec.load_meta()["processing"]["status"] == "cancelled"


def test_cancelled_retranscription_keeps_the_previous_result(tmp_path: Path, profiles):  # noqa: F811
    tr = DetectingTranscriber({})
    rec = recording_with_voices(tmp_path / "r", 330, 550)
    pipeline = _pipeline_with(profiles, tr)
    pipeline.run(rec)  # complete first processing
    before = {t: rec.asr_path(t).read_text(encoding="utf-8") for t in ("mic", "remote")}
    calls = {"n": 0}

    def should_cancel() -> bool:  # cancel once the mic track of the re-transcription is done
        return calls["n"] >= 1

    orig = tr.transcribe

    def counting(*a, **kw):
        res = orig(*a, **kw)
        calls["n"] += 1
        return res

    tr.transcribe = counting  # type: ignore[method-assign]
    with pytest.raises(ProcessingCancelled):
        pipeline.run(rec, {"transcribe", "identify", "render", "summarize"}, should_cancel=should_cancel)
    assert calls["n"] == 1
    # the previous ASR results are back in place, consistent with meta.json ...
    assert {t: rec.asr_path(t).read_text(encoding="utf-8") for t in ("mic", "remote")} == before
    # ... so rendering (rename, speaker review) still works on the cancelled recording
    pipeline.run(rec, {"render"})


# -- app exit during processing: resumed at the next start, not "cancelled" -------------
def test_shutdown_marks_running_job_interrupted_and_skips_waiting(tmp_path: Path):
    from callrec.recording import find_unprocessed
    from tests.conftest import make_recording

    started = threading.Event()
    seen: list[str] = []

    class SlowPipeline:
        def run(self, rec, steps, progress, should_cancel=None):
            seen.append(rec.folder.name)
            rec.update_meta(["processing", "status"], "running")
            started.set()
            while not should_cancel():
                time.sleep(0.01)
            rec.update_meta(["processing", "status"], "cancelled")
            raise ProcessingCancelled()

    a = make_recording(tmp_path / "a")
    b = make_recording(tmp_path / "b")
    q = ProcessingQueue(lambda **kw: SlowPipeline())
    ja = q.submit(a)
    q.submit(b)
    assert started.wait(5)
    q.shutdown()
    q.join(5)
    assert ja.state == CANCELLED and seen == [a.folder.name]  # b was not started during the exit
    assert a.load_meta()["processing"]["status"] == "running"
    assert [r.folder for r in find_unprocessed(tmp_path / "a")] == [a.folder]


def test_join_before_any_job_does_not_raise():
    ProcessingQueue(lambda **kw: None).join(0.1)


def test_cancel_model_downloads_kills_registered_processes():
    from callrec import cli

    killed: list[int] = []

    class Proc:
        def kill(self) -> None:
            killed.append(1)

    proc = Proc()
    cli._DOWNLOADS.add(proc)
    try:
        assert cli.cancel_model_downloads() == 1 and killed == [1]
    finally:
        cli._DOWNLOADS.discard(proc)


# -- opening files on Windows ----------------------------------------------------------------
def test_open_path_falls_back_when_no_default_application(tmp_path: Path):
    pytest.importorskip("tkinter")
    from callrec.ui.actions import open_path

    readme = tmp_path / "README.md"
    readme.write_text("x", encoding="utf-8")
    flac = tmp_path / "mix.flac"
    flac.write_bytes(b"x")
    started: list[list[str]] = []

    def no_association(path: str) -> None:
        raise OSError(1155, "Für diese Datei ist dieser Vorgang keine Anwendung zugeordnet")

    open_path(readme, startfile=no_association, popen=started.append)
    open_path(flac, startfile=no_association, popen=started.append)
    assert started == [["notepad.exe", str(readme)], ["explorer.exe", f"/select,{flac}"]]
    with pytest.raises(OSError):
        open_path(tmp_path / "missing-folder", startfile=no_association, popen=started.append)


# -- double click on "stop" ------------------------------------------------------------------
def test_toggle_right_after_stop_does_not_open_a_new_consent_dialog(tmp_path: Path, monkeypatch):
    from tests.test_controller import FakeUi, _controller, _decision

    ui = FakeUi(decision_factory=_decision)
    ctl, q = _controller(ui, tmp_path)
    ctl.toggle()
    assert ctl.is_recording and len(ui.asked) == 1
    ctl.toggle()  # stop
    ctl.toggle()  # second click of the double click / hotkey pressed twice
    assert not ctl.is_recording and len(ui.asked) == 1 and len(q.jobs) == 1
    monkeypatch.setattr(type(ctl), "TOGGLE_GUARD_S", 0.0)
    ctl.toggle()  # later: a new recording - through the consent dialog as always
    assert ctl.is_recording and len(ui.asked) == 2
    ctl.stop()
