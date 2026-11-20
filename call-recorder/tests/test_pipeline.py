from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np
import pytest

from callrec.audio.capture import MIC_PART, REMOTE_PART
from callrec.audio.finalize import load_mono
from callrec.config import Config
from callrec.jobs import ProcessingQueue
from callrec.models import AsrResult, AsrSegment, SpeakerTurn, Word
from callrec.pipeline import ALL_STEPS, Components, Pipeline, initial_prompt_for
from callrec.recording import Recording, find_incomplete, find_unprocessed, recordings_to_resume
from callrec.review import (
    Assignment,
    apply_assignments,
    name_choices,
    parse_cli_assignments,
    speaker_overview,
)
from callrec.speakers.diarize import renumber_turns
from callrec.speakers.profiles import PlainFilePersistence, ProfileStore
from callrec.summary import SummaryUnavailable
from tests.conftest import make_recording, tone, write_wav

SR_REMOTE = 44100


def remote_audio(first: float, second: float) -> np.ndarray:
    return np.concatenate([tone(first, 3.0, SR_REMOTE), tone(second, 3.0, SR_REMOTE)])


class FakeTranscriber:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def transcribe(self, audio: Path, out_json: Path, *, initial_prompt: str = "", hotwords: str = "", progress=lambda f: None) -> AsrResult:
        self.calls.append((audio.name, initial_prompt))
        self.hotwords = hotwords
        progress(0.5)
        progress(1.0)
        if audio.name.startswith("mic"):
            segs = [AsrSegment(0.2, 1.0, "Hallo zusammen.", [Word(0.2, 0.6, " Hallo"), Word(0.6, 1.0, " zusammen.")]),
                    AsrSegment(6.1, 6.8, "Danke.", [Word(6.1, 6.8, " Danke.")])]
        else:
            segs = [AsrSegment(1.2, 2.5, "Hallo, hier ist die erste Stimme.", [Word(1.2, 1.8, " Hallo,"), Word(1.8, 2.5, " erste Stimme.")]),
                    AsrSegment(3.2, 5.5, "Und hier die zweite Stimme.", [Word(3.2, 4.0, " Und"), Word(4.0, 5.5, " zweite Stimme.")])]
        return AsrResult("de", 6.0, segs, "fake-large", "cuda", "float16")


class FakeDiarizer:
    def diarize(self, samples, sr, progress):
        assert sr == 16000
        progress(1.0)
        return renumber_turns([SpeakerTurn(1.0, 2.9, "raw7"), SpeakerTurn(3.0, 5.9, "raw3")])


