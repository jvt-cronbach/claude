"""Short diarization fragments are never offered for naming; their text is kept (0.3.1)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from callrec.models import Person, SpeakerInfo, SpeakerTurn
from callrec.review import Assignment, apply_assignments, set_participants, speaker_overview
from callrec.speakers.fragments import minor_ids, plan_fragments
from callrec.speakers.profiles import PlainFilePersistence, ProfileStore
from callrec.speakers.samples import TARGET_MAX_S, TARGET_MIN_S, mic_samples, remote_samples
from callrec.transcript import speaker_lists
from tests.test_pipeline import make_pipeline, recording_with_voices, short_cfg


def frag_cfg(min_named: float = 2.5):
    """The fake diarizer: S1 1.0-2.9 s (1.9 s), S2 3.0-5.9 s (2.9 s)."""
    cfg = short_cfg()
    cfg.speakers.min_named_speech_s = min_named
    return cfg


def store(tmp_path: Path) -> ProfileStore:
    return ProfileStore(PlainFilePersistence(tmp_path / "vp.json"))


def test_minor_ids_respect_decisions():
    turns = [SpeakerTurn(0, 10, "S1"), SpeakerTurn(10, 12, "S2"), SpeakerTurn(13, 14, "S3")]
    assert minor_ids(turns, 5.0) == {"S2", "S3"}
    assert minor_ids(turns, 5.0, {"S3"}) == {"S2"}
    assert minor_ids(turns, 0.0) == set()


def _sp(**minor: bool) -> dict[str, SpeakerInfo]:
    out = {"ME": SpeakerInfo("ME", "Ich", "Ich", source="self")}
    for i, sid in enumerate(["S1", "S2", "S3"], 1):
        out[sid] = SpeakerInfo(sid, f"Sprecher {i}", minor=minor.get(sid, False))
    return out


def test_plan_fragments_similarity_adjacency_and_unclear():
    turns = [SpeakerTurn(0, 20, "S1"), SpeakerTurn(30, 50, "S2"),
             SpeakerTurn(20.4, 21.0, "S3"),  # right after S1
             SpeakerTurn(25.0, 25.5, "S3"),  # alone in the middle
             SpeakerTurn(29.5, 30.2, "S3")]  # touches S2 only
    emb = {"S1": np.array([1.0, 0, 0]), "S2": np.array([0, 1.0, 0]), "S3": np.array([0, 0, 1.0])}
    plan = plan_fragments(turns, _sp(S3=True), emb, threshold=0.55, min_speech_s=5.0)
    got = [(t.start, t.speaker) for t in plan.turns if t.start in (20.4, 25.0, 29.5)]
    # several voices and no voice evidence: the position alone does not name anybody
    assert got == [(20.4, "S3"), (25.0, "S3"), (29.5, "S3")]
    rec = plan.record["speakers"]["S3"]
    assert rec["attributed"] == {} and rec["unclear_s"] == 1.8
    assert any("gleichzeitig" in e["why"] for e in plan.record["turns"])  # 29.5-30.2 overlaps S2
    # only one voice on the far side: right next to its turn -> that voice; overlapping it -> unclear
    one = [SpeakerTurn(0, 20, "S1"), SpeakerTurn(20.4, 21.0, "S3"), SpeakerTurn(10.0, 10.4, "S3")]
    plan = plan_fragments(one, _sp(S3=True), {}, threshold=0.55, min_speech_s=5.0)
    assert {t.start: t.speaker for t in plan.turns if t.start in (20.4, 10.0)} == {20.4: "S1", 10.0: "S3"}
    # a voice that is clearly one of them wins over the position
    emb["S3"] = np.array([0.1, 0.99, 0])
    plan = plan_fragments(turns, _sp(S3=True), emb, threshold=0.55, min_speech_s=5.0)
    assert {t.speaker for t in plan.turns if t.start in (20.4, 25.0, 29.5)} == {"S2"}
    # a voice about as close to both people is no evidence for either
    emb["S3"] = np.array([0.7, 0.66, 0])
    plan = plan_fragments(turns, _sp(S3=True), emb, threshold=0.55, min_speech_s=5.0, margin=0.08)
    assert {t.speaker for t in plan.turns if t.start in (20.4, 25.0, 29.5)} == {"S3"}
    # between two people and no voice evidence: unclear
    turns2 = [SpeakerTurn(0, 10, "S1"), SpeakerTurn(10.6, 11.0, "S3"), SpeakerTurn(11.5, 20, "S2")]
    plan = plan_fragments(turns2, _sp(S3=True), {}, threshold=0.55, min_speech_s=5.0)
    assert [t.speaker for t in plan.turns if t.start == 10.6] == ["S3"]
    assert "mehreren" in plan.record["turns"][0]["why"]
    # nothing is dropped
    assert len(plan.turns) == len(turns2)


def test_unclear_fragment_is_labelled_and_never_open():
    sp = _sp(S3=True)
    assert sp["S3"].display == "Unklar"
    from callrec.models import Utterance

    utts = [Utterance(0, 1, "S1", "a", "remote"), Utterance(2, 3, "S3", "hm", "remote")]
    _spoken, unassigned, silent = speaker_lists(utts, sp)
    assert [s.id for s in unassigned] == ["S1"] and "S3" not in [s.id for s in silent]


def test_pipeline_hides_fragment_attributes_text_and_records_it(tmp_path: Path):
    rec = recording_with_voices(tmp_path / "r", 330, 550)
    profiles = store(tmp_path)
    pipeline, _, summ = make_pipeline(profiles, cfg=frag_cfg())
    res = pipeline.run(rec)
    sp = rec.load_speakers()
    assert sp["S1"].minor and not sp["S1"].name and "kurzes Fragment" in sp["S1"].note
    assert not sp["S2"].minor
    assert res.unassigned_speakers == 1  # only Sprecher 2 is "open"
    assert rec.load_meta()["processing"]["speakers"]["unassigned"] == 1
    md = rec.transcript_path.read_text(encoding="utf-8")
    assert "Sprecher 1" not in md and "Hallo, erste Stimme." in md and "Und zweite Stimme." in md  # nothing lost
    assert "Sprecher 2:** Hallo, erste Stimme. Und zweite Stimme." in md  # adjacent: attributed
    frag = rec.load_meta()["processing"]["fragments"]
    assert frag["speakers"]["S1"]["attributed"] == {"S2": 1.9}
    assert rec.load_meta()["processing"]["identification"]["fragments"] == ["S1"]
    assert not any("Sprecher 1" in line for line in summ.seen[-1])
    # never matched / learned
    assert all(c.speaker != "S1" for p in profiles.all_profiles() for c in p.contributions)
    ov = {o.id: o for o in speaker_overview(rec)}
    assert ov["S1"].minor and not ov["S2"].minor and ov["S1"].sample_text  # revealable, with its words


def test_naming_a_revealed_fragment_keeps_it_named_without_profile(tmp_path: Path):
    rec = recording_with_voices(tmp_path / "r", 330, 550)
    profiles = store(tmp_path)
    pipeline, _, _ = make_pipeline(profiles, cfg=frag_cfg())
    pipeline.run(rec)
    # an untouched hidden fragment sent with "" stays a fragment
    apply_assignments(rec, [Assignment("S1", "")], profiles, lambda: None)
    assert rec.load_speakers()["S1"].minor
    msgs = apply_assignments(rec, [Assignment("S1", "Anna Beispiel", save_profile=True)], profiles,
                             lambda: pipeline.run(rec, {"render"}), min_learn_speech_s=0)
    sp = rec.load_speakers()
    assert sp["S1"].name == "Anna Beispiel" and not sp["S1"].minor and sp["S1"].source == "manual"
    assert any("kurzes Fragment" in m for m in msgs) and profiles.find("Anna Beispiel", "", "fake-embedder") is None
    assert "Anna Beispiel:** Hallo, erste Stimme." in rec.transcript_path.read_text(encoding="utf-8")
    pipeline.run(rec, {"identify", "render"})  # a re-check keeps the manual name
    assert rec.load_speakers()["S1"].name == "Anna Beispiel"


def test_recheck_of_an_existing_recording_applies_the_rule(tmp_path: Path):
    rec = recording_with_voices(tmp_path / "r", 330, 550)
    profiles = store(tmp_path)
    old, _, _ = make_pipeline(profiles, cfg=frag_cfg(0.0))  # as 0.3.0 did it: both voices open
    assert old.run(rec).unassigned_speakers == 2
    new, _, _ = make_pipeline(profiles, cfg=frag_cfg())
    changes = new.preview_identification(rec)
    assert any(label == "Sprecher 1" and "Fragment" in after for label, _b, after in changes)
    new.run(rec, {"identify", "render"})
    assert rec.load_meta()["processing"]["speakers"]["unassigned"] == 1


def test_fragments_do_not_count_for_the_participant_bound_or_the_sole_rule(tmp_path: Path):
    rec = recording_with_voices(tmp_path / "r", 330, 550)
    set_participants(rec, [Person("Ich Selbst", "ich@firma.de"), Person("Kunde, Karl", "karl@kunde.de")])
    pipeline, _, _ = make_pipeline(store(tmp_path), cfg=frag_cfg())
    res = pipeline.run(rec)
    sp = rec.load_speakers()
    bound = rec.load_meta()["processing"]["diarization"]["bound"]
    assert bound["fragments"] == ["S1"] and not bound["merged"]  # not merged into Karl's voice
    named = [s for sid, s in sp.items() if sid != "ME" and s.name]
    assert [s.name for s in named] == ["Kunde, Karl"] and res.unassigned_speakers == 0


def test_samples_prefer_long_clean_stretches_and_cycle():
    turns = [SpeakerTurn(0, 40, "S1"), SpeakerTurn(40, 42, "S2"), SpeakerTurn(10, 11, "S2")]
    mic = [(20.0, 21.0)]  # you talk at 20 s
    smp = remote_samples(turns, "S1", mic)
    assert smp and all(TARGET_MIN_S <= s.duration <= TARGET_MAX_S + 0.6 for s in smp)
    for s in smp:  # never over another speaker or you
        for a, b in s.pieces:
            assert not (a < 11 and b > 10.0 + 0.3) and not (a < 21 and b > 20.0 + 0.3)
    assert smp[0].count == len(smp) >= 2 and smp[0].pieces != smp[1].pieces


def test_samples_concatenate_short_turns_when_needed():
    turns = [SpeakerTurn(i * 10.0, i * 10.0 + 3.0, "S1") for i in range(6)] + \
        [SpeakerTurn(i * 10.0 + 5, i * 10.0 + 8, "S2") for i in range(6)]
    smp = remote_samples(turns, "S1", [])
    assert len(smp[0].pieces) >= 3 and smp[0].duration >= TARGET_MIN_S
    assert mic_samples([(0.0, 2.0)], turns)  # always something for your own track


def test_clip_store_writes_cycles_and_cleans_up(tmp_path: Path):
    import soundfile as sf

    from callrec.speakers.samples import Sample
    from callrec.ui.clips import ClipStore

    folder = tmp_path / "rec"
    (folder / "audio").mkdir(parents=True)
    sr = 16000
    for name in ("mic.flac", "remote.flac"):
        sf.write(str(folder / "audio" / name), np.zeros(sr * 30, dtype="float32"), sr)
    removed: list[Path] = []
    store_ = ClipStore(on_remove=removed.append)
    samples = [Sample([(0.0, 9.0)], 0, 2), Sample([(10.0, 12.0), (14.0, 16.0)], 1, 2)]
    c1 = store_.make(folder, "remote", samples, ("r", "S1"))
    c2 = store_.make(folder, "remote", samples, ("r", "S1"), step=1)
    c3 = store_.make(folder, "remote", samples, ("r", "S1"), step=1)
    assert (c1.index, c2.index, c3.index) == (0, 1, 0)
    assert abs(sf.info(str(c2.path)).duration - 4.3) < 0.01  # two pieces + gap
    assert "Beispiel 2 von 2" in c2.caption(61.0) and "01:01" in c2.caption(61.0)
    for _ in range(5):
        store_.make(folder, "remote", samples, ("r", "S1"))
    assert removed and not removed[0].exists()
    d = store_.folder
    store_.close()
    assert not d.exists()


def test_stale_clip_folders_of_a_crash_are_removed(tmp_path: Path):
    import os
    import time

    from callrec.ui.clips import STALE_S, remove_stale_clip_folders

    old = tmp_path / "callrec_clips_abc"
    old.mkdir()
    (old / "x.wav").write_bytes(b"RIFF")
    fresh = tmp_path / "callrec_clips_new"
    fresh.mkdir()
    other = tmp_path / "something_else"
    other.mkdir()
    past = time.time() - STALE_S - 60
    os.utime(old, (past, past))
    os.utime(other, (past, past))
    assert remove_stale_clip_folders(tmp_path) == 1
    assert not old.exists() and fresh.exists() and other.exists()
