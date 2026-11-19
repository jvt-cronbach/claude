"""Logic behind the post-call speaker review (UI-independent)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from callrec.models import SELF_SPEAKER_ID, Person, SpeakerTurn
from callrec.recording import Recording
from callrec.speakers.profiles import ProfileStore


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
        sample_text = ""
        if spk_utts:
            longest = max(spk_utts, key=lambda u: len(u.text))
            sample_text = longest.text if len(longest.text) <= 160 else longest.text[:157] + "..."
        span = choose_sample_span(spk_turns)
        if span is None and spk_utts:
            u = max(spk_utts, key=lambda u: u.end - u.start)
            span = (u.start, min(u.end, u.start + 8.0))
        out.append(SpeakerOverview(sid, info.label, info.name, info.score, info.source, talk, len(spk_utts), sample_text, span))
    out.sort(key=lambda o: (min((t.start for t in turns if t.speaker == o.id), default=1e12), o.id))
    return out


def name_choices(rec: Recording, profiles: ProfileStore) -> list[str]:
    """Invitees first (without the user), then known voice profiles."""
    me = rec.self_person()
    names: list[str] = []
    for p in rec.participants():
        if p.key() == me.key() or (me.name and p.name.casefold() == me.name.casefold()):
            continue
        names.append(p.name)
    names += [p.name for p in profiles.all_profiles()]
    return list(dict.fromkeys(n for n in names if n))


@dataclass
class Assignment:
    speaker_id: str
    name: str  # "" = back to "Sprecher N"
    save_profile: bool = True
    email: str = ""


def _email_for(name: str, participants: list[Person]) -> str:
    for p in participants:
        if p.name.casefold() == name.casefold():
            return p.email
    return ""


def apply_assignments(
    rec: Recording,
    assignments: list[Assignment],
    profiles: ProfileStore,
    rerender: Callable[[], object],
) -> list[str]:
    """Rename speakers, learn voice profiles, then re-render the transcript."""
    speakers = rec.load_speakers()
    embeddings, model_id = rec.load_embeddings()
    participants = rec.participants()
    messages: list[str] = []
    for a in assignments:
        info = speakers.get(a.speaker_id)
        if info is None or a.speaker_id == SELF_SPEAKER_ID:
            continue
        name = a.name.strip()
        if not name:
            info.name, info.email, info.profile_id, info.score, info.source = "", "", "", None, ""
            continue
        info.name = name
        info.email = a.email or _email_for(name, participants)
        info.score = None
        info.source = "manual"
        if a.save_profile:
            emb = embeddings.get(a.speaker_id)
            if emb is not None and model_id:
                prof = profiles.add_sample(name, info.email, emb, model_id)
                info.profile_id = prof.id
                messages.append(f"Stimmprofil für {name} gespeichert ({prof.samples} Probe(n)).")
            else:
                messages.append(f"Für {info.label} liegt kein Stimm-Embedding vor - kein Profil gespeichert.")
    rec.save_speakers(speakers)
    rerender()
    return messages


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
        if info is not None and info.source == "manual" and info.name and ov / total >= min_fraction:
            out[nid] = dataclasses.replace(info, id=nid)
    return out
