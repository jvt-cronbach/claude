"""Regression tests for the independent review of 0.4.1 (speaker identification).

Each test pins one defect found in review: a rule that put a confident wrong name into the transcript
and summary, a wrong voice teaching a profile, the repair/bulk re-identification without backup, and
the upgrade path from 0.4.0."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from callrec.models import Person, SpeakerInfo, SpeakerTurn
from callrec.postcall import DONE, Plan, chain_state
from callrec.review import Assignment, apply_assignments, mark_summary_outdated, set_participants
from callrec.speakers import backup, repair
from callrec.speakers.bound import merge_to_bound
from callrec.speakers.identify import Params, Voice, build_candidates, identify
from callrec.speakers.profiles import PlainFilePersistence, ProfileStore
from tests.test_identify_041 import axis, mix
from tests.test_pipeline import make_pipeline, recording_with_voices
from tests.test_postcall import World
from tests.test_speaker_identity import _two_known_voices


@pytest.fixture
def store(tmp_path: Path) -> ProfileStore:
    return ProfileStore(PlainFilePersistence(tmp_path / "vp.json"))


def run(store: ProfileStore, voices: list[Voice], people: list[Person], expected: list[Person] | None = None,
        **kw):
    cands, others = build_candidates(people, people if expected is None else expected, store.all_profiles(), [])
    return identify(voices, cands, others, Params(), recording="now", **kw)


# -- single-participant rule ------------------------------------------------------------------------------
def test_single_rule_does_not_name_a_voice_that_contradicts_the_invitees_profile(store: ProfileStore):
    # a stranger (no profile) came instead of Karl, who has a profile: 0.4.1 named Karl AND taught Karl's profile
    # the stranger's voice ("sole"); the next call with the stranger then matched "Karl" by profile
    store.learn("Kunde, Karl", "karl@kunde.de", axis(5), "m", recording="r0", speaker="S1", source="manual")
    out = run(store, [Voice("S1", mix(9, 5, 0.95), 40.0)], [Person("Kunde, Karl", "karl@kunde.de")])
    o = out["S1"]
    assert o.name == "" and o.learn == "" and o.preselect == "Kunde, Karl"
    assert o.explain["decision"] == "mismatch"


def test_single_rule_without_profile_and_a_known_other_voice_is_a_question(store: ProfileStore):
    store.learn("Bernd Muster", "bernd@firma.de", axis(6), "m", recording="r1", speaker="S1", source="manual")
    o = run(store, [Voice("S1", mix(6, 9, 0.75), 40.0)], [Person("Neu, Nora", "nora@kunde.de")])["S1"]
    assert o.name == "" and o.preselect == "Neu, Nora" and o.suggestion is not None
    # a little alike (0.6): named after the invitation, but no profile is created from it, and it is confirmed
    o = run(store, [Voice("S1", mix(6, 9, 0.6), 40.0)], [Person("Neu, Nora", "nora@kunde.de")])["S1"]
    assert o.name == "Neu, Nora" and o.learn == "" and o.confirm


def test_single_rule_weak_profile_match_is_named_but_not_learned_and_to_be_confirmed(store: ProfileStore):
    store.learn("Kunde, Karl", "karl@kunde.de", axis(5), "m", recording="r0", speaker="S1", source="manual")
    o = run(store, [Voice("S1", mix(5, 9, 0.5), 40.0)], [Person("Kunde, Karl", "karl@kunde.de")])["S1"]
    assert o.name == "Kunde, Karl" and o.learn == "" and o.confirm
    o = run(store, [Voice("S1", mix(5, 9, 0.9), 40.0)], [Person("Kunde, Karl", "karl@kunde.de")])["S1"]
    assert o.name == "Kunde, Karl" and o.learn == "sole" and not o.confirm


def test_first_call_with_a_new_person_is_named_but_confirmed_before_the_summary(store: ProfileStore):
    o = run(store, [Voice("S1", axis(3), 40.0)], [Person("Kunde, Karl", "karl@kunde.de")])["S1"]
    assert o.name == "Kunde, Karl" and o.learn == "sole" and o.confirm
    p = Params()
    p.confirm_below = 0.0  # switched off: never asks
    cands, others = build_candidates([Person("Kunde, Karl", "karl@kunde.de")], [Person("Kunde, Karl", "karl@kunde.de")],
                                     [], [])
    assert not identify([Voice("S1", axis(3), 40.0)], cands, others, p, recording="now")["S1"].confirm


def test_wrong_voice_in_a_one_to_one_never_reaches_the_invitees_profile(tmp_path: Path, store: ProfileStore):
    """Call 1 teaches Karl; in call 2 a stranger speaks instead: Karl's profile must stay as it was, and in
    call 3 (Karl + Anna) the stranger must not be recognised as Karl."""
    pipeline, _, _ = make_pipeline(store)
    r1 = recording_with_voices(tmp_path / "k1", 760, 770)
    set_participants(r1, [Person("Kunde, Karl", "karl@kunde.de")])
    pipeline.run(r1)
    karl = store.find("Kunde, Karl", "karl@kunde.de", "fake-embedder")
    assert karl is not None
    before = [(c.recording, c.speaker) for c in karl.contributions]
    r2 = recording_with_voices(tmp_path / "k2", 160, 170)  # a stranger, no profile anywhere
    set_participants(r2, [Person("Kunde, Karl", "karl@kunde.de")])
    pipeline.run(r2)
    s1 = r2.load_speakers()["S1"]
    assert s1.name == "" and s1.preselect_name == "Kunde, Karl"
    karl = store.find("Kunde, Karl", "karl@kunde.de", "fake-embedder")
    assert karl is not None and [(c.recording, c.speaker) for c in karl.contributions] == before


# -- elimination ----------------------------------------------------------------------------------------
def test_elimination_does_not_name_a_voice_that_contradicts_the_remaining_profile(store: ProfileStore):
    store.learn("Anna", "anna@firma.de", axis(0), "m", recording="r0", speaker="S1", source="manual")
    store.learn("Dirk", "dirk@firma.de", axis(2), "m", recording="r0", speaker="S2", source="manual")
    people = [Person("Anna", "anna@firma.de"), Person("Dirk", "dirk@firma.de")]
    # Anna recognised; the other voice is an uninvited guest, far from Dirk's profile
    out = run(store, [Voice("S1", mix(0, 9, 0.95), 40.0), Voice("S2", mix(8, 2, 0.95), 40.0)], people)
    assert out["S1"].name == "Anna"
    assert out["S2"].name == "" and out["S2"].preselect == "Dirk" and out["S2"].explain["decision"] == "mismatch"


def test_elimination_without_a_profile_is_named_but_to_be_confirmed(store: ProfileStore):
    store.learn("Anna", "anna@firma.de", axis(0), "m", recording="r0", speaker="S1", source="manual")
    people = [Person("Anna", "anna@firma.de"), Person("Neu, Nora", "nora@kunde.de")]
    out = run(store, [Voice("S1", mix(0, 9, 0.95), 40.0), Voice("S2", axis(7), 40.0)], people)
    assert out["S2"].name == "Neu, Nora" and out["S2"].rule == "elimination" and out["S2"].confirm
    assert out["S2"].learn == ""


def test_profile_match_below_the_confirmation_level_is_flagged(store: ProfileStore):
    store.learn("Anna", "anna@firma.de", axis(0), "m", recording="r0", speaker="S1", source="manual")
    store.learn("Bernd", "b@firma.de", axis(1), "m", recording="r0", speaker="S2", source="manual")
    people = [Person("Anna", "anna@firma.de"), Person("Bernd", "b@firma.de")]
    out = run(store, [Voice("S1", mix(0, 9, 0.55), 40.0), Voice("S2", mix(1, 8, 0.95), 40.0)], people)
    assert out["S1"].name == "Anna" and out["S1"].confirm
    assert out["S2"].name == "Bernd" and not out["S2"].confirm


# -- the speaker limit ----------------------------------------------------------------------------------------
def test_soft_bound_merges_a_split_voice_but_not_two_people():
    turns = [SpeakerTurn(0, 20, "A"), SpeakerTurn(20, 40, "B")]
    same = {"A": axis(1), "B": mix(1, 2, 0.95)}
    other = {"A": axis(1), "B": mix(1, 2, 0.5)}
    speech = {"A": 20.0, "B": 20.0}
    assert len(merge_to_bound(turns, same, speech, 2, soft_bound=1).merged) == 1
    assert merge_to_bound(turns, other, speech, 2, soft_bound=1).merged == []
    assert len(merge_to_bound(turns, other, speech, 1).merged) == 1  # the hard limit as before


# -- the guided processing waits for an uncertain name ----------------------------------------------------------
def test_chain_shows_the_dialog_and_the_summary_waits_for_unconfirmed_names(tmp_path: Path):
    from tests.conftest import make_recording

    rec = make_recording(tmp_path / "recs", with_parts=False)
    w = World(unassigned=0)
    w.chain.unassigned = lambda r: 1  # one uncertain automatic name
    rec.update_meta(["processing", "speakers"], {"spoken": 1, "unassigned": 0, "unconfirmed": 1})
    w.review_answer = False  # "Überspringen"
    w.chain.start(rec, Plan(True, True, True, True), "dialog")
    w.finish_job()
    w.run_posted()
    assert len(w.reviews) == 1
    ch = chain_state(rec)
    assert ch["stage"] == DONE and "nicht bestätigt" in ch["summary_problem"]
    assert not any("summarize" in steps for _r, steps, _cb in w.jobs) and w.handoffs == []
    # confirmed: the summary is written
    rec.update_meta(["processing", "speakers"], {"spoken": 1, "unassigned": 0, "unconfirmed": 0})
    w2 = World(unassigned=0)
    w2.chain.start(rec, Plan(True, True, True, False), "dialog")
    w2.finish_job()
    assert w2.jobs[-1][1] == frozenset({"summarize"})


def test_unconfirmed_count_reaches_the_chain_and_taking_it_over_confirms(tmp_path: Path, store: ProfileStore):
    from callrec.library import read_info, status_description
    from callrec.postcall import _unassigned_with_text

    pipeline, _, _ = make_pipeline(store)
    rec = recording_with_voices(tmp_path / "k", 760, 770)
    set_participants(rec, [Person("Kunde, Karl", "karl@kunde.de")])
    pipeline.run(rec, {"finalize", "transcribe", "diarize", "identify", "render"})
    info = read_info(rec.folder)
    assert info.speakers_unassigned == 0 and info.speakers_unconfirmed == 1 and _unassigned_with_text(rec) == 1
    assert "unsicher" in status_description(info)
    sole = [c.source for c in store.find("Kunde, Karl", "karl@kunde.de", "fake-embedder").contributions]  # type: ignore[union-attr]
    # "Übernehmen" without "Stimme merken": confirmed (yours now), what was learned stays, nothing new learned
    apply_assignments(rec, [Assignment("S1", "Kunde, Karl", save_profile=False)], store,
                      lambda: pipeline.run(rec, {"render"}), min_learn_speech_s=0)
    s1 = rec.load_speakers()["S1"]
    assert s1.source == "manual" and not s1.needs_confirmation
    assert [c.source for c in store.find("Kunde, Karl", "karl@kunde.de", "fake-embedder").contributions] == sole  # type: ignore[union-attr]
    assert read_info(rec.folder).speakers_unconfirmed == 0 and _unassigned_with_text(rec) == 0


def test_certain_automatic_name_taken_over_unchanged_stays_automatic(tmp_path: Path, store: ProfileStore):
    rec = recording_with_voices(tmp_path / "k", 760, 770)
    rec.save_speakers({"ME": SpeakerInfo("ME", "Ich", source="self"),
                       "S1": SpeakerInfo("S1", "Sprecher 1", name="Anna", source="auto", score=0.9)})
    apply_assignments(rec, [Assignment("S1", "Anna", save_profile=False)], store, lambda: None)
    assert rec.load_speakers()["S1"].source == "auto"


# -- summary flagged as outdated against the names it was written with ---------------------------------------
def test_summary_outdated_compares_with_the_names_in_the_summary(tmp_path: Path):
    from tests.conftest import make_recording

    rec = make_recording(tmp_path / "recs", with_parts=False)
    rec.summary_path.write_text("# Zusammenfassung", encoding="utf-8")
    rec.update_meta(["processing", "summary", "speaker_names"], {"ME": "Ich", "S1": "Anna"})
    anna = {"S1": SpeakerInfo("S1", "Sprecher 1", name="Anna", source="auto")}
    bernd = {"S1": SpeakerInfo("S1", "Sprecher 1", name="Bernd", source="auto")}
    assert mark_summary_outdated(rec, anna, bernd) is True
    assert mark_summary_outdated(rec, bernd, anna) is False  # back to what the summary says (e.g. a restore)
    assert rec.load_meta()["processing"]["summary"]["speakers_outdated"] is False


def test_reidentifying_a_handed_over_recording_flags_the_summary(tmp_path: Path, store: ProfileStore):
    from callrec.library import read_info, status_description

    _two_known_voices(tmp_path, store)
    pipeline, _, _ = make_pipeline(store)
    rec = recording_with_voices(tmp_path / "r", 330, 550)
    set_participants(rec, [Person("Anna Beispiel", "anna@firma.de"), Person("Bernd Muster", "bernd@firma.de")])
    pipeline.run(rec)
    rec.update_meta(["handoffs"], [{"at": "x", "location": "SharePoint/x.md"}])
    names_then = {sid: s.display for sid, s in rec.load_speakers().items()}
    # the profiles changed since (e.g. a correction in another call): re-identification gives other names
    for p in store.all_profiles():
        store.delete(p.id)
    pipeline.run(rec, {"identify", "render"})
    names_now = {sid: s.display for sid, s in rec.load_speakers().items()}
    assert names_now != names_then
    text = status_description(read_info(rec.folder))
    assert "alten Sprechernamen" in text


# -- repair and backups ----------------------------------------------------------------------------------------
def _profile(store: ProfileStore, name: str, email: str, v: np.ndarray, rid: str = "") -> None:
    store.learn(name, email, v, "m", recording=rid, speaker=f"x{len(store.all_profiles())}", source="manual")


def _profiles(store: ProfileStore, *items: tuple[str, str, np.ndarray]) -> None:
    """Profiles exactly as given (two of the same identity can exist from earlier versions)."""
    out = []
    for i, (name, email, v) in enumerate(items):
        e = [round(float(x), 6) for x in v]
        out.append({"id": f"p{i}", "name": name, "email": email, "model_id": "m", "embedding": e, "samples": 1,
                    "contributions": [{"recording": f"r{i}", "speaker": "S1", "source": "manual", "embedding": e}]})
    store.restore_snapshot(json.dumps({"version": 2, "profiles": out}))


def test_repair_never_merges_two_people_sharing_a_mailbox_or_a_name(tmp_path: Path, store: ProfileStore,
                                                                     monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CALLREC_HOME", str(tmp_path / "home"))
    _profiles(store, ("Anna Müller", "anna.mueller@firma.de", axis(0)),
              ("Anna Müller", "anna.mueller2@firma.de", axis(1)),  # a namesake colleague
              ("Thomas Schmidt", "t.schmidt@firma.de", axis(2)),
              ("Thomas Schmidt", "", axis(7)))  # typed for an external guest - clearly another voice
    rp = repair.plan(store, tmp_path / "none")
    assert rp.merges == []
    assert any("deutlich verschieden" in n for n in rp.notes)
    # the same address with different names (shared mailbox) is not one person either
    from callrec.models import Person as P
    from callrec.speakers.repair import _same_person_for_merge

    a, b = SimpleNamespace(name="Anna Kunde", email="info@kunde.de"), SimpleNamespace(name="Bernd Kunde", email="info@kunde.de")
    assert _same_person_for_merge(a, b) == ""
    assert _same_person_for_merge(SimpleNamespace(name="Anna", email="a@x.de"), SimpleNamespace(
        name="Anna Kunde", email="a@x.de")) == "same address"
    assert _same_person_for_merge(SimpleNamespace(name="Kunde, Anna", email=""), SimpleNamespace(
        name="Anna Kunde", email="a@x.de")) == "typed name"
    assert P  # noqa: B018


def test_repair_does_not_add_assignments_made_without_stimme_merken(tmp_path: Path, store: ProfileStore,
                                                                    monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CALLREC_HOME", str(tmp_path / "home"))
    pipeline, _, _ = make_pipeline(store)
    rec = recording_with_voices(tmp_path / "r1", 330, 550)
    set_participants(rec, [Person("Anna Beispiel", "anna@firma.de"), Person("Bernd Muster", "bernd@firma.de")])
    pipeline.run(rec)
    apply_assignments(rec, [Assignment("S1", "Anna Beispiel", save_profile=True),
                            Assignment("S2", "Bernd Muster", save_profile=False)], store, lambda: None,
                      min_learn_speech_s=0)
    _profile(store, "Bernd Muster", "bernd@firma.de", axis(3), rid="older")
    _profile(store, "Bernd Muster", "bernd@firma.de", axis(4), rid="older2")
    rp = repair.plan(store, tmp_path / "r1")
    repair.apply(store, rp)
    bernd = store.find("Bernd Muster", "bernd@firma.de", "fake-embedder")
    assert bernd is None or all(c.recording != rec.uid for c in bernd.contributions)
    m = store.find("Bernd Muster", "bernd@firma.de", "m")
    assert m is not None and all(c.recording != rec.uid for c in m.contributions)


def test_repair_backs_up_the_store_and_restore_puts_it_back(tmp_path: Path, store: ProfileStore,
                                                            monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CALLREC_HOME", str(tmp_path / "home"))
    _profiles(store, ("Müller, Anna", "", axis(0)), ("Anna Müller", "anna@firma.de", mix(0, 1, 0.8)))
    before = store.snapshot()
    rp = repair.plan(store, tmp_path / "none")
    assert rp.merges
    repair.apply(store, rp)
    assert len(store.all_profiles()) == 1
    b = backup.latest()
    assert b is not None and "Reparatur" in backup.describe(b)
    n, folders = backup.restore(store, b)
    assert n == 2 and folders == [] and json.loads(store.snapshot()) == json.loads(before)


def test_backup_of_recordings_restores_speaker_data(tmp_path: Path, store: ProfileStore,
                                                    monkeypatch: pytest.MonkeyPatch):
    from tests.conftest import make_recording

    monkeypatch.setenv("CALLREC_HOME", str(tmp_path / "home"))
    rec = make_recording(tmp_path / "recs", with_parts=False)
    rec.save_speakers({"ME": SpeakerInfo("ME", "Ich", source="self"),
                       "S1": SpeakerInfo("S1", "Sprecher 1", name="Anna", source="manual")})
    _profile(store, "Anna", "anna@firma.de", axis(0), rid="r1")
    b = backup.create(store, [rec], "Sprecher neu erkennen (1 Aufnahmen)")
    rec.save_speakers({"ME": SpeakerInfo("ME", "Ich", source="self"),
                       "S1": SpeakerInfo("S1", "Sprecher 1", name="Bernd", source="auto")})
    store.delete_all()
    n, folders = backup.restore(store, backup.latest())
    assert b.exists() and n == 1 and folders == [rec.folder]
    assert rec.load_speakers()["S1"].name == "Anna" and rec.load_speakers()["S1"].source == "manual"


# -- upgrade from 0.4.0 --------------------------------------------------------------------------------------------
PROFILES_040 = (
    '{"version": 2, "profiles": [{"id": "5560c58b17a243e6966306635ed1d3ab", "name": "Müller, Anna", "email": '
    '"anna@firma.de", "model_id": "m", "embedding": [0.8, 0.6, 0.0, 0.0], "samples": 2, "created_at": '
    '"2026-11-25T11:00:54+00:00", "updated_at": "2026-11-25T11:00:54+00:00", "contributions": [{"recording": '
    '"rec1", "speaker": "S1", "source": "manual", "embedding": [1.0, 0.0, 0.0, 0.0], "weight": 1.0, "seconds": 30, '
    '"at": "2026-11-25T11:00:54+00:00", "title": "T", "folder": "F"}, {"recording": "rec2", "speaker": "S2", '
    '"source": "sole", "embedding": [0.0, 1.0, 0.0, 0.0], "weight": 0.75, "seconds": 30, "at": '
    '"2026-11-25T11:00:54+00:00", "title": "", "folder": ""}]}]}'
)  # written by the 0.4.0 code (ProfileStore.learn: one manual, one "sole" contribution)
SPEAKER_040 = ('{"id": "S1", "label": "Sprecher 1", "name": "Anna", "email": "", "profile_id": "", "score": 0.7, '
               '"source": "auto", "suggested_name": "", "suggested_email": "", "suggested_score": null, "note": "", '
               '"self_suspect": false, "preselect_name": "", "minor": false}')


def test_profiles_and_speaker_records_of_0_4_0_load(tmp_path: Path):
    (tmp_path / "vp.json").write_text(PROFILES_040, encoding="utf-8")
    st = ProfileStore(PlainFilePersistence(tmp_path / "vp.json"))
    p = st.all_profiles()[0]
    assert [c.source for c in p.contributions] == ["manual", "sole"] and p.mode == "" and p.aliases == []
    assert p.score(np.array([1, 0, 0, 0], np.float32)) is not None
    assert st.forget("rec2", sources=("auto", "sole")) == ["Müller, Anna"]  # 0.4.0 "sole" stays undoable
    s = SpeakerInfo.from_dict(json.loads(SPEAKER_040))
    assert s.rule == "" and not s.needs_confirmation and s.name == "Anna"


def test_auto_update_threshold_is_not_lowered_by_the_migration(tmp_path: Path):
    from callrec.config import SPEAKER_DEFAULTS_041, load_config

    assert "auto_update_threshold" not in SPEAKER_DEFAULTS_041
    path = tmp_path / "config.toml"
    path.write_text("[speakers]\nmatch_threshold = 0.55\nauto_update_threshold = 0.75\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.speakers.match_threshold == 0.45 and cfg.speakers.auto_update_threshold == 0.75
    assert cfg.speakers.confirm_below == 0.75


def test_immer_erkennen_names_an_uninvited_person_only_on_a_clear_match(store: ProfileStore):
    # 0.4.1 named a person set to "immer erkennen" at the participants' relative bar (0.45): in every call
    # without them, any unprofiled voice a little like them got their name
    c = store.learn("Chef, Clemens", "chef@firma.de", axis(4), "m", recording="r0", speaker="S1", source="manual")
    store.set_mode(c.id, "always")
    people = [Person("Kunde, Karl", "karl@kunde.de"), Person("Neu, Nora", "nora@kunde.de")]
    out = run(store, [Voice("S1", mix(4, 9, 0.55), 40.0), Voice("S2", axis(10), 40.0)], people)
    assert out["S1"].name == "" and out["S1"].preselect == "Chef, Clemens"
    out = run(store, [Voice("S1", mix(4, 9, 0.9), 40.0), Voice("S2", axis(10), 40.0)], people)
    assert (out["S1"].name, out["S1"].rule) == ("Chef, Clemens", "always")


def test_rerun_never_files_a_similar_other_voice_under_your_manual_name(tmp_path: Path, store: ProfileStore):
    """Speaker limit on a re-run: a different voice (0.6 alike, e.g. the same kind of headset) must not be
    merged into a cluster you named - 0.4.1 allowed it from 0.45 (match_threshold)."""
    from callrec.pipeline import NAMED_MERGE_MIN

    turns = [SpeakerTurn(0, 20, "A"), SpeakerTurn(20, 40, "B")]
    emb = {"A": axis(1), "B": mix(1, 2, 0.6)}
    res = merge_to_bound(turns, emb, {"A": 20.0, "B": 20.0}, 1, keep_apart={"A": "Anna"},
                         named_merge_min=max(0.45, NAMED_MERGE_MIN))
    assert res.merged == []
    emb = {"A": axis(1), "B": mix(1, 2, 0.95)}
    res = merge_to_bound(turns, emb, {"A": 20.0, "B": 20.0}, 1, keep_apart={"A": "Anna"},
                         named_merge_min=max(0.45, NAMED_MERGE_MIN))
    assert len(res.merged) == 1


def test_profile_name_is_flagged_when_an_uninvited_profile_or_an_unprofiled_invitee_could_be_the_voice(
        store: ProfileStore):
    store.learn("Anna", "anna@firma.de", axis(0), "m", recording="r0", speaker="S1", source="manual")
    store.learn("Kai", "kai@firma.de", mix(0, 3, 0.97), "m", recording="r1", speaker="S1", source="manual")  # alike
    store.learn("Bernd", "b@firma.de", axis(1), "m", recording="r0", speaker="S2", source="manual")
    # the lone profiled participant, while an invitee without a profile may be the one speaking
    out = run(store, [Voice("S1", mix(1, 9, 0.9), 40.0)], [Person("Bernd", "b@firma.de"), Person("Olga", "o@k.de")])
    assert out["S1"].name == "Bernd" and out["S1"].confirm
    # Anna and Bernd invited; the voice is as close to Kai (not invited) as to Anna
    out = run(store, [Voice("S1", mix(0, 3, 0.98), 40.0), Voice("S2", mix(1, 8, 0.95), 40.0)],
              [Person("Anna", "anna@firma.de"), Person("Bernd", "b@firma.de")])
    assert out["S1"].name == "Anna" and out["S1"].confirm and out["S1"].learn == ""
    assert out["S2"].name == "Bernd" and not out["S2"].confirm
