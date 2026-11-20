"""Logic behind the post-call speaker review (UI-independent)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from callrec.models import (
    SELF_SPEAKER_ID,
    SOURCE_AUTO,
    SOURCE_MANUAL,
    SOURCE_SELF_ECHO,
    USER_DECIDED,
    Person,
    SpeakerInfo,
    SpeakerTurn,
)
from callrec.recording import Recording
from callrec.speakers.profiles import ProfileStore

SOURCE_TEXT = {"manual": "von Ihnen zugeordnet", "confirmed": "von Ihnen bestätigt", "auto": "automatisch",
               "sole": "einzige eingeladene Person der Gegenseite", "legacy": "frühere Version (Herkunft unbekannt)"}
SELF_CHOICE = "Das bin ich (meine Stimme auf der Spur der Gegenseite)"
SUGGESTION_SUFFIX = " (kein Teilnehmer dieses Termins)"


@dataclass
class SpeakerOverview:
    id: str
    label: str
    name: str
    score: float | None
    source: str
    talk_time_s: float
    utterances: int
    sample_text: str
    sample_span: tuple[float, float] | None
    suggested_name: str = ""
    suggested_score: float | None = None
    note: str = ""
    self_suspect: bool = False
    preselect_name: str = ""


def choose_sample_span(turns: list[SpeakerTurn], min_len: float = 3.0, max_len: float = 8.0) -> tuple[float, float] | None:
    """A clip that is long enough to recognise the voice but not too long."""
    if not turns:
        return None
    good = [t for t in turns if t.end - t.start >= min_len]
    t = min(good, key=lambda t: t.start) if good else max(turns, key=lambda t: t.end - t.start)
    return (t.start, min(t.end, t.start + max_len))


def load_turns(rec: Recording) -> list[SpeakerTurn]:
    if not rec.turns_path.exists():
        return []
    return [SpeakerTurn(**d) for d in json.loads(rec.turns_path.read_text(encoding="utf-8"))]


def _sample_text(utts: list[Any]) -> str:
    if not utts:
        return ""
    longest = max(utts, key=lambda u: len(u.text))
    return longest.text if len(longest.text) <= 160 else longest.text[:157] + "..."


def speaker_overview(rec: Recording) -> list[SpeakerOverview]:
    speakers = rec.load_speakers()
    utterances = rec.load_utterances()
    turns = load_turns(rec)
    out = []
    for sid, info in speakers.items():
        if sid == SELF_SPEAKER_ID:
            continue
        spk_turns = [t for t in turns if t.speaker == sid]
        spk_utts = [u for u in utterances if u.speaker == sid]
        talk = sum(t.end - t.start for t in spk_turns) or sum(u.end - u.start for u in spk_utts)
        span = choose_sample_span(spk_turns)
        if span is None and spk_utts:
            u = max(spk_utts, key=lambda u: u.end - u.start)
            span = (u.start, min(u.end, u.start + 8.0))
        out.append(SpeakerOverview(sid, info.label, info.name, info.score, info.source, talk, len(spk_utts),
                                   _sample_text(spk_utts), span, info.suggested_name, info.suggested_score,
                                   info.note, info.self_suspect, info.preselect_name))
    out.sort(key=lambda o: (min((t.start for t in turns if t.speaker == o.id), default=1e12), o.id))
    return out


def self_overview(rec: Recording) -> SpeakerOverview:
    """The fixed row for your own (mic) track: not diarized, always attributed to you."""
    speakers = rec.load_speakers()
    me = speakers.get(SELF_SPEAKER_ID)
    mine = [u for u in rec.load_utterances() if u.speaker == SELF_SPEAKER_ID and u.track == "mic"]
    talk = sum(u.end - u.start for u in mine)
    good = [u for u in mine if u.end - u.start >= 3.0]
    pick = min(good, key=lambda u: u.start) if good else (max(mine, key=lambda u: u.end - u.start) if mine else None)
    span = (pick.start, min(pick.end, pick.start + 8.0)) if pick is not None else None
    name = me.display if me is not None else rec.self_person().display
    return SpeakerOverview(SELF_SPEAKER_ID, "Ich", name, None, "self", talk, len(mine), _sample_text(mine), span)


def _is_me(p: Person, me: Person) -> bool:
    from callrec.speakers.names import persons_match

    return p.key() == me.key() or persons_match(p, me)


def name_choices(rec: Recording, profiles: ProfileStore, all_profiles: bool = False,
                 exclude_speaker: str = "") -> list[str]:
    """This call's participants (without you) - those not yet assigned first - then, only in the
    "all_profiles" mode, the other known voice profiles."""
    me = rec.self_person()
    speakers = rec.load_speakers()
    taken = {s.name.casefold() for sid, s in speakers.items() if s.name and sid not in (SELF_SPEAKER_ID, exclude_speaker)}
    people = [p.name or p.email for p in rec.participants() if not _is_me(p, me)]
    people = [n for n in dict.fromkeys(people) if n]
    names = [n for n in people if n.casefold() not in taken] + [n for n in people if n.casefold() in taken]
    if all_profiles:
        names += [p.name for p in profiles.all_profiles() if not p.matches_person(me)]
    return list(dict.fromkeys(n for n in names if n))


def row_choices(rec: Recording, profiles: ProfileStore, ov: SpeakerOverview, all_profiles: bool = False) -> list[str]:
    """Dropdown of one speaker: participants, the labelled suggestion, "Das bin ich"."""
    names = name_choices(rec, profiles, all_profiles, exclude_speaker=ov.id)
    if ov.suggested_name and not any(n.casefold() == ov.suggested_name.casefold() for n in names):
        names.append(suggestion_label(ov.suggested_name))
    names.append(SELF_CHOICE)
    return names


def suggestion_label(name: str) -> str:
    return f"klingt wie {name}{SUGGESTION_SUFFIX}"


def parse_choice(text: str) -> tuple[str, bool]:
    """(name, is_self) of a dropdown entry or free text."""
    t = text.strip()
    if t == SELF_CHOICE:
        return "", True
    if t.startswith("klingt wie ") and t.endswith(SUGGESTION_SUFFIX):
        return t[len("klingt wie "):-len(SUGGESTION_SUFFIX)].strip(), False
    return t, False


@dataclass
class Assignment:
    speaker_id: str
    name: str  # "" = back to "Sprecher N"; may also be a dropdown entry (suggestion / SELF_CHOICE)
    save_profile: bool = True
    email: str = ""
    is_self: bool = False  # "das bin ich": the user's own voice on the remote track


def _participant_for(name: str, participants: list[Person]) -> Person | None:
    from callrec.speakers.names import same_person

    for p in participants:
        if same_person(p.name or p.email, "", name, "") or (p.email and p.email.casefold() == name.casefold()):
            return p
    return None


def apply_assignments(
    rec: Recording,
    assignments: list[Assignment],
    profiles: ProfileStore,
    rerender: Callable[[], object],
    min_learn_speech_s: float | None = None,
) -> list[str]:
    """Rename speakers, learn voice profiles, then re-render the transcript.

    * An automatic match that you leave unchanged and do not tick "Stimme merken" for stays
      automatic (it is not silently turned into a confirmed assignment, and nothing is learned).
    * A changed or confirmed assignment is "manual": it always wins over later re-runs. With
      "Stimme merken" its embedding is recorded for this recording+speaker - replacing whatever
      that speaker had taught any profile before (a correction undoes the earlier update).
    * A person assigned here who is not yet a participant of the recording becomes one.
    * "Das bin ich": the speaker's text is attributed to you (duplicates of the mic track dropped);
      it never teaches a profile.
    """
    from callrec.config import SpeakerConfig
    from callrec.speakers.diarize import speech_seconds

    min_learn = SpeakerConfig().min_learn_speech_s if min_learn_speech_s is None else min_learn_speech_s
    speakers = rec.load_speakers()
    before = {k: SpeakerInfo.from_dict(v.to_dict()) for k, v in speakers.items()}
    embeddings, model_id = rec.load_embeddings()
    participants = rec.participants()
    me = rec.self_person()
    speech = speech_seconds(load_turns(rec))
    rid = rec.uid
    messages: list[str] = []
    added: list[Person] = []
    for a in assignments:
        info = speakers.get(a.speaker_id)
        if info is None or a.speaker_id == SELF_SPEAKER_ID:
            continue
        name, is_self = parse_choice(a.name)
        is_self = is_self or a.is_self
        if is_self:
            profiles.forget(rid, a.speaker_id)
            info.name, info.email, info.profile_id, info.score = me.name or "Ich", me.email, "", None
            info.source = SOURCE_SELF_ECHO
            info.clear_hints()
            messages.append(f"{info.label}: als Ihre eigene Stimme markiert.")
            continue
        if not name:
            if profiles.forget(rid, a.speaker_id):
                messages.append(f"{info.label}: frühere Profil-Aktualisierung zurückgenommen.")
            info.name, info.email, info.profile_id, info.score, info.source = "", "", "", None, ""
            continue
        unchanged_auto = info.source == SOURCE_AUTO and info.name.casefold() == name.casefold()
        if unchanged_auto and not a.save_profile:
            continue  # stays an automatic (re-checkable) assignment
        person = _participant_for(name, participants + added)
        if person is None and not _is_me(Person(name, a.email), me):
            person = Person(name, a.email, "assigned")
            added.append(person)
        corrected = bool(info.name) and info.name.casefold() != name.casefold()
        info.name = name
        info.email = a.email or (person.email if person is not None else "")
        info.score = None
        info.source = SOURCE_MANUAL
        info.clear_hints()
        if a.save_profile:
            emb = embeddings.get(a.speaker_id)
            secs = speech.get(a.speaker_id, 0.0)
            if emb is None or not model_id:
                profiles.forget(rid, a.speaker_id)
                messages.append(f"Für {info.label} liegt kein Stimm-Embedding vor - kein Profil gespeichert.")
            elif secs < min_learn:
                profiles.forget(rid, a.speaker_id)
                messages.append(f"{info.label}: zu wenig Sprache ({secs:.0f} s, mindestens {min_learn:.0f} s) – "
                                f"kein Stimmprofil gespeichert.")
            else:
                prof = profiles.learn(name, info.email, emb, model_id, recording=rid, speaker=a.speaker_id,
                                      source="confirmed" if unchanged_auto else "manual", seconds=secs,
                                      title=rec.title(), folder=rec.folder.name)
                info.profile_id = prof.id
                messages.append(f"Stimmprofil für {name} gespeichert ({prof.samples} Probe(n)).")
        elif corrected or info.profile_id:
            if profiles.forget(rid, a.speaker_id):
                messages.append(f"{info.label}: frühere Profil-Aktualisierung zurückgenommen.")
            info.profile_id = ""
    if added:
        rec.update_meta(["participants"], [p.to_dict() for p in participants + added])
        messages.append("Als Teilnehmende ergänzt: " + ", ".join(p.display for p in added) + ".")
    rec.save_speakers(speakers)
    rerender()
    mark_summary_outdated(rec, before, rec.load_speakers())
    return messages


def speaker_names(speakers: dict[str, Any]) -> dict[str, str]:
    return {sid: s.display for sid, s in speakers.items()}


def mark_summary_outdated(rec: Recording, before: dict[str, Any], after: dict[str, Any]) -> bool:
    """The speaker names changed after the summary was written: flag it (main window, header of the
    hand-over) so the user can regenerate and hand over again."""
    if not rec.summary_path.exists() or speaker_names(before) == speaker_names(after):
        return False
    rec.update_meta(["processing", "summary", "speakers_outdated"], True)
    return True


def foreign_old_assignments(rec: Recording, participants: list[Person]) -> list[tuple[str, str, str]]:
    """Recordings identified by an earlier version: "manual" names of people who are not
    participants. Earlier versions turned every pre-filled automatic match into a manual assignment
    when "Übernehmen" was clicked, so these may never have been checked. (sid, label, name)."""
    from callrec.speakers.names import persons_match

    ident = (rec.load_meta().get("processing") or {}).get("identification")
    if isinstance(ident, dict) and ident.get("rule"):
        return []
    out = []
    for sid, s in rec.load_speakers().items():
        if sid == SELF_SPEAKER_ID or s.source != SOURCE_MANUAL or not s.name:
            continue
        if not any(persons_match(Person(s.name, s.email), p) for p in participants):
            out.append((sid, s.label, s.name))
    return out


def reset_speakers(rec: Recording, sids: list[str], profiles: ProfileStore | None = None) -> None:
    speakers = rec.load_speakers()
    for sid in sids:
        info = speakers.get(sid)
        if info is not None:
            info.name, info.email, info.profile_id, info.score, info.source = "", "", "", None, ""
            if profiles is not None:
                profiles.forget(rec.uid, sid)
    rec.save_speakers(speakers)


def archive_speakers(rec: Recording) -> Any:
    """Keep a copy of speakers.json before a re-check rewrites the automatic assignments."""
    import shutil
    from datetime import datetime

    if not rec.speakers_path.exists():
        return None
    dst = rec.data_dir / "archive" / f"speakers.{datetime.now():%Y%m%dT%H%M%S}.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rec.speakers_path, dst)
    return dst


def set_participants(rec: Recording, people: list[Person], by: str = "user") -> list[str]:
    """Replace the participants of a recording ("Teilnehmende bearbeiten"). The consent record is
    not changed (it keeps the list confirmed at the start). Returns German messages."""
    from datetime import datetime

    old = rec.participants()
    rec.update_meta(["participants"], [p.to_dict() for p in people])
    hist = list(rec.load_meta().get("participants_history") or [])
    hist.append({"at": datetime.now().astimezone().isoformat(timespec="seconds"), "by": by,
                 "before": [p.to_dict() for p in old], "after": [p.to_dict() for p in people]})
    rec.update_meta(["participants_history"], hist)
    return [f"{len(people)} Teilnehmende gespeichert."]


def parse_cli_assignments(items: list[str], no_profile: bool = False) -> list[Assignment]:
    """Parse ``S1=Anna Beispiel`` or ``"Sprecher 1=Anna"`` arguments."""
    out = []
    for item in items:
        if "=" not in item:
            raise ValueError(f"Erwartet ID=Name, erhalten: {item!r}")
        key, name = item.split("=", 1)
        key = key.strip()
        if key.lower().startswith("sprecher"):
            num = key.split()[-1]
            key = f"S{int(num)}"
        out.append(Assignment(speaker_id=key, name=name.strip(), save_profile=not no_profile))
    return out


def carry_over_assignments(
    old_turns: list[SpeakerTurn],
    old_speakers: dict[str, Any],
    new_turns: list[SpeakerTurn],
    min_fraction: float = 0.5,
) -> dict[str, Any]:
    """Map manual assignments of an earlier diarization onto new speaker ids by time overlap.

    A new speaker inherits a manual name if at least ``min_fraction`` of its speaking
    time overlaps with turns of one old, manually assigned speaker.
    """
    import dataclasses

    out: dict[str, Any] = {}
    new_ids = list(dict.fromkeys(t.speaker for t in new_turns))
    for nid in new_ids:
        mine = [t for t in new_turns if t.speaker == nid]
        total = sum(t.end - t.start for t in mine)
        if total <= 0:
            continue
        overlap: dict[str, float] = {}
        for a in mine:
            for b in old_turns:
                ov = min(a.end, b.end) - max(a.start, b.start)
                if ov > 0:
                    overlap[b.speaker] = overlap.get(b.speaker, 0.0) + ov
        if not overlap:
            continue
        best, ov = max(overlap.items(), key=lambda kv: kv[1])
        info = old_speakers.get(best)
        if info is not None and info.source in USER_DECIDED and info.name and ov / total >= min_fraction:
            out[nid] = dataclasses.replace(info, id=nid)
    return out


def rebuild_profile(profiles: ProfileStore, profile_id: str, recordings_root: Any) -> str:
    """Rebuild a profile only from the assignments you made in the recordings (manual speakers
    with this profile's person). Replaces everything it learned so far (incl. entries of earlier
    versions without origin). Returns a German message."""
    from callrec.recording import list_recordings
    from callrec.speakers.diarize import speech_seconds
    from callrec.speakers.profiles import SOURCE_WEIGHT, Contribution

    prof = profiles.get(profile_id)
    if prof is None:
        return "Profil nicht gefunden."
    found: list[Contribution] = []
    for rec in list_recordings(recordings_root):
        try:
            speakers = rec.load_speakers()
            embeddings, model_id = rec.load_embeddings()
        except (OSError, ValueError):
            continue
        if model_id != prof.model_id:
            continue
        speech: dict[str, float] | None = None
        for sid, info in speakers.items():
            if info.source != SOURCE_MANUAL or sid not in embeddings:
                continue
            if info.profile_id != prof.id and not prof.matches_person(Person(info.name, info.email)):
                continue
            if speech is None:
                speech = speech_seconds(load_turns(rec))
            found.append(Contribution(rec.uid, sid, "manual", embeddings[sid], SOURCE_WEIGHT["manual"],
                                      speech.get(sid, 0.0), title=rec.title(), folder=rec.folder.name))
    if not found:
        return (f"Für {prof.name} gibt es keine von Ihnen zugeordneten Stimmen mit gespeicherten Stimmmerkmalen - "
                "das Profil bleibt unverändert.")
    profiles.replace_contributions(prof.id, found)
    return f"{prof.name}: neu aufgebaut aus {len(found)} Zuordnung(en) in Ihren Aufnahmen."