class FakeEmbedder:
    """Embedding = dominant frequency bucket (so identical 'voices' match)."""

    model_id = "fake-embedder"

    def embed(self, samples, sr):
        spec = np.abs(np.fft.rfft(samples))
        freq = np.argmax(spec) * sr / len(samples)
        v = np.full(16, 0.01, dtype=np.float32)
        v[int(freq // 50) % 16] = 1.0
        return v


class FakeSummarizer:
    model_name = "fake-llm"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.seen: list[list[str]] = []

    def summarize(self, title, participants, lines, date=""):
        if self.fail:
            raise SummaryUnavailable("Ollama ist nicht erreichbar unter http://127.0.0.1:11434")
        self.seen.append(lines)
        return "## Kurzfassung\nTest.\n\n## Aufgaben\n| Aufgabe | Verantwortlich | Frist |\n|---|---|---|\n| X | Anna Beispiel | - |"


@pytest.fixture
def profiles(tmp_path: Path) -> ProfileStore:
    return ProfileStore(PlainFilePersistence(tmp_path / "vp.json"))


def make_pipeline(profiles: ProfileStore, summarizer: FakeSummarizer | None = None, cfg: Config | None = None):
    tr = FakeTranscriber()
    summ = summarizer or FakeSummarizer()
    comps = Components(
        transcriber_factory=lambda: tr,
        diarizer_factory=FakeDiarizer,
        embedder_factory=FakeEmbedder,
        profiles=profiles,
        summarizer_factory=lambda: summ,
        audio_loader=load_mono,
    )
    return Pipeline(cfg or short_cfg(), comps), tr, summ


def short_cfg() -> Config:
    """The fake calls are a few seconds long: no minimum speech time for matching/learning."""
    cfg = Config()
    cfg.speakers.min_match_speech_s = 0.0
    cfg.speakers.min_learn_speech_s = 0.0
    cfg.speakers.min_named_speech_s = 0.0  # the fake voices speak 2-3 s: no fragments here
    return cfg


def recording_with_voices(root: Path, first: float, second: float) -> Recording:
    rec = make_recording(root)
    write_wav(rec.folder / "audio" / REMOTE_PART, remote_audio(first, second), SR_REMOTE)
    return rec


def test_full_pipeline_then_review_then_auto_recognition(tmp_path: Path, profiles: ProfileStore):
    rec = recording_with_voices(tmp_path / "r1", 330, 550)
    pipeline, tr, summ = make_pipeline(profiles)
    progress: list[tuple[str, float]] = []
    result = pipeline.run(rec, ALL_STEPS, lambda s, f, m: progress.append((s, f)))

    assert result.steps_done == ["finalize", "transcribe", "diarize", "identify", "render", "summarize"]
    assert ("transcribe", 0.25) in progress and ("summarize", 1.0) in progress
    assert (rec.folder / "audio" / "mic.flac").exists() and (rec.folder / "audio" / "mix.flac").exists()
    assert not (rec.folder / "audio" / MIC_PART).exists()
    assert tr.calls[0][1].startswith("Gespräch mit Ich Selbst, Olga Organisatorin")
    md = rec.transcript_path.read_text(encoding="utf-8")
    assert "**[00:00:00] Ich:** Hallo zusammen." in md  # first name of "Ich Selbst"
    assert "**[00:00:01] Sprecher 1:** Hallo, erste Stimme." in md
    assert "**[00:00:03] Sprecher 2:** Und zweite Stimme." in md
    assert md.index("Ich:** Hallo") < md.index("Sprecher 1:") < md.index("Sprecher 2:") < md.index("Danke.")
    assert rec.summary_path.read_text(encoding="utf-8").startswith("# Zusammenfassung: Projekt Jour fixe")
    assert result.unassigned_speakers == 2
    meta = rec.load_meta()
    assert meta["processing"]["transcription"]["mic"]["device"] == "cuda"
    assert meta["processing"]["diarization"]["speakers"] == 2
    assert meta["processing"]["summary"]["model"] == "fake-llm"
    assert meta["processing"]["status"] == "done"
    assert meta["consent"]["confirmed"] is True and meta["consent"]["confirmed_at"]
    assert meta["audio"]["files"]["remote"] == "remote.flac"

    # --- review: assign the unknown voices -----------------------------------
    ov = speaker_overview(rec)
    assert [o.label for o in ov] == ["Sprecher 1", "Sprecher 2"]
    assert ov[0].sample_span is not None and ov[0].sample_text
    choices = name_choices(rec, profiles)
    assert "Anna Beispiel" in choices and "Ich Selbst" not in choices
    msgs = apply_assignments(
        rec, [Assignment("S1", "Anna Beispiel"), Assignment("S2", "Bernd Muster")], profiles,
        lambda: pipeline.run(rec, {"render"}), min_learn_speech_s=0,
    )
    assert len(msgs) == 2 and len(profiles.all_profiles()) == 2
    assert profiles.find("Anna Beispiel", "", "fake-embedder").email == "anna@firma.de"  # type: ignore[union-attr]
    md = rec.transcript_path.read_text(encoding="utf-8")
    assert "Anna Beispiel:** Hallo, erste Stimme." in md and "Bernd Muster:** Und zweite Stimme." in md
    assert "noch nicht zugeordnet" not in md

    # --- next call: same voices in reverse order are recognised automatically -
    rec2 = recording_with_voices(tmp_path / "r2", 550, 330)
    pipeline2, _, _ = make_pipeline(profiles)
    result2 = pipeline2.run(rec2)
    assert result2.unassigned_speakers == 0
    sp = rec2.load_speakers()
    assert sp["S1"].name == "Bernd Muster" and sp["S1"].source == "auto"
    assert sp["S2"].name == "Anna Beispiel" and sp["S2"].score is not None
    assert profiles.find("Anna Beispiel", "", "fake-embedder").samples == 2  # type: ignore[union-attr]  # auto-updated


def test_summary_skipped_then_rerun(tmp_path: Path, profiles: ProfileStore):
    rec = recording_with_voices(tmp_path / "r", 330, 550)
    pipeline, _, _ = make_pipeline(profiles, FakeSummarizer(fail=True))
    result = pipeline.run(rec)
    assert "nicht erreichbar" in result.summary_skipped
    assert not rec.summary_path.exists()
    assert rec.transcript_path.exists()
    assert "skipped" in rec.load_meta()["processing"]["summary"]
    ok = FakeSummarizer()
    pipeline2, tr2, _ = make_pipeline(profiles, ok)
    r2 = pipeline2.run(rec, {"summarize"})
    assert r2.steps_done == ["summarize"] and rec.summary_path.exists()
    assert tr2.calls == []  # nothing re-transcribed
    assert any("Sprecher 1" in line for line in ok.seen[0])


def test_summary_disabled(tmp_path: Path, profiles: ProfileStore):
    cfg = Config()
    cfg.summary.enabled = False
    rec = recording_with_voices(tmp_path / "r", 330, 550)
    pipeline, _, _ = make_pipeline(profiles, cfg=cfg)
    assert pipeline.run(rec).summary_skipped == "Zusammenfassung ist deaktiviert."


def test_rerun_steps_and_errors(tmp_path: Path, profiles: ProfileStore):
    rec = make_recording(tmp_path / "r", with_parts=False)
    pipeline, _, _ = make_pipeline(profiles)
    with pytest.raises(FileNotFoundError):
        pipeline.run(rec, {"finalize"})
    assert rec.load_meta()["processing"]["status"].startswith("error")
    with pytest.raises(ValueError):
        pipeline.run(rec, {"dance"})


def test_silent_remote_track(tmp_path: Path, profiles: ProfileStore):
    rec = make_recording(tmp_path / "r")
    write_wav(rec.folder / "audio" / REMOTE_PART, np.zeros(SR_REMOTE * 6, dtype=np.float32), SR_REMOTE)
    pipeline, _, _ = make_pipeline(profiles)
    result = pipeline.run(rec)
    assert any("stumm" in m for m in result.messages)
    # without diarization the remote side is labelled as one speaker
    assert "Gegenseite" in rec.transcript_path.read_text(encoding="utf-8")


def test_manual_assignments_survive_retranscribe_and_rediarize(tmp_path: Path, profiles: ProfileStore):
    rec = recording_with_voices(tmp_path / "r", 330, 550)
    pipeline, _, _ = make_pipeline(profiles)
    pipeline.run(rec)
    apply_assignments(rec, [Assignment("S1", "Anna Beispiel", save_profile=False)], profiles, lambda: pipeline.run(rec, {"render"}))
    assert rec.load_speakers()["S1"].name == "Anna Beispiel"
    pipeline.run(rec, {"transcribe", "identify", "render"})  # re-transcription keeps the diarization
    assert rec.load_speakers()["S1"].source == "manual"
    result = pipeline.run(rec, {"diarize", "identify", "render"})  # same clusters again -> carried over
    sp = rec.load_speakers()
    assert sp["S1"].name == "Anna Beispiel" and sp["S1"].source == "manual" and sp["S1"].label == "Sprecher 1"
    assert sp["S2"].name == ""
    assert any("übernommen" in m for m in result.messages)
    assert "Anna Beispiel:** Hallo, erste Stimme." in rec.transcript_path.read_text(encoding="utf-8")


def test_crash_recovery_detection(tmp_path: Path, profiles: ProfileStore):
    root = tmp_path / "recs"
    rec = make_recording(root)
    rec.update_meta(["recording", "status"], "recording")
    assert [r.folder for r in find_incomplete(root)] == [rec.folder]
    pipeline, _, _ = make_pipeline(profiles)
    result = pipeline.run(rec)
    assert "wiederhergestellt" in " ".join(result.messages)
    assert rec.load_meta()["recording"]["status"] == "recovered"
    assert find_incomplete(root) == []
    rec.update_meta(["processing", "status"], "running")
    assert [r.folder for r in find_unprocessed(root)] == [rec.folder]


def test_stopped_but_unconverted_recording_is_resumed_once(tmp_path: Path):
    root = tmp_path / "recs"
    rec = make_recording(root)  # WAV parts still present
    rec.update_meta(["recording", "status"], "stopped")  # app closed before 'finalize' ran
    assert find_incomplete(root) and find_unprocessed(root)  # matches both lists ...
    assert [(r.folder, i) for r, i in recordings_to_resume(root)] == [(rec.folder, True)]  # ... queued once
    rec.update_meta(["recording", "status"], "failed")  # audio device could not be opened
    assert recordings_to_resume(root) == []  # not "recovered" as a 0-second call at next start


def test_embeddings_roundtrip_and_delete(tmp_path: Path):
    rec = make_recording(tmp_path / "r", with_parts=False)
    rec.save_embeddings({"S1": np.ones(4), "S2": np.zeros(4)}, "m1")
    embs, model = rec.load_embeddings()
    assert model == "m1" and set(embs) == {"S1", "S2"}
    assert rec.delete_embeddings() and rec.load_embeddings() == ({}, "")


def test_cli_assignment_parsing():
    a = parse_cli_assignments(["S1=Anna Beispiel", "Sprecher 2 = Bernd", "S3="], no_profile=True)
    assert [(x.speaker_id, x.name, x.save_profile) for x in a] == [("S1", "Anna Beispiel", False), ("S2", "Bernd", False), ("S3", "", False)]
    with pytest.raises(ValueError):
        parse_cli_assignments(["Anna"])


def test_initial_prompt():
    from callrec.models import Person

    assert initial_prompt_for([Person("a@b.de", "a@b.de"), Person("Anna")], Person("Ich")) == "Gespräch mit Ich, Anna."
    assert initial_prompt_for([], Person("")) == ""


def test_processing_queue(tmp_path: Path, profiles: ProfileStore):
    rec = recording_with_voices(tmp_path / "r", 330, 550)
    statuses: list[str] = []
    done = threading.Event()
    seen = {}

    def on_done(job, result, error):
        seen["result"], seen["error"] = result, error
        done.set()

    q = ProcessingQueue(lambda: make_pipeline(profiles)[0], status=statuses.append)
    q.submit(rec, {"finalize", "transcribe"}, on_done=on_done)
    assert done.wait(20)
    assert seen["error"] is None and seen["result"].steps_done == ["finalize", "transcribe"]
    assert any("Transkription" in s for s in statuses) and statuses[-1] == ""
    q.stop()
    q.join(5)
    json.loads(rec.asr_path("remote").read_text(encoding="utf-8"))
