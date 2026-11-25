"""0.4.1: speaker recognition that learns - relative decision among the participants, multi-reference
profiles, the single-participant rule with expected participants only, elimination, linking typed
names, explanations, statistics, diagnostics, re-identification and the one-time profile repair."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from callrec.models import Person, SpeakerInfo
from callrec.review import Assignment, apply_assignments, recognition_text, set_participants
from callrec.speakers import candidates, diagnostics, names, repair
from callrec.speakers.identify import Cand, Params, Voice, _joint, build_candidates, explain_lines, identify
from callrec.speakers.profiles import PlainFilePersistence, ProfileStore
from callrec.speakers.stats import profile_stats, stats_lines
from tests.test_pipeline import make_pipeline, recording_with_voices
from tests.test_speaker_identity import _two_known_voices

DIM = 16


def vec(*weights: float) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[: len(weights)] = weights
    return v / np.linalg.norm(v)


def mix(a: int, b: int, cos_to_a: float) -> np.ndarray:
    """A unit vector with cosine ``cos_to_a`` to axis ``a`` (rest on axis ``b``)."""
    v = np.zeros(DIM, dtype=np.float32)
    v[a] = cos_to_a
    v[b] = np.sqrt(max(0.0, 1 - cos_to_a ** 2))
    return v


def axis(i: int) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    v[i] = 1.0
    return v


@pytest.fixture
def store(tmp_path: Path) -> ProfileStore:
    return ProfileStore(PlainFilePersistence(tmp_path / "vp.json"))


def run(store: ProfileStore, voices: list[Voice], participants: list[Person], expected: list[Person] | None = None,
        params: Params | None = None, **kw):  # type: ignore[no-untyped-def]
    cands, others = build_candidates(participants, participants if expected is None else expected,
                                     [p for p in store.all_profiles() if p.model_id == "m"], [Person("Ich", "ich@x.de")])
    return identify(voices, cands, others, params or Params(), **kw)


# -- participants: who is expected to speak ---------------------------------------------------------
def test_declined_rooms_and_unanswered_optional_attendees_do_not_count():
    people = [Person("Kunde, Karl", "karl@kunde.de", "organizer"), Person("Raum Elbe 3.12", "raum.elbe@firma.de"),
              Person("Abgesagt, Anna", "anna@firma.de"), Person("Optional, Otto", "otto@firma.de"),
              Person("Zusage, Zoe", "zoe@firma.de")]
    event = {"attendee_status": {"anna@firma.de": {"type": "required", "response": "declined"},
                                 "otto@firma.de": {"type": "optional", "response": "none"},
                                 "zoe@firma.de": {"type": "optional", "response": "accepted"}}}
    kinds = {c.person.name: (c.expected, c.excluded, c.why) for c in candidates.classify(people, event)}
    assert kinds["Kunde, Karl"][:2] == (True, False)
    assert kinds["Raum Elbe 3.12"][:2] == (False, True)
    assert kinds["Abgesagt, Anna"] == (False, True, "abgesagt")
    assert kinds["Optional, Otto"] == (False, False, "optional, keine Zusage")  # may still be matched
    assert kinds["Zusage, Zoe"][:2] == (True, False)
    assert candidates.classify(people, {"attendee_status": "kaputt"})  # robust against odd meta.json


def test_graph_and_ics_keep_attendee_type_and_answer():
    from callrec.calendar.graph import parse_graph_event

    ev = parse_graph_event({"id": "1", "subject": "x", "start": {"dateTime": "2026-11-25T10:00:00", "timeZone": "UTC"},
                            "end": {"dateTime": "2026-11-25T11:00:00", "timeZone": "UTC"},
                            "attendees": [{"type": "optional", "status": {"response": "declined"},
                                           "emailAddress": {"name": "Otto", "address": "otto@firma.de"}},
                                          {"type": "resource", "emailAddress": {"name": "Raum", "address": "r@firma.de"}}]})
    assert [a.name for a in ev.attendees] == ["Otto"]
    assert ev.attendee_status == {"otto@firma.de": {"type": "optional", "response": "declined"}}
    from callrec.models import CalendarEvent

    assert CalendarEvent.from_dict(ev.to_dict()).attendee_status == ev.attendee_status


def test_probable_links_and_rooms():
    assert names.probable_same("Anna", "", "Müller, Anna", "anna.mueller@firma.de") == "first_name"
    assert names.probable_same("Anna Müller", "anna.mueller@firma.de", "Müller, Anna", "a.mueller@firma.de") == "alias"
    assert names.probable_same("Anna Müller", "anna@firma.de", "Anna Müller", "anna@kunde.de") == ""  # other domain
    assert names.probable_same("Anna", "", "Bernd Muster", "") == ""
    assert names.looks_like_room_or_list(Person("Konferenzraum 2", ""))
    assert not names.looks_like_room_or_list(Person("Anna Raumann", "anna.raumann@firma.de"))


# -- profiles: several references -------------------------------------------------------------------
def test_profile_recognises_a_second_device_the_average_misses(store: ProfileStore):
    for i in range(4):  # mostly learned on the headset (axis 0), once on the laptop (axis 1)
        store.learn("Anna", "", axis(0), "m", recording=f"r{i}", speaker="S1", source="manual")
    prof = store.learn("Anna", "", axis(1), "m", recording="r9", speaker="S1", source="manual")
    laptop_again = mix(1, 2, 0.9)
    centroid = float(np.dot(laptop_again, prof.embedding))
    assert centroid < 0.3  # one averaged embedding: the laptop voice looks like somebody else
    assert prof.score(laptop_again) == pytest.approx(0.45, abs=0.01)  # mean of the two closest references


# -- the decision ---------------------------------------------------------------------------------------
def test_single_participant_is_named_without_profile_and_learns(store: ProfileStore):
    out = run(store, [Voice("S1", axis(3), 30.0)], [Person("Kunde, Karl", "karl@kunde.de")])
    o = out["S1"]
    assert (o.name, o.rule, o.learn) == ("Kunde, Karl", "single", "sole")
    assert "einzige weitere Person" in o.why and o.explain["decision"] == "named"


def test_single_participant_with_extra_entries_and_a_fragment(store: ProfileStore):
    people = [Person("Kunde, Karl", "karl@kunde.de"), Person("Raum", "raum@firma.de")]
    voices = [Voice("S1", axis(3), 40.0), Voice("S2", axis(4), 2.0, blocked="kurzes Fragment")]
    out = run(store, voices, people, expected=[people[0]])
    assert out["S1"].name == "Kunde, Karl" and out["S2"].name == ""


def test_single_participant_who_sounds_like_a_known_colleague_is_named_but_not_learned(store: ProfileStore):
    store.learn("Kunde, Karl", "karl@kunde.de", axis(5), "m", recording="r0", speaker="S1", source="manual")
    store.learn("Bernd Muster", "bernd@firma.de", axis(6), "m", recording="r1", speaker="S1", source="manual")
    out = run(store, [Voice("S1", mix(6, 5, 0.95), 30.0)], [Person("Kunde, Karl", "karl@kunde.de")])
    o = out["S1"]
    assert o.name == "Kunde, Karl" and o.learn == "" and o.suggestion is not None and o.suggestion[0] == "Bernd Muster"
    assert "klingt eher wie Bernd Muster" in o.why


def test_participant_restricted_threshold_below_the_old_bar(store: ProfileStore):
    store.learn("Anna", "anna@firma.de", axis(0), "m", recording="r0", speaker="S1", source="manual")
    store.learn("Bernd", "bernd@firma.de", axis(1), "m", recording="r0", speaker="S2", source="manual")
    store.learn("Fremd", "fremd@x.de", axis(7), "m", recording="r0", speaker="S3", source="manual")
    people = [Person("Anna", "anna@firma.de"), Person("Bernd", "bernd@firma.de")]
    # 0.50 for Anna (0.4.0 needed 0.55 against EVERY profile), Bernd only 0.1, a non-participant at 0.55
    v = 0.50 * axis(0) + 0.10 * axis(1) + 0.55 * axis(7)
    v = (v / np.linalg.norm(v)).astype(np.float32)
    sims = {n: float(np.dot(v, axis(i))) for n, i in (("Anna", 0), ("Bernd", 1), ("Fremd", 7))}
    assert sims["Fremd"] > sims["Anna"] > 0.45
    out = run(store, [Voice("S1", v, 20.0), Voice("S2", axis(1), 20.0)], people)
    assert out["S1"].name == "Anna" and out["S1"].rule == "profile"
    assert out["S2"].name == "Bernd"


def test_ambiguous_between_participants_stays_open_with_reason_and_suggestion(store: ProfileStore):
    store.learn("Anna", "anna@firma.de", axis(0), "m", recording="r0", speaker="S1", source="manual")
    store.learn("Bernd", "bernd@firma.de", axis(1), "m", recording="r0", speaker="S2", source="manual")
    v = (axis(0) * 0.52 + axis(1) * 0.50)
    out = run(store, [Voice("S1", (v / np.linalg.norm(v)).astype(np.float32), 20.0)],
              [Person("Anna", "anna@firma.de"), Person("Bernd", "bernd@firma.de"), Person("Carla", "c@firma.de")])
    o = out["S1"]
    assert o.name == "" and "nicht eindeutig" in o.why and o.preselect == "Anna" and o.preselect_score is not None
    lines = explain_lines(o.explain)
    assert any(line.startswith("Kandidaten: Anna") for line in lines)


def test_joint_assignment_decides_what_pairwise_margins_cannot():
    # voice A: Frida 0.77 / Dirk 0.72; voice B: Dirk 0.65 / Frida 0.62 - each row alone is ambiguous,
    # but swapping both costs 0.08: Frida->A, Dirk->B is clear
    scores = {"A": {0: 0.77, 1: 0.72}, "B": {0: 0.62, 1: 0.65}}
    res = _joint([Voice("A", None, 10), Voice("B", None, 10)], [0, 1], scores, null_value=0.39, margin=0.06)
    assert res["A"][0] == 0 and res["B"][0] == 1 and min(res["A"][2], res["B"][2]) >= 0.06


def test_elimination_names_the_last_participant_unless_the_voice_duplicates_a_named_one(store: ProfileStore):
    store.learn("Anna", "anna@firma.de", axis(0), "m", recording="r0", speaker="S1", source="manual")
    people = [Person("Anna", "anna@firma.de"), Person("Neu, Nils", "nils@firma.de")]
    out = run(store, [Voice("S1", axis(0), 20.0), Voice("S2", axis(9), 20.0)], people)
    assert out["S1"].name == "Anna" and (out["S2"].name, out["S2"].rule) == ("Neu, Nils", "elimination")
    assert out["S2"].learn == ""  # elimination names, it does not create a profile
    # the second voice is Anna split in two clusters: no elimination, a question instead
    out = run(store, [Voice("S1", axis(0), 20.0), Voice("S2", mix(0, 9, 0.9), 20.0)], people)
    assert out["S2"].name == "" and out["S2"].preselect == "Neu, Nils" and "gleiche Person" in out["S2"].why


def test_typed_first_name_profile_is_suggested_then_linked_once(store: ProfileStore):
    store.learn("Anna", "", axis(0), "m", recording="r0", speaker="S1", source="manual")
    people = [Person("Müller, Anna", "anna.mueller@firma.de"), Person("Bernd", "b@firma.de")]
    out = run(store, [Voice("S1", axis(0), 20.0), Voice("S2", axis(2), 20.0)], people)
    o = out["S1"]
    assert o.name == "" and o.preselect == "Müller, Anna" and o.preselect_profile_id
    assert "einmal bestätigen" in o.why
    # in a call with only her the typed profile is linked automatically when the voice matches
    out = run(store, [Voice("S1", axis(0), 20.0)], [people[0]])
    assert out["S1"].name == "Müller, Anna" and out["S1"].profile_id == o.preselect_profile_id


def test_never_and_always(store: ProfileStore):
    a = store.learn("Anna", "anna@firma.de", axis(0), "m", recording="r0", speaker="S1", source="manual")
    c = store.learn("Chef, Clemens", "chef@firma.de", axis(4), "m", recording="r0", speaker="S2", source="manual")
    store.set_mode(a.id, "never")
    store.set_mode(c.id, "always")
    out = run(store, [Voice("S1", axis(0), 20.0), Voice("S2", axis(4), 20.0)],
              [Person("Anna", "anna@firma.de"), Person("Bernd", "b@firma.de")])
    assert out["S1"].name == "" and out["S1"].preselect == "Anna"
    assert (out["S2"].name, out["S2"].rule) == ("Chef, Clemens", "always")  # not invited, set to "immer erkennen"


def test_without_participant_list_only_a_clear_known_voice_is_preselected(store: ProfileStore):
    store.learn("Anna", "anna@firma.de", axis(0), "m", recording="r0", speaker="S1", source="manual")
    store.learn("Bernd", "b@firma.de", axis(1), "m", recording="r0", speaker="S2", source="manual")
    out = run(store, [Voice("S1", mix(0, 3, 0.9), 20.0)], [], has_participants=False)
    assert out["S1"].name == "" and out["S1"].preselect == "Anna" and "keine Teilnehmerliste" in out["S1"].why
    out = run(store, [Voice("S1", vec(0.6, 0.6, 0, 0.5), 20.0)], [], has_participants=False)
    assert out["S1"].preselect == ""  # 0.55 to both: no pre-selection


# -- the pipeline with these rules -----------------------------------------------------------------------
def test_one_to_one_with_a_declined_attendee_is_named_and_learned(tmp_path: Path, store: ProfileStore):
    rec = recording_with_voices(tmp_path / "r", 760, 770)  # one unknown voice, split in two clusters
    set_participants(rec, [Person("Kunde, Karl", "karl@kunde.de"), Person("Abgesagt, Anna", "anna@firma.de")])
    rec.update_meta(["event"], {"id": "e", "subject": "x", "attendee_status": {
        "anna@firma.de": {"type": "required", "response": "declined"}}})
    pipeline, _, _ = make_pipeline(store)
    result = pipeline.run(rec)
    sp = {sid: s for sid, s in rec.load_speakers().items() if sid != "ME"}
    assert list(sp) == ["S1"] and sp["S1"].name == "Kunde, Karl" and sp["S1"].rule == "single"
    assert result.unassigned_speakers == 0
    karl = store.find("Kunde, Karl", "karl@kunde.de", "fake-embedder")
    assert karl is not None and [c.source for c in karl.contributions] == ["sole"]
    assert [h["how"] for h in karl.history] == ["single"]
    ident = rec.load_meta()["processing"]["identification"]
    assert ident["rule"] == "participants-v3" and ident["expected"] == 1
    assert ident["decisions"]["S1"]["decision"] == "named"
    assert "einzige weitere Person" in recognition_text(rec)


def test_suggestion_confirmed_links_profile_and_history_counts(tmp_path: Path, store: ProfileStore):
    _two_known_voices(tmp_path, store)  # "Anna Beispiel" (330 Hz), "Bernd Muster" (550 Hz), no addresses
    rec = recording_with_voices(tmp_path / "r2", 330, 990)
    set_participants(rec, [Person("Beispiel, Anna", "anna@firma.de"), Person("Neu, Nils", "nils@firma.de")])
    pipeline, _, _ = make_pipeline(store)
    pipeline.run(rec)
    sp = rec.load_speakers()
    assert sp["S1"].name in ("", "Beispiel, Anna")
    # whether named or confirmed: afterwards the profile carries the calendar identity
    if not sp["S1"].name:
        apply_assignments(rec, [Assignment("S1", sp["S1"].preselect_name)], store, lambda: None, min_learn_speech_s=0)
    anna = store.find("Beispiel, Anna", "anna@firma.de", "fake-embedder")
    assert anna is not None
    st = profile_stats(anna)
    assert st["recent_n"] >= 1 and stats_lines(st)[0].startswith(anna.name)


def test_correction_is_counted_against_the_profile(tmp_path: Path, store: ProfileStore):
    _two_known_voices(tmp_path, store)
    rec = recording_with_voices(tmp_path / "r2", 550, 330)
    set_participants(rec, [Person("Anna Beispiel"), Person("Bernd Muster")])
    pipeline, _, _ = make_pipeline(store)
    pipeline.run(rec)
    sp = rec.load_speakers()
    assert sp["S1"].name == "Bernd Muster" and sp["S2"].name == "Anna Beispiel"
    bernd = store.find("Bernd Muster", "", "fake-embedder")
    assert bernd is not None and any(h["how"] == "profile" for h in bernd.history)
    apply_assignments(rec, [Assignment("S1", "Dora Dritte", save_profile=False)], store, lambda: None)
    bernd = store.find("Bernd Muster", "", "fake-embedder")
    assert [h["how"] for h in bernd.history if h["recording"] == rec.uid] == ["corrected"]  # type: ignore[union-attr]
    assert "korrigiert" in " ".join(stats_lines(profile_stats(bernd)))  # type: ignore[arg-type]


# -- diagnostics, repair, CLI ------------------------------------------------------------------------------
def test_diagnostics_are_anonymous_unless_names_are_wanted(tmp_path: Path, store: ProfileStore):
    rec = _two_known_voices(tmp_path, store)
    pipeline, _, _ = make_pipeline(store)
    pipeline.run(rec, {"identify", "render"})
    anon = json.dumps(diagnostics.build(rec))
    for secret in ("Anna Beispiel", "Bernd Muster", "olga@firma.de", "Olga", rec.title(), rec.folder.name, "Hallo"):
        assert secret not in anon
    assert "Person " in anon
    named = diagnostics.export(rec, tmp_path / "d.json", include_names=True)
    assert "Anna Beispiel" in named.read_text(encoding="utf-8")


def test_repair_merges_links_and_rebuilds(tmp_path: Path, store: ProfileStore):
    rec = recording_with_voices(tmp_path / "r1", 330, 550)
    set_participants(rec, [Person("Müller, Anna", "anna.mueller@firma.de"), Person("Bernd Muster", "b@firma.de")])
    pipeline, _, _ = make_pipeline(store)
    pipeline.run(rec)
    apply_assignments(rec, [Assignment("S2", "Bernd Muster")], store, lambda: None, min_learn_speech_s=0)
    # 0.4.0: "Anna" typed for S1 became a profile of its own, never linked to the calendar identity
    sp = rec.load_speakers()
    sp["S1"].name, sp["S1"].source = "Anna", "manual"
    rec.save_speakers(sp)
    store.learn("Anna", "", rec.load_embeddings()[0]["S1"], "fake-embedder", recording="", speaker="x", source="manual")
    # an earlier version: Bernd also exists under a second address, with an entry of unknown origin
    store.add_sample("Bernd Muster", "bernd.muster@firma.de", np.ones(16, np.float32), "fake-embedder")
    legacy = store.find("Bernd Muster", "bernd.muster@firma.de", "fake-embedder")
    assert legacy is not None
    rp = repair.plan(store, tmp_path / "r1")
    text = "\n".join(rp.lines())
    assert "dieselbe Person" in text and "Profil „Anna“ ist Müller, Anna" in text
    repair.apply(store, rp)
    assert not repair.is_due()
    anna = store.find("Müller, Anna", "anna.mueller@firma.de", "fake-embedder")
    assert anna is not None and anna.matches_person(Person("Anna"))
    assert len([p for p in store.all_profiles() if p.matches_person(Person("Bernd Muster"))]) == 1


def test_typed_first_name_becomes_the_participant(tmp_path: Path, store: ProfileStore):
    rec = recording_with_voices(tmp_path / "r1", 330, 550)
    set_participants(rec, [Person("Müller, Anna", "anna.mueller@firma.de"), Person("Bernd Muster", "b@firma.de")])
    pipeline, _, _ = make_pipeline(store)
    pipeline.run(rec)
    msgs = apply_assignments(rec, [Assignment("S1", "Anna")], store, lambda: None, min_learn_speech_s=0)
    assert rec.load_speakers()["S1"].name == "Müller, Anna" and any("als Müller, Anna" in m for m in msgs)
    assert [p.name for p in rec.participants()] == ["Müller, Anna", "Bernd Muster"]  # no duplicate "Anna"
    assert store.find("Müller, Anna", "anna.mueller@firma.de", "fake-embedder") is not None


def test_cli_explain_and_diagnostics(tmp_path: Path, store: ProfileStore, capsys, monkeypatch):
    from callrec import cli, factory

    rec = _two_known_voices(tmp_path, store)
    monkeypatch.setattr(factory, "profile_store", lambda: store)
    assert cli.main(["speakers", str(rec.folder), "--explain"]) == 0
    out = capsys.readouterr().out
    assert "Entscheidung: selbst zugeordnet" in out
    target = tmp_path / "diag.json"
    assert cli.main(["diagnostics", str(rec.folder), "--out", str(target)]) == 0
    assert "Anna Beispiel" not in target.read_text(encoding="utf-8")


def test_config_041_migration_keeps_own_values(tmp_path: Path):
    from callrec import config as C

    p = tmp_path / "config.toml"
    C.write_default_config(p)
    text = p.read_text(encoding="utf-8")
    text = text.replace("match_threshold = 0.45", "match_threshold = 0.55").replace(
        "min_learn_speech_s = 12.0", "min_learn_speech_s = 30.0").replace("single_participant_rule = true\n", "")
    p.write_text(text, encoding="utf-8")
    cfg = C.load_config(p)
    assert cfg.speakers.match_threshold == 0.45 and cfg.speakers.min_learn_speech_s == 30.0
    assert C.SPEAKER_RULES_NOTE in cfg.warnings
    assert C.load_config(p).warnings == [] or C.SPEAKER_RULES_NOTE not in C.load_config(p).warnings


def test_speakerinfo_round_trip_of_new_fields():
    s = SpeakerInfo("S1", "Sprecher 1", rule="single", preselect_profile_id="p", preselect_score=0.5)
    assert SpeakerInfo.from_dict(s.to_dict()) == s
    assert Cand(Person("A"), True).label == "A"


def test_web_dtos_carry_explanation_stats_and_mode(tmp_path: Path, store: ProfileStore):
    from callrec.library import read_info
    from callrec.ui import dto

    rec = _two_known_voices(tmp_path, store)
    review = dto.speaker_review(rec, store, None)
    assert all(r["explain"] for r in review["rows"]) and "suggestions" in review
    detail = dto.recording_detail(read_info(rec.folder), False, False, False)
    assert detail["recognition"] and detail["recognition"][0]["lines"][0].startswith("Entscheidung: selbst zugeordnet")
    profs = dto.profiles(store)
    assert {p["mode"] for p in profs} == {""} and all(p["state"] for p in profs)
    assert detail["actions"]["explain"] and detail["actions"]["diagnostics"]
