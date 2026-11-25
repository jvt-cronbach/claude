"""One-time repair of the voice profiles after the update to 0.4.1.

Up to 0.4.0 profiles often did not reach the people they belong to: a name typed in a call
without a calendar entry ("Anna") was never linked to the calendar identity ("Müller, Anna"
<anna.mueller@...>); the same person under a second address got a second profile, which then
competed with the first; profiles of versions before the provenance change hold entries of
unknown origin. The repair

1. merges profiles of the same person (the same address with compatible names, or the same full name
   where one side has no address) unless their voices are clearly different - never the same name under
   two different addresses (two colleagues can share a name),
2. links profiles typed as a first name to the one participant of the recordings they were
   assigned in (only when every such recording points to the same person),
3. rebuilds each profile from your own assignments (manual/confirmed) in the recordings that still
   have speaker data and that the profile already learned from (an assignment made without "Stimme
   merken" is never added) - entries of an earlier version without origin are dropped when at least
   two of your own assignments exist; automatic entries of recordings without speaker data stay.

Before anything changes the whole profile store is backed up (:mod:`callrec.speakers.backup`).

Recordings older than ``retention.max_age_days`` (default 7 days) no longer have speaker data
(the whole data folder is deleted), so only recent recordings can contribute.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from callrec import paths
from callrec.models import SOURCE_MANUAL, Person
from callrec.speakers.names import name_tokens, probable_same
from callrec.speakers.profiles import SOURCE_WEIGHT, USER_SOURCES, Contribution, ProfileStore

MARKER = "speaker-repair-0.4.1.json"


@dataclass
class RepairPlan:
    merges: list[tuple[str, str, str]] = field(default_factory=list)  # (keep id, other id, text)
    links: list[tuple[str, Person, str]] = field(default_factory=list)  # (profile id, participant, text)
    rebuilds: list[tuple[str, list[Contribution], str]] = field(default_factory=list)  # (profile id, contribs, text)
    recordings: int = 0
    with_speaker_data: int = 0
    notes: list[str] = field(default_factory=list)  # what was deliberately left alone

    @property
    def empty(self) -> bool:
        return not (self.merges or self.links or self.rebuilds)

    def lines(self) -> list[str]:
        out = [f"{self.with_speaker_data} von {self.recordings} Aufnahmen haben noch Sprecherdaten (ältere wurden "
               "durch das automatische Löschen entfernt und können nicht beitragen)."]
        out += [t for _a, _b, t in self.merges] + [t for _p, _x, t in self.links] + [t for _p, _c, t in self.rebuilds]
        out += self.notes
        if self.empty:
            out.append("Keine Änderung nötig.")
        return out


def marker_path() -> Path:
    return paths.data_dir() / MARKER


def is_due() -> bool:
    return not marker_path().exists()


def mark_done(result: str) -> None:
    p = marker_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"result": result}), encoding="utf-8")


MERGE_MIN_VOICE = 0.35  # below this the two profiles' closest references are clearly different voices


def _same_person_for_merge(a: Any, b: Any) -> str:
    """Why two profiles are the same person ("" = not sure enough to merge them without asking).

    Only an identity that cannot belong to two people: the same address *and* compatible names (a shared
    mailbox - "info@kunde.de" - used by two people is not one person), or the same full name where one
    side has no address at all (a name typed once, then the calendar identity). The same full name under
    two different addresses is NOT merged automatically - two colleagues can share a name; that link is
    offered as a pre-selection in the naming dialog instead (``probable_same`` "alias")."""
    ea, eb = a.email.strip().casefold(), b.email.strip().casefold()
    ta, tb = name_tokens(a.name), name_tokens(b.name)
    if ea and eb:
        return "same address" if ea == eb and (not ta or not tb or ta == tb or ta < tb or tb < ta) else ""
    if (ea or eb) and len(ta) >= 2 and ta == tb:
        return "typed name"
    if not ea and not eb and ta and ta == tb:
        return "same name"
    return ""


def _voices_compatible(a: Any, b: Any) -> bool:
    """No merge of two profiles whose voices are clearly different (one would teach the other a stranger)."""
    import numpy as np

    from callrec.speakers.profiles import _normalize

    ra = [_normalize(c.embedding) for c in a.contributions]
    rb = [_normalize(c.embedding) for c in b.contributions]
    pairs = [float(np.dot(x, y)) for x in ra for y in rb if x.shape == y.shape]
    return not pairs or max(pairs) >= MERGE_MIN_VOICE


def plan(store: ProfileStore, recordings_root: Path) -> RepairPlan:
    from callrec.recording import list_recordings
    from callrec.review import load_turns
    from callrec.speakers.diarize import speech_seconds

    rp = RepairPlan()
    profiles = store.all_profiles()
    # 1. the same person twice
    gone: set[str] = set()
    for i, a in enumerate(profiles):
        for b in profiles[i + 1:]:
            if a.id in gone or b.id in gone or a.model_id != b.model_id:
                continue
            why = _same_person_for_merge(a, b)
            if why and not _voices_compatible(a, b):
                rp.notes.append(f"{a.name} <{a.email or '–'}> und {b.name} <{b.email or '–'}>: nicht zusammengeführt "
                                "– die Stimmen sind deutlich verschieden.")
                continue
            if why:
                keep, other = (a, b) if len(a.contributions) >= len(b.contributions) else (b, a)
                rp.merges.append((keep.id, other.id, f"{other.name} <{other.email or '–'}> und {keep.name} "
                                                     f"<{keep.email or '–'}> sind dieselbe Person: zusammengeführt."))
                gone.add(other.id)
    # 2./3. what the recordings say
    found: dict[str, list[Contribution]] = {}
    link_votes: dict[str, list[Person]] = {}
    for rec in list_recordings(recordings_root):
        rp.recordings += 1
        try:
            speakers = rec.load_speakers()
            embeddings, model_id = rec.load_embeddings()
            participants = rec.participants()
        except (OSError, ValueError):
            continue
        if embeddings:
            rp.with_speaker_data += 1
        speech: dict[str, float] | None = None
        for sid, info in speakers.items():
            if info.source != SOURCE_MANUAL or not info.name:
                continue
            person = Person(info.name, info.email)
            for p in profiles:
                if p.id in gone or (p.id != info.profile_id and not p.matches_person(person)):
                    continue
                if not p.email:
                    hits = [q for q in participants if probable_same(p.name, "", q.name, q.email) == "first_name"]
                    if len(hits) == 1:
                        link_votes.setdefault(p.id, []).append(hits[0])
                # only what the profile already learned from this speaker: an assignment you made without
                # "Stimme merken" (or with too little speech, or a fragment) was deliberately not learned
                held = any(c.recording == rec.uid and c.speaker == sid for c in p.contributions)
                if sid in embeddings and model_id == p.model_id and held and not info.minor:
                    if speech is None:
                        speech = speech_seconds(load_turns(rec))
                    found.setdefault(p.id, []).append(Contribution(
                        rec.uid, sid, "manual", embeddings[sid], SOURCE_WEIGHT["manual"], speech.get(sid, 0.0),
                        title=rec.title(), folder=rec.folder.name))
    for pid, votes in link_votes.items():
        keys = {v.key() for v in votes}
        prof = store.get(pid)
        if prof is not None and len(keys) == 1:
            person = votes[0]
            rp.links.append((pid, person, f"Profil „{prof.name}“ ist {person.name} <{person.email}> "
                                          f"({len(votes)} Aufnahme(n)): verknüpft."))
    for p in profiles:
        if p.id in gone:
            continue
        mine = found.get(p.id, [])
        new = _rebuilt(p.contributions, mine)
        before = {(c.recording, c.speaker) for c in p.contributions}
        added = sum(1 for c in mine if (c.recording, c.speaker) not in before)
        dropped_legacy = sum(1 for c in p.contributions if c.source == "legacy") - sum(
            1 for c in new if c.source == "legacy")
        if added or dropped_legacy:
            rp.rebuilds.append((p.id, mine, f"{p.name}: {added} eigene Zuordnung(en) aus den Aufnahmen übernommen"
                                + (f", {dropped_legacy} Eintrag/Einträge einer früheren Version ohne Herkunft entfernt"
                                   if dropped_legacy else "") + f" – jetzt {len(new)} Stimmprobe(n)."))
    return rp


def _rebuilt(current: list[Contribution], mine: list[Contribution]) -> list[Contribution]:
    """Your assignments found in the recordings + everything else the profile holds; entries of an
    earlier version (origin unknown, possibly wrong) go once at least two of your own are there."""
    have = {(c.recording, c.speaker) for c in mine}
    rest = [c for c in current if (c.recording, c.speaker) not in have or not c.recording]
    users = len(mine) + sum(1 for c in rest if c.source in USER_SOURCES)
    if users >= 2:
        rest = [c for c in rest if c.source != "legacy"]
    return mine + rest


def apply(store: ProfileStore, rp: RepairPlan) -> list[str]:
    """Carry out the plan - after a backup of the whole profile store (one-click restore)."""
    from callrec.speakers import backup

    out: list[str] = []
    if not rp.empty:
        backup.create(store, [], "Reparatur der Stimmprofile")
    for keep, other, text in rp.merges:
        if store.merge(keep, other) is not None:
            out.append(text)
    for pid, person, text in rp.links:
        if store.add_alias(pid, person) is not None:
            out.append(text)
    for pid, mine, text in rp.rebuilds:
        prof = store.get(pid)  # after the merges: rebuild on what the profile holds now
        if prof is None:
            continue
        store.replace_contributions(pid, _rebuilt(prof.contributions, mine))
        out.append(text)
    mark_done("applied")
    return out or ["Keine Änderung nötig."]
