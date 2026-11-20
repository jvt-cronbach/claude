"""Speaker identification restricted to the participants, profile provenance, own-voice handling."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from callrec.config import Config, apply_preset, load_config
from callrec.models import AsrResult, AsrSegment, Person, SpeakerInfo, SpeakerTurn, Word
from callrec.review import (
    SELF_CHOICE,
    Assignment,
    apply_assignments,
    name_choices,
    parse_choice,
    rebuild_profile,
    row_choices,
    self_overview,
    set_participants,
    speaker_overview,
    suggestion_label,
)
from callrec.speakers import names, threads, trackcheck
from callrec.speakers.assign import assign_remote
from callrec.speakers.diarize import clean_intervals, speech_seconds
from callrec.speakers.profiles import PlainFilePersistence, ProfileStore, only_legacy_unconfirmed
from tests.conftest import make_recording
from tests.test_pipeline import make_pipeline, recording_with_voices, short_cfg

DIM = 16


def onehot(i: int, noise: float = 0.0, seed: int = 0) -> np.ndarray:
    v = np.full(DIM, 0.01, dtype=np.float32)
    v[i] = 1.0
    if noise:
        v = v + np.random.default_rng(seed).normal(scale=noise, size=DIM).astype(np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture
def store(tmp_path: Path) -> ProfileStore:
    return ProfileStore(PlainFilePersistence(tmp_path / "vp.json"))


# -- names ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("a,b", [
    (("Müller, Anna", ""), ("Anna Mueller", "")),
    (("Dr. Anna Müller (extern)", ""), ("anna müller", "")),
    (("anna.mueller@firma.de", "anna.mueller@firma.de"), ("Anna Müller", "")),
    (("Hans-Peter Schmidt", ""), ("Schmidt, Hans Peter", "")),
    (("Prof. Dr.-Ing. José García", ""), ("Jose Garcia", "")),
    (("Anna Müller", "ANNA@firma.de"), ("A. Müller", "anna@firma.de")),
])
def test_same_person_variants(a, b):
    assert names.same_person(a[0], a[1], b[0], b[1])


@pytest.mark.parametrize("a,b", [
    (("Anna Müller", "anna@firma.de"), ("Anna Müller", "anna.mueller@extern.de")),  # addresses decide
    (("Anna Müller", ""), ("Anna Meier", "")),
    (("Anna", ""), ("Anna Müller", "")),
    (("", ""), ("", "")),
])
def test_different_people(a, b):
    assert not names.same_person(a[0], a[1], b[0], b[1])


def test_first_name():
    assert names.first_name("van Treeck, Joost") == "Joost"
    assert names.first_name("Dr. Anna Müller") == "Anna"
    assert names.first_name("joost.vantreeck@firma.de") == "Joost"
    assert names.first_name("") == ""


# -- matching ------------------------------------------------------------------------------------
def test_only_participants_are_assigned_others_are_suggestions(store: ProfileStore):
    store.add_sample("Anna Beispiel", "anna@firma.de", onehot(1), "m")
    store.add_sample("Karl Fremd", "karl@extern.de", onehot(2), "m")
    emb = {"S1": onehot(1, 0.05, 1), "S2": onehot(2, 0.05, 2)}
    dec = store.decide(emb, "m", participants=[Person("Müller, Anna", "anna@firma.de")], threshold=0.55, margin=0.08)
    assert dec["S1"].match is not None and dec["S1"].match.profile.name == "Anna Beispiel"
    assert dec["S2"].match is None and dec["S2"].suggestion is not None
    assert dec["S2"].suggestion.profile.name == "Karl Fremd" and "kein Teilnehmer" in dec["S2"].reason
    # a participant that sounds less like the voice than a non-participant is not assigned either
    dec2 = store.decide({"S1": onehot(2, 0.05, 3)}, "m", participants=[Person("Anna Beispiel", "anna@firma.de")],
                        threshold=0.0, margin=0.0)
    assert dec2["S1"].match is None and dec2["S1"].suggestion.profile.name == "Karl Fremd"  # type: ignore[union-attr]


def test_margin_and_not_eligible(store: ProfileStore):
    a = onehot(1)
    b = a + 0.15 * onehot(2)
    store.add_sample("Anna", "", a, "m")
    store.add_sample("Berta", "", b / np.linalg.norm(b), "m")
    both = [Person("Anna"), Person("Berta")]
    dec = store.decide({"S1": a}, "m", participants=both, threshold=0.5, margin=0.08)
    assert dec["S1"].match is None and "nicht eindeutig" in dec["S1"].reason
    assert store.decide({"S1": a}, "m", participants=both, threshold=0.5, margin=0.0)["S1"].match is not None
    dec = store.decide({"S1": a}, "m", participants=both, threshold=0.5, not_eligible={"S1": "zu wenig Sprache"})
    assert dec["S1"].match is None and dec["S1"].reason == "zu wenig Sprache"


# -- provenance ----------------------------------------------------------------------------------
def test_contributions_replace_undo_and_reset(store: ProfileStore, tmp_path: Path):
    store.learn("Anna", "", onehot(1), "m", recording="r1", speaker="S1", source="manual", seconds=30)
    p = store.learn("Anna", "", onehot(1, 0.1), "m", recording="r1", speaker="S1", source="manual", seconds=30)
    assert len(p.contributions) == 1  # the same recording+speaker is never counted twice
    store.learn("Anna", "", onehot(1, 0.1, 5), "m", recording="r2", speaker="S2", source="auto")
    assert len(store.find("Anna", "", "m").contributions) == 2  # type: ignore[union-attr]
    with pytest.raises(ValueError):  # an automatic match never creates a profile
        store.learn("Neu", "", onehot(3), "m", recording="r2", speaker="S3", source="auto")
    # correction: r1/S1 was Bernd, not Anna -> Anna loses that sample, Bernd gets it
    store.learn("Bernd", "", onehot(1), "m", recording="r1", speaker="S1", source="manual")
    anna = store.find("Anna", "", "m")
    assert anna is not None and [c.recording for c in anna.contributions] == ["r2"]
    assert store.reset_to_confirmed(anna.id) is None  # only automatic left -> deleted
    assert store.find("Anna", "", "m") is None
    assert [p.name for p in ProfileStore(PlainFilePersistence(tmp_path / "vp.json")).all_profiles()] == ["Bernd"]
    assert store.forget("r1", "S1") == ["Bernd"] and store.all_profiles() == []


def test_legacy_profile_is_readable_and_resettable(tmp_path: Path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"version": 1, "profiles": [{
        "id": "x1", "name": "Alt", "email": "", "model_id": "m", "embedding": onehot(4).tolist(), "samples": 7,
        "created_at": "", "updated_at": ""}]}), encoding="utf-8")
    s = ProfileStore(PlainFilePersistence(path))
    p = s.get("x1")
    assert p is not None and p.samples == 7 and p.contributions[0].source == "legacy"
    p2 = s.learn("Alt", "", onehot(4, 0.1), "m", recording="r", speaker="S1", source="manual")
    assert p2.samples == 8 and np.dot(p2.embedding, onehot(4)) > 0.9
    assert s.reset_to_confirmed("x1") is not None and [c.source for c in s.get("x1").contributions] == ["manual"]  # type: ignore[union-attr]
    # a profile of an earlier version with nothing confirmed since is not deleted by "Zurücksetzen"
    path.write_text(json.dumps({"version": 1, "profiles": [{
        "id": "x2", "name": "Nur Alt", "email": "", "model_id": "m", "embedding": onehot(5).tolist(), "samples": 3,
        "created_at": "", "updated_at": ""}]}), encoding="utf-8")
    s = ProfileStore(PlainFilePersistence(path))
    s.learn("Nur Alt", "", onehot(5, 0.1), "m", recording="r", speaker="S1", source="auto")
    left = s.reset_to_confirmed("x2")
    assert left is not None and only_legacy_unconfirmed(left) and len(left.contributions) == 2
    assert ProfileStore(PlainFilePersistence(path)).get("x2") is not None


# -- pipeline ------------------------------------------------------------------------------------
def _two_known_voices(tmp_path: Path, profiles: ProfileStore):
    rec = recording_with_voices(tmp_path / "r1", 330, 550)
    pipeline, _tr, _s = make_pipeline(profiles)
    pipeline.run(rec)
    apply_assignments(rec, [Assignment("S1", "Anna Beispiel"), Assignment("S2", "Bernd Muster")], profiles,
                      lambda: pipeline.run(rec, {"render"}), min_learn_speech_s=0)
    return rec


def test_previous_call_people_are_not_assigned_without_being_participants(tmp_path: Path, store: ProfileStore):
    _two_known_voices(tmp_path, store)
    rec2 = recording_with_voices(tmp_path / "r2", 550, 330)
    set_participants(rec2, [Person("Olga Organisatorin", "olga@firma.de"), Person("Anna Beispiel", "anna@firma.de")])
    pipeline, _, _ = make_pipeline(store)
    pipeline.run(rec2)
    sp = rec2.load_speakers()
    assert sp["S2"].name == "Anna Beispiel" and sp["S2"].source == "auto"
    assert sp["S1"].name == "" and sp["S1"].suggested_name == "Bernd Muster"  # suggestion only
    ov = {o.id: o for o in speaker_overview(rec2)}
    choices = row_choices(rec2, store, ov["S1"])
    assert choices[0] == "Olga Organisatorin"  # not yet assigned participants first
    assert suggestion_label("Bernd Muster") in choices and choices[-1] == SELF_CHOICE
    assert "Bernd Muster" not in name_choices(rec2, store)
    # picking the suggestion makes Bernd a participant of this recording
    apply_assignments(rec2, [Assignment("S1", suggestion_label("Bernd Muster"), save_profile=False)], store,
                      lambda: pipeline.run(rec2, {"render"}))
    assert any(p.name == "Bernd Muster" for p in rec2.participants())
    assert rec2.load_speakers()["S1"].source == "manual"


def test_no_participant_list_assigns_nothing(tmp_path: Path, store: ProfileStore):
    _two_known_voices(tmp_path, store)
    rec2 = recording_with_voices(tmp_path / "r2", 550, 330)
    set_participants(rec2, [])
    pipeline, _, _ = make_pipeline(store)
    res = pipeline.run(rec2)
    assert res.unassigned_speakers == 2
    assert "keine Teilnehmerliste" in rec2.load_speakers()["S1"].note
    # relaxed mode (config.toml only): earlier behaviour
    cfg = short_cfg()
    cfg.speakers.match_scope = "all_profiles"
    pipeline2, _, _ = make_pipeline(store, cfg=cfg)
    pipeline2.run(rec2, {"identify", "render"})
    assert rec2.load_speakers()["S1"].name == "Bernd Muster"


def test_participants_edit_reidentifies_and_manual_wins(tmp_path: Path, store: ProfileStore):
    _two_known_voices(tmp_path, store)
    rec2 = recording_with_voices(tmp_path / "r2", 550, 330)
    set_participants(rec2, [])
    pipeline, _, _ = make_pipeline(store)
    pipeline.run(rec2)
    apply_assignments(rec2, [Assignment("S2", "Carla Neu", save_profile=False)], store,
                      lambda: pipeline.run(rec2, {"render"}))
    set_participants(rec2, rec2.participants() + [Person("Bernd Muster", "bernd@firma.de"),
                                                  Person("Anna Beispiel", "anna@firma.de")])
    assert pipeline.preview_identification(rec2) == [("Sprecher 1", "Sprecher 1", "Bernd Muster")]
    pipeline.run(rec2, {"identify", "render"})
    sp = rec2.load_speakers()
    assert sp["S1"].name == "Bernd Muster" and sp["S2"].name == "Carla Neu" and sp["S2"].source == "manual"
    meta = rec2.load_meta()["processing"]
    assert meta["identification"]["rule"] == "participants-v2" and "identify_s" in meta["identification"]["timing"]
    assert {"diarize_s", "embed_s", "total_s", "rtf"} <= set(meta["diarization"]["timing"])


def test_auto_learning_is_replaced_not_repeated_and_corrections_undo(tmp_path: Path, store: ProfileStore):
    _two_known_voices(tmp_path, store)
    rec2 = recording_with_voices(tmp_path / "r2", 550, 330)
    pipeline, _, _ = make_pipeline(store)
    pipeline.run(rec2)
    for _ in range(3):  # re-running the identification never adds the same recording again
        pipeline.run(rec2, {"identify", "render"})
    anna = store.find("Anna Beispiel", "", "fake-embedder")
    assert anna is not None and [c.source for c in anna.contributions] == ["manual", "auto"]
    # the user corrects S2: not Anna -> Anna forgets what S2 taught her
    apply_assignments(rec2, [Assignment("S2", "Dora Dritte", save_profile=False)], store,
                      lambda: pipeline.run(rec2, {"render"}))
    assert [c.source for c in store.find("Anna Beispiel", "", "fake-embedder").contributions] == ["manual"]  # type: ignore[union-attr]


def test_unchanged_auto_match_is_not_confirmed_silently(tmp_path: Path, store: ProfileStore):
    _two_known_voices(tmp_path, store)
    rec2 = recording_with_voices(tmp_path / "r2", 550, 330)
    cfg = short_cfg()
    cfg.speakers.auto_update_on_match = False
    pipeline, _, _ = make_pipeline(store, cfg=cfg)
    pipeline.run(rec2)
    sp = rec2.load_speakers()
    apply_assignments(rec2, [Assignment(sid, sp[sid].name, save_profile=False) for sid in ("S1", "S2")], store,
                      lambda: pipeline.run(rec2, {"render"}))
    assert {s.source for sid, s in rec2.load_speakers().items() if sid != "ME"} == {"auto"}
    assert all(len(p.contributions) == 1 for p in store.all_profiles())
    apply_assignments(rec2, [Assignment("S1", sp["S1"].name, save_profile=True)], store,
                      lambda: pipeline.run(rec2, {"render"}), min_learn_speech_s=0)
    bernd = store.find("Bernd Muster", "", "fake-embedder")
    assert [c.source for c in bernd.contributions] == ["manual", "confirmed"]  # type: ignore[union-attr]


def test_minimum_speech_blocks_matching_and_learning(tmp_path: Path, store: ProfileStore):
    rec = _two_known_voices(tmp_path, store)
    rec2 = recording_with_voices(tmp_path / "r2", 550, 330)
    cfg = Config()  # defaults: 8 s / 20 s, the fake voices speak ~2-3 s
    cfg.speakers.min_named_speech_s = 0.0  # (fragments: see test_fragments.py)
    pipeline, _, _ = make_pipeline(store, cfg=cfg)
    pipeline.run(rec2)
    assert all(not s.name for sid, s in rec2.load_speakers().items() if sid != "ME")
    assert "zu wenig Sprache" in rec2.load_speakers()["S1"].note
    msgs = apply_assignments(rec, [Assignment("S1", "Olga Organisatorin")], store, lambda: None)
    assert any("zu wenig Sprache" in m for m in msgs) and store.find("Olga Organisatorin", "", "fake-embedder") is None


def test_rebuild_profile_from_manual_assignments(tmp_path: Path, store: ProfileStore):
    rec = _two_known_voices(tmp_path / "recs", store)
    anna = store.find("Anna Beispiel", "", "fake-embedder")
    assert anna is not None
    store.learn("Anna Beispiel", "", onehot(9), "fake-embedder", recording="elsewhere", speaker="S1", source="manual")
    msg = rebuild_profile(store, anna.id, tmp_path / "recs" / "r1")
    assert "1 Zuordnung" in msg
    assert [c.folder for c in store.get(anna.id).contributions] == [rec.folder.name]  # type: ignore[union-attr]


# -- own voice -----------------------------------------------------------------------------------
def test_self_row_name_and_summary_use_the_display_name(tmp_path: Path, store: ProfileStore, isolated_home: Path):
    rec = recording_with_voices(tmp_path / "r1", 330, 550)
    pipeline, _, summ = make_pipeline(store)
    pipeline.cfg.transcript.self_display_name = ""
    (isolated_home / "data").mkdir(parents=True, exist_ok=True)
    (isolated_home / "data" / "me.json").write_text(json.dumps(
        {"name": "van Treeck, Joost", "email": "j@firma.de", "given_name": ""}), encoding="utf-8")
    pipeline.run(rec)
    md = rec.transcript_path.read_text(encoding="utf-8")
    assert "**[00:00:00] Joost:** Hallo zusammen." in md
    assert "- **Eigene Spur:** Joost (Mikrofonspur, nicht in der Sprecherzuordnung): 2 Beiträge" in md
    assert any(line.endswith("Joost: Hallo zusammen.") for line in summ.seen[-1])
    me = self_overview(rec)
    assert me.name == "Joost" and me.utterances == 2 and me.sample_span is not None
    pipeline.cfg.transcript.self_display_name = "J. (ich)"
    pipeline.run(rec, {"render"})  # no new transcription needed
    assert "J. (ich):** Hallo zusammen." in rec.transcript_path.read_text(encoding="utf-8")


def test_das_bin_ich_moves_text_to_me_without_duplicates():
    remote = AsrResult("de", 10, [
        AsrSegment(1.0, 2.0, "Hallo", [Word(1.0, 2.0, " Hallo")]),  # sidetone copy of the mic
        AsrSegment(5.0, 6.0, "Nur hier", [Word(5.0, 6.0, " Nur hier")]),  # mic missed it
        AsrSegment(7.0, 8.0, "Gegenseite", [Word(7.0, 8.0, " Gegenseite")]),
    ])
    turns = [SpeakerTurn(0.5, 6.5, "S2"), SpeakerTurn(6.8, 8.5, "S1")]
    out = assign_remote(remote, turns, self_ids={"S2"}, self_speaker="ME", mic_words=[Word(1.1, 1.9, " Hallo")])
    assert [(u.speaker, u.text) for u in out] == [("ME", "Nur hier"), ("S1", "Gegenseite")]


def test_das_bin_ich_assignment(tmp_path: Path, store: ProfileStore):
    rec = recording_with_voices(tmp_path / "r1", 330, 550)
    pipeline, _, _ = make_pipeline(store)
    pipeline.run(rec)
    assert parse_choice(SELF_CHOICE) == ("", True)
    apply_assignments(rec, [Assignment("S2", SELF_CHOICE)], store, lambda: pipeline.run(rec, {"render"}))
    assert rec.load_speakers()["S2"].source == "self_echo" and store.all_profiles() == []
    md = rec.transcript_path.read_text(encoding="utf-8")
    assert "Ich:** Und zweite Stimme." in md and "Sprecher 2" not in md.split("---", 1)[1]
    pipeline.run(rec, {"identify", "render"})
    assert rec.load_speakers()["S2"].source == "self_echo"  # survives like a manual assignment


def _asr(words: list[tuple[float, str]]) -> AsrResult:
    return AsrResult("de", 600, [AsrSegment(t, t + 0.8, w, [Word(t, t + 0.8, " " + w)]) for t, w in words])


def test_trackcheck_little_speech_echo_and_own_voice_on_remote():
    remote_words = [(i * 2.0, f"wort{i}") for i in range(100)]  # 100 segments = 80 s speech
    rep = trackcheck.check(_asr([(3.0, "jawohl")]), _asr(remote_words), [], {}, None)
    assert rep.warnings and "kaum Sprache" in rep.warnings[0]
    rep = trackcheck.check(_asr([(t + 0.2, w) for t, w in remote_words]), _asr(remote_words), [], {}, None,
                           devices={"mic": {"name": "Headset (Jabra Hands-Free AG Audio)", "sample_rate": 16000}})
    assert rep.mic_unreliable and any("Echo" in w and "Freisprech" in w for w in rep.warnings)
    # the user's words also appear on the remote track, in cluster S2
    mic = [(300 + i * 2.0, f"mein{i}") for i in range(30)]
    remote = _asr(remote_words + [(t + 0.1, w) for t, w in mic])
    turns = [SpeakerTurn(0, 200, "S1"), SpeakerTurn(299, 400, "S2")]
    rep = trackcheck.check(_asr(mic), remote, turns, {}, None)
    assert set(rep.self_suspects) == {"S2"} and "Sprecher 2" in rep.warnings[-1] and len(rep.warnings) == 1
    # partial leakage of the other side into the microphone
    rep = trackcheck.check(_asr(mic + [(t + 0.2, w) for t, w in remote_words[:30]]), _asr(remote_words),
                           [SpeakerTurn(0, 200, "S1")], {}, None)
    assert not rep.mic_unreliable and "Teil der Gegenseite" in rep.warnings[0]
    # embedding similarity alone also flags it
    rep = trackcheck.check(_asr(mic), _asr(remote_words), [SpeakerTurn(0, 200, "S1")], {"S1": onehot(3)}, onehot(3))
    assert "S1" in rep.self_suspects


def test_envelope_correlation():
    rng = np.random.default_rng(0)
    sr = 1000
    env = np.repeat(rng.random(800) > 0.3, int(0.1 * sr)).astype(np.float32)  # 80 s of on/off "speech"
    speech = env * rng.normal(scale=0.2, size=env.size).astype(np.float32)
    echo = np.concatenate([np.zeros(100, np.float32), 0.3 * speech[:-100]])  # 100 ms later, quieter
    other = np.roll(speech, 40000) * 0.5
    corr, lag = trackcheck.envelope_correlation(trackcheck.envelope(echo, sr), trackcheck.envelope(speech, sr))
    assert corr > 0.9 and lag == pytest.approx(0.1, abs=0.03)
    assert trackcheck.envelope_correlation(trackcheck.envelope(other, sr), trackcheck.envelope(speech, sr))[0] < 0.6
    tone = np.sin(np.arange(40 * sr) / 3.0).astype(np.float32)
    assert trackcheck.envelope_correlation(trackcheck.envelope(tone, sr), trackcheck.envelope(tone, sr))[0] == 0.0


def test_clean_intervals_exclude_overlap():
    turns = [SpeakerTurn(0, 10, "S1"), SpeakerTurn(8, 12, "S2"), SpeakerTurn(11, 20, "S1")]
    iv = clean_intervals(turns)
    assert iv["S1"] == [(0, 8), (12, 20)] and iv["S2"] == [(10, 11)]
    assert speech_seconds(turns) == {"S1": 16.0, "S2": 1.0}


def test_self_is_excluded_even_with_name_variant(tmp_path: Path, store: ProfileStore):
    rec = recording_with_voices(tmp_path / "r1", 330, 550)
    store.add_sample("Selbst, Ich", "", np.eye(16, dtype=np.float32)[6], "fake-embedder")  # 330 Hz bucket
    pipeline, _, _ = make_pipeline(store)
    cfg = pipeline.cfg
    cfg.speakers.match_scope = "all_profiles"
    pipeline.run(rec)
    assert rec.load_speakers()["S1"].name == ""


# -- threads / config ----------------------------------------------------------------------------
def test_auto_threads():
    assert threads.auto_threads(2, recording=False) == 1
    assert threads.auto_threads(6, recording=False) == 5
    assert threads.auto_threads(16, recording=False) == 8
    assert threads.auto_threads(8, recording=True) == 3
    assert threads.auto_threads(2, recording=True) == 1
    assert threads.effective_threads(3, cores=16) == 3
    threads.set_recording_active(True)
    try:
        assert threads.effective_threads(0, cores=16) == 4 and "Aufnahme läuft" in threads.describe(0)
    finally:
        threads.set_recording_active(False)


def test_legacy_thread_default_and_preset_cannot_relax_matching(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text("[diarization]\nnum_threads = 2\n[speakers]\nmatch_threshold = 0.6\n", encoding="utf-8")
    assert load_config(p, preset="").diarization.num_threads == 0  # old template default -> automatic
    p.write_text("[diarization]\nnum_threads = 2\n[speakers]\nmatch_scope = \"participants\"\n", encoding="utf-8")
    assert load_config(p, preset="").diarization.num_threads == 2  # written by this version: deliberate
    cfg = apply_preset(Config(), {"speakers": {"match_scope": "all_profiles", "match_margin": 0.1}}, "x")
    assert cfg.speakers.match_scope == "participants" and cfg.speakers.match_margin == 0.1
    assert any("match_scope" in w for w in cfg.warnings)


def test_old_identification_hint_and_capture_warning_in_library(tmp_path: Path):
    from callrec.library import read_info, status_description

    rec = make_recording(tmp_path, with_parts=False)
    rec.save_speakers({"ME": SpeakerInfo("ME", "Ich", source="self"),
                       "S1": SpeakerInfo("S1", "Sprecher 1", name="Karl", source="auto")})
    assert "Sprecherzuordnung neu prüfen" in status_description(read_info(rec.folder))
    rec.update_meta(["processing", "identification"], {"rule": "participants-v2",
                                                       "trackcheck": {"warnings": ["Mikro kaputt"]}})
    text = status_description(read_info(rec.folder))
    assert "neu prüfen" not in text and "Mikrofon prüfen: Mikro kaputt" in text


# -- speaker count bounded by the participants ----------------------------------------------------
def test_two_participant_call_has_one_remote_speaker_named_after_the_participant(tmp_path: Path, store: ProfileStore):
    _two_known_voices(tmp_path, store)  # profiles of Anna and Bernd from an earlier call
    rec = recording_with_voices(tmp_path / "r2", 550, 330)  # the diarizer finds two clusters
    set_participants(rec, [Person("Ich Selbst", "ich@firma.de"), Person("Kunde, Karl", "karl@kunde.de")])
    pipeline, _, summ = make_pipeline(store)
    res = pipeline.run(rec)
    sp = rec.load_speakers()
    remote = {sid: s for sid, s in sp.items() if sid != "ME"}
    assert list(remote) == ["S1"] and remote["S1"].name == "Kunde, Karl" and remote["S1"].source == "auto"
    assert res.unassigned_speakers == 0 and any("begrenzt" in m for m in res.messages)
    md = rec.transcript_path.read_text(encoding="utf-8")
    assert "Anna Beispiel" not in md and "Bernd Muster" not in md and "Sprecher 2" not in md
    assert any("Kunde, Karl:" in line for line in summ.seen[-1])
    meta = rec.load_meta()["processing"]["diarization"]["bound"]
    assert meta["bound"] == 1 and meta["raw_speakers"] == 2 and meta["speakers"] == 1
    # the two merged clusters sound exactly like Bernd and Anna: named Karl, but Karl's profile learns nothing
    assert store.find("Karl Kunde", "karl@kunde.de", "fake-embedder") is None
    assert "Stimmprofil nicht aktualisiert" in remote["S1"].note


def test_sole_participant_learns_only_a_voice_that_is_plausibly_theirs(tmp_path: Path, store: ProfileStore):
    _two_known_voices(tmp_path, store)  # Anna (330 Hz) and Bernd (550 Hz)
    karl_rec = recording_with_voices(tmp_path / "k1", 760, 770)  # one unknown voice, split in two clusters
    set_participants(karl_rec, [Person("Kunde, Karl", "karl@kunde.de")])
    pipeline, _, _ = make_pipeline(store)
    pipeline.run(karl_rec)
    karl = store.find("Karl Kunde", "karl@kunde.de", "fake-embedder")  # min_learn_speech_s = 0 in the fake
    assert karl is not None and [c.source for c in karl.contributions] == ["sole"]
    # "Weitere Sprecher zulassen": back to the unbounded diarization (cached, no audio)
    karl_rec.update_meta(["speaker_limit"], {"lifted": True})
    pipeline.run(karl_rec, {"identify", "render"})
    assert {sid for sid in karl_rec.load_speakers() if sid != "ME"} == {"S1", "S2"}
    assert store.find("Karl Kunde", "karl@kunde.de", "fake-embedder") is None  # the "sole" sample is undone
    karl_rec.update_meta(["speaker_limit"], {"lifted": False})
    pipeline.run(karl_rec, {"identify", "render"})
    assert store.find("Karl Kunde", "karl@kunde.de", "fake-embedder") is not None
    # Karl invited, but his colleague Bernd came instead: named Karl (the invitation), Karl's profile untouched
    sub = recording_with_voices(tmp_path / "k2", 550, 560)
    set_participants(sub, [Person("Kunde, Karl", "karl@kunde.de")])
    pipeline.run(sub)
    s1 = sub.load_speakers()["S1"]
    assert s1.name == "Kunde, Karl" and "Stimme passt nicht zum Stimmprofil" in s1.note
    assert all(c.recording != sub.uid for c in store.find("Karl Kunde", "karl@kunde.de", "fake-embedder").contributions)  # type: ignore[union-attr]


def test_bound_is_participants_minus_one_and_no_list_keeps_the_count():
    from callrec.speakers.bound import merge_to_bound

    turns = [SpeakerTurn(i * 10.0, i * 10.0 + 9, f"S{i + 1}") for i in range(5)]
    emb = {f"S{i + 1}": onehot(i % 3, 0.05, i) for i in range(5)}
    speech = {k: 9.0 for k in emb}
    for n_participants in (2, 3, 4):  # incl. the user
        res = merge_to_bound(turns, emb, speech, n_participants - 1)
        assert len({t.speaker for t in res.turns}) <= n_participants - 1
    res = merge_to_bound(turns, emb, speech, 3)
    assert res.mapping["S1"] == res.mapping["S4"] and res.mapping["S2"] == res.mapping["S5"]  # same voices merge
    assert len({t.speaker for t in merge_to_bound(turns, emb, speech, None).turns}) == 5
    # clusters you named differently are never merged; your own voice is not counted
    res = merge_to_bound(turns, emb, speech, 1, keep_apart={"S1": "Anna", "S4": "Bernd"}, exclude={"S5"})
    assert res.mapping["S1"] != res.mapping["S4"] and res.mapping["S5"] not in (res.mapping["S1"], res.mapping["S4"])


def test_no_participants_keeps_automatic_speaker_count(tmp_path: Path, store: ProfileStore):
    rec = recording_with_voices(tmp_path / "r", 550, 330)
    set_participants(rec, [])
    pipeline, _, _ = make_pipeline(store)
    pipeline.run(rec)
    assert {sid for sid in rec.load_speakers() if sid != "ME"} == {"S1", "S2"}


def test_recheck_fixes_an_old_recording_with_foreign_names(tmp_path: Path, store: ProfileStore):
    _two_known_voices(tmp_path, store)
    rec = recording_with_voices(tmp_path / "r2", 550, 330)
    set_participants(rec, [Person("Kunde, Karl", "karl@kunde.de")])
    pipeline, _, _ = make_pipeline(store)
    pipeline.run(rec)
    # simulate the state an earlier version left: two clusters auto-assigned to people of earlier calls
    import json as _json

    raw = _json.loads(rec.raw_turns_path.read_text(encoding="utf-8"))
    rec.turns_path.write_text(_json.dumps(raw), encoding="utf-8")
    rec.raw_turns_path.unlink()
    rec.raw_embeddings_path.unlink()
    emb, model = rec.load_embeddings()
    rec.save_speakers({"ME": SpeakerInfo("ME", "Ich", name="Ich", source="self"),
                       "S1": SpeakerInfo("S1", "Sprecher 1", name="Bernd Muster", source="auto", score=0.6),
                       "S2": SpeakerInfo("S2", "Sprecher 2", name="Anna Beispiel", source="auto", score=0.6)})
    from callrec.speakers.diarize import speaker_embeddings  # noqa: F401  (embeddings stay cached)

    rec.save_embeddings({"S1": onehot(11), "S2": onehot(6)}, "fake-embedder")
    rec.update_meta(["processing", "identification"], None)
    changes = pipeline.preview_identification(rec)
    assert ("Sprecher 1", "Bernd Muster", "Kunde, Karl") in changes
    assert any(c[0] == "Sprecher 2" and c[1] == "Anna Beispiel" and "zusammengelegt" in c[2] for c in changes)
    pipeline.run(rec, {"identify", "render"})
    assert {s.name for sid, s in rec.load_speakers().items() if sid != "ME"} == {"Kunde, Karl"}
    assert rec.load_raw_diarization() is not None  # the original diarization is kept for "Weitere Sprecher"
    from callrec.library import read_info, status_description

    assert "alten Sprechernamen" in status_description(read_info(rec.folder))
    pipeline.run(rec, {"summarize"})
    assert "alten Sprechernamen" not in status_description(read_info(rec.folder))


def test_only_remaining_participant_is_preselected(tmp_path: Path, store: ProfileStore):
    _two_known_voices(tmp_path, store)
    rec = recording_with_voices(tmp_path / "r2", 550, 330)
    set_participants(rec, [Person("Bernd Muster", "bernd@firma.de"), Person("Neu, Nora")])
    pipeline, _, _ = make_pipeline(store)
    pipeline.run(rec)
    sp = rec.load_speakers()
    assert sp["S1"].name == "Bernd Muster"
    assert sp["S2"].name == "" and sp["S2"].preselect_name == "Neu, Nora"
    assert sp["S2"].suggested_name == "Anna Beispiel"  # the voice suggestion stays visible in the list


def test_old_manual_assignments_of_non_participants_can_be_reset(tmp_path: Path, store: ProfileStore):
    from callrec.review import foreign_old_assignments, reset_speakers

    rec = recording_with_voices(tmp_path / "r", 550, 330)
    pipeline, _, _ = make_pipeline(store)
    pipeline.run(rec)
    sp = rec.load_speakers()
    sp["S1"].name, sp["S1"].source = "Fremd, Franz", "manual"
    sp["S2"].name, sp["S2"].source = "Anna Beispiel", "manual"  # a participant: stays
    rec.save_speakers(sp)
    assert foreign_old_assignments(rec, pipeline._participants(rec)) == []  # identified by this version
    rec.update_meta(["processing", "identification"], None)
    foreign = foreign_old_assignments(rec, pipeline._participants(rec))
    assert foreign == [("S1", "Sprecher 1", "Fremd, Franz")]
    reset_speakers(rec, ["S1"], store)
    pipeline.run(rec, {"identify", "render"})
    sp = rec.load_speakers()
    assert sp["S1"].name == "" and sp["S2"].name == "Anna Beispiel"


# -- summary prompt --------------------------------------------------------------------------------
def test_prompt_defaults_leave_out_private_talk_and_upgrade_unchanged_files(tmp_path: Path):
    from callrec import userfiles as uf
    from callrec.summary import missing_sections

    assert "Smalltalk und Privates" in uf.SUMMARY_SYSTEM_TEMPLATE and uf.PRIVATE_CALL_LINE in uf.SUMMARY_FINAL_TEMPLATE
    d = tmp_path / "prompts"
    d.mkdir()
    prev_sys = uf._PREVIOUS_DEFAULTS["summary_system.txt"][0]
    assert "Smalltalk" not in prev_sys
    (d / "summary_system.txt").write_text(prev_sys, encoding="utf-8")  # unchanged earlier default
    (d / "summary_final.txt").write_text("Mein eigenes Format\n{material}\n", encoding="utf-8")  # customised
    uf._ensure_prompt(d, "summary_system.txt", uf.SUMMARY_SYSTEM_TEMPLATE)
    uf._ensure_prompt(d, "summary_final.txt", uf.SUMMARY_FINAL_TEMPLATE)
    assert (d / "summary_system.txt").read_text(encoding="utf-8") == uf.SUMMARY_SYSTEM_TEMPLATE
    assert (d / "summary_final.txt").read_text(encoding="utf-8") == "Mein eigenes Format\n{material}\n"
    assert missing_sections(uf.PRIVATE_CALL_LINE) == []
    assert uf.validate_prompt("final", uf.SUMMARY_FINAL_TEMPLATE) == []
    warn = uf.validate_prompt("final", "Fasse zusammen: {transkript}")
    assert any("{material}" in w for w in warn) and any("{transkript}" in w for w in warn)
    assert uf.validate_prompt("chunk", "Teil {i}") and uf.default_prompt("chunk") == uf.SUMMARY_CHUNK_TEMPLATE


def test_you_listed_under_another_address_are_not_a_remote_participant(tmp_path: Path, store: ProfileStore,
                                                                       isolated_home: Path):
    """The invitation lists you under an alias (or the UPN), /me and the recording know another address."""
    rec = recording_with_voices(tmp_path / "r", 550, 330)
    rec.update_meta(["self"], Person("Joost van Treeck", "jvt@firma.de", "self").to_dict())
    set_participants(rec, [Person("van Treeck, Joost", "joost.vantreeck@firma.de", "organizer"),
                           Person("Kunde, Karl", "karl@kunde.de")])
    pipeline, _, _ = make_pipeline(store)
    assert [p.name for p in pipeline._participants(rec)] == ["Kunde, Karl"]
    pipeline.run(rec)
    remote = {sid: s for sid, s in rec.load_speakers().items() if sid != "ME"}
    assert list(remote) == ["S1"] and remote["S1"].name == "Kunde, Karl"
    assert name_choices(rec, store) == ["Kunde, Karl"]
    # different people with the same surname or a shared first name are still told apart
    assert not names.same_identity_loose(Person("Anna Müller", "anna@a.de"), Person("Bernd Müller", "bernd@a.de"))
    assert not names.same_identity_loose(Person("Anna", "anna@a.de"), Person("Anna", "anna@b.de"))
    assert names.same_identity_loose(Person("Joost van Treeck", ""), Person("", "joost.van.treeck@firma.de"))


def test_sole_participant_without_profile_whose_voice_is_somebody_known(tmp_path: Path, store: ProfileStore):
    _two_known_voices(tmp_path, store)
    rec = recording_with_voices(tmp_path / "n", 550, 560)  # sounds like Bernd
    set_participants(rec, [Person("Neu, Nora", "nora@kunde.de")])
    pipeline, _, _ = make_pipeline(store)
    pipeline.run(rec)
    s1 = rec.load_speakers()["S1"]
    assert s1.name == "Neu, Nora" and "klingt wie Bernd Muster" in s1.note
    assert store.find("Nora Neu", "nora@kunde.de", "fake-embedder") is None


def test_limit_never_files_a_different_voice_under_your_decision():
    from callrec.speakers.bound import merge_to_bound

    turns = [SpeakerTurn(0, 50, "S1"), SpeakerTurn(50, 55, "S2"), SpeakerTurn(55, 60, "S3")]
    emb = {"S1": onehot(1), "S2": onehot(2), "S3": onehot(2, 0.05, 1)}
    speech = {"S1": 50.0, "S2": 5.0, "S3": 5.0}
    # S2 = Petra (your assignment, a short contribution of an uninvited guest), bound 1 (only Karl invited)
    res = merge_to_bound(turns, emb, speech, 1, keep_apart={"S2": "Petra"}, named_merge_min=0.55)
    assert res.mapping["S2"] == res.mapping["S3"] != res.mapping["S1"]  # similar fragment joins, Karl stays apart


def test_manual_assignment_of_an_uninvited_guest_survives_the_participant_limit(tmp_path: Path, store: ProfileStore):
    rec = recording_with_voices(tmp_path / "g", 550, 760)
    set_participants(rec, [Person("Kunde, Karl", "karl@kunde.de")])
    rec.update_meta(["speaker_limit"], {"lifted": True})
    pipeline, _, _ = make_pipeline(store)
    pipeline.run(rec)
    apply_assignments(rec, [Assignment("S2", "Petra Gast", save_profile=False)], store,
                      lambda: pipeline.run(rec, {"render"}), min_learn_speech_s=0)
    set_participants(rec, [Person("Kunde, Karl", "karl@kunde.de")])  # Petra removed again
    rec.update_meta(["speaker_limit"], {"lifted": False})
    pipeline.run(rec, {"identify", "render"})
    remote = {sid: s for sid, s in rec.load_speakers().items() if sid != "ME"}
    assert len(remote) == 2 and [s.name for s in remote.values()].count("Petra Gast") == 1  # Karl's voice is not hers
    karl_sid = next(sid for sid, s in remote.items() if s.name != "Petra Gast")
    assert remote[karl_sid].preselect_name == "Kunde, Karl"


def test_profiles_sharpened_by_an_earlier_version_are_named(tmp_path: Path, store: ProfileStore):
    from callrec.review import legacy_profile_updates

    rec = recording_with_voices(tmp_path / "r", 550, 330)
    rec.save_speakers({"ME": SpeakerInfo("ME", "Ich", name="Ich", source="self"),
                       "S1": SpeakerInfo("S1", "Sprecher 1", name="Bernd Muster", source="auto", score=0.93),
                       "S2": SpeakerInfo("S2", "Sprecher 2", name="Anna Beispiel", source="auto", score=0.62)})
    assert legacy_profile_updates(rec) == ["Bernd Muster"]  # 0.62 was below the old threshold of 0.70
    rec.update_meta(["processing", "identification"], {"rule": "participants-v2"})
    assert legacy_profile_updates(rec) == []


def test_prompt_editor_warns_when_a_section_heading_is_removed(tmp_path: Path):
    from callrec.library import read_info, status_description
    from callrec.userfiles import default_prompt, validate_prompt

    assert validate_prompt("final", default_prompt("final")) == []
    edited = default_prompt("final").replace("## Offene Fragen", "## Sonstiges")
    assert any("Offene Fragen" in w for w in validate_prompt("final", edited))
    rec = recording_with_voices(tmp_path / "r", 550, 330)
    rec.summary_path.write_text("# x\n", encoding="utf-8")
    rec.update_meta(["processing", "summary"], {"missing_sections": ["Offene Fragen"]})
    assert "fehlen Abschnitte (Offene Fragen)" in status_description(read_info(rec.folder))


def test_config_of_an_earlier_version_with_strict_matching_still_loads(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text("[speakers]\nmatch_threshold = 0.72\ninvitee_bonus = 0.05\nauto_update_on_match = true\n"
                 "auto_update_threshold = 0.70\n\n[diarization]\nnum_threads = 2\n", encoding="utf-8")
    cfg = load_config(p, preset="")
    assert cfg.speakers.auto_update_threshold == 0.72 and any("auto_update_threshold" in w for w in cfg.warnings)
