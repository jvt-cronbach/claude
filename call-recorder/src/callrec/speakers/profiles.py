"""Local voice-profile store (biometric data!) and matching.

Profiles are stored in one file under %LOCALAPPDATA%\\CallRecorder, encrypted
with DPAPI on Windows (via msal-extensions' FilePersistenceWithDataProtection),
so only the same Windows user on the same machine can read them.

What a profile learned is kept per origin (``Contribution``: recording + speaker id + source),
so a wrong assignment can be undone exactly: correcting a speaker removes the embedding it had
added, re-running the identification replaces (never repeats) the automatic contribution of that
recording, and the profile dialog shows and resets what was learned from which recording.
Profiles of versions before this one carry one "legacy" contribution of unknown origin.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from callrec.models import Person
from callrec.speakers.names import persons_match, same_person

log = logging.getLogger(__name__)

MAX_CENTROID_WEIGHT = 20  # weight cap of the legacy centroid (profiles of earlier versions)
MAX_CONTRIBUTIONS = 40  # older contributions are folded into the legacy centroid (origin then lost)
# weight of one contribution in the centroid: an automatic match counts half of a user decision
SOURCE_WEIGHT = {"manual": 1.0, "confirmed": 1.0, "auto": 0.5}
USER_SOURCES = ("manual", "confirmed")


class Persistence(Protocol):
    def save(self, content: str) -> None: ...

    def load(self) -> str: ...


class PlainFilePersistence:
    """Unencrypted file (non-Windows development and tests)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, content: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        from callrec.recording import replace_file

        replace_file(tmp, self.path)

    def load(self) -> str:
        return self.path.read_text(encoding="utf-8")


class AtomicProtectedFile:
    """DPAPI-encrypted file written atomically (temp file + fsync + os.replace).

    msal-extensions' FilePersistenceWithDataProtection truncates and rewrites the file in
    place, so a crash or full disk while saving would leave an undecryptable blob and all
    voice profiles would be lost. The ciphertext format is the same (CryptProtectData with
    the same entropy), so existing files stay readable.
    """

    def __init__(self, path: Path, agent: Any) -> None:
        self.path = path
        self._agent = agent  # object with protect(str) -> bytes / unprotect(bytes) -> str

    def save(self, content: str) -> None:
        import os

        data = self._agent.protect(content)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        from callrec.recording import replace_file

        replace_file(tmp, self.path)

    def load(self) -> str:
        data = self.path.read_bytes()  # FileNotFoundError if there are no profiles yet
        return str(self._agent.unprotect(data))


def secure_persistence(path: Path) -> Persistence:
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        from msal_extensions.windows import WindowsDataProtectionAgent

        return AtomicProtectedFile(path, WindowsDataProtectionAgent(entropy="callrec-voiceprints"))
    log.warning("Nicht-Windows-System: Stimmprofile werden UNVERSCHLÜSSELT gespeichert (%s)", path)
    return PlainFilePersistence(path)


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


@dataclass
class Contribution:
    """One embedding a profile learned, with its origin."""

    recording: str  # recording folder name; "" = learned before origins were recorded
    speaker: str  # speaker id in that recording ("S2"); "" for legacy
    source: str  # manual | confirmed | auto | legacy
    embedding: np.ndarray
    weight: float
    seconds: float = 0.0
    at: str = ""
    title: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"recording": self.recording, "speaker": self.speaker, "source": self.source,
                "embedding": [round(float(x), 6) for x in self.embedding], "weight": round(self.weight, 4),
                "seconds": round(self.seconds, 1), "at": self.at, "title": self.title}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Contribution:
        return Contribution(str(d.get("recording", "")), str(d.get("speaker", "")), str(d.get("source", "manual")),
                            np.asarray(d["embedding"], dtype=np.float32), float(d.get("weight", 1.0)),
                            float(d.get("seconds", 0.0)), str(d.get("at", "")), str(d.get("title", "")))


@dataclass
class VoiceProfile:
    id: str
    name: str
    email: str
    model_id: str
    embedding: np.ndarray
    samples: int
    created_at: str
    updated_at: str
    contributions: list[Contribution] = field(default_factory=list)

    def recompute(self) -> None:
        if not self.contributions:
            return
        acc = np.zeros_like(self.contributions[0].embedding, dtype=np.float32)
        for c in self.contributions:
            acc += c.weight * _normalize(c.embedding)
        self.embedding = _normalize(acc)
        self.samples = sum(int(round(c.weight)) if c.source == "legacy" else 1 for c in self.contributions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "model_id": self.model_id,
            "embedding": [round(float(x), 6) for x in self.embedding],
            "samples": self.samples,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "contributions": [c.to_dict() for c in self.contributions],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> VoiceProfile:
        p = VoiceProfile(
            id=str(d["id"]),
            name=str(d["name"]),
            email=str(d.get("email", "")),
            model_id=str(d["model_id"]),
            embedding=np.asarray(d["embedding"], dtype=np.float32),
            samples=int(d.get("samples", 1)),
            created_at=str(d.get("created_at", "")),
            updated_at=str(d.get("updated_at", "")),
            contributions=[Contribution.from_dict(c) for c in d.get("contributions") or []],
        )
        if not p.contributions:
            # profile of an earlier version: everything it learned, origin unknown
            p.contributions = [Contribution("", "", "legacy", p.embedding.copy(),
                                            float(min(max(p.samples, 1), MAX_CENTROID_WEIGHT)), at=p.updated_at)]
            p.samples = max(p.samples, 1)
        return p

    def matches_person(self, person: Person) -> bool:
        return same_person(self.name, self.email, person.name, person.email)

    @property
    def person(self) -> Person:
        return Person(self.name, self.email)

    def origins(self) -> list[Contribution]:
        return list(self.contributions)


@dataclass
class Match:
    profile: VoiceProfile
    similarity: float  # raw cosine similarity
    score: float  # similarity (+ participant bonus in the "all profiles" mode)


@dataclass
class Decision:
    """Outcome of the matching for one diarized speaker."""

    match: Match | None = None  # automatic assignment
    suggestion: Match | None = None  # shown in the dialog only ("klingt wie ..."), never applied
    reason: str = ""  # why there is no automatic assignment (German, for dialog/log)
    candidates: list[tuple[str, float]] = field(default_factory=list)  # (name, similarity) best first


class ProfileStore:
    def __init__(self, persistence: Persistence) -> None:
        self._p = persistence
        self._profiles: dict[str, VoiceProfile] | None = None
        self._lock = threading.RLock()

    # -- persistence -----------------------------------------------------
    def _load(self) -> dict[str, VoiceProfile]:
        if self._profiles is None:
            try:
                raw = self._p.load()
                data = json.loads(raw) if raw else {}
            except OSError as exc:
                # Missing file is fine; anything else (e.g. DPAPI decryption
                # failure) must not lead to silently overwriting the profiles.
                if isinstance(exc, FileNotFoundError) or type(exc).__name__ == "PersistenceNotFound":
                    data = {}
                else:
                    raise
            self._profiles = {d["id"]: VoiceProfile.from_dict(d) for d in data.get("profiles", [])}
        return self._profiles

    def _save(self) -> None:
        profiles = self._load()
        payload = {"version": 2, "profiles": [p.to_dict() for p in profiles.values()]}
        self._p.save(json.dumps(payload, ensure_ascii=False))

    # -- queries -----------------------------------------------------------
    def all_profiles(self) -> list[VoiceProfile]:
        with self._lock:
            return sorted(self._load().values(), key=lambda p: p.name.casefold())

    def get(self, profile_id: str) -> VoiceProfile | None:
        with self._lock:
            return self._load().get(profile_id)

    def find(self, name: str, email: str, model_id: str) -> VoiceProfile | None:
        person = Person(name=name, email=email)
        with self._lock:
            for p in self._load().values():
                if p.model_id == model_id and p.matches_person(person):
                    return p
        return None

    def learned_from(self, recording: str) -> list[tuple[VoiceProfile, Contribution]]:
        with self._lock:
            return [(p, c) for p in self._load().values() for c in p.contributions if c.recording == recording]

    # -- mutation ----------------------------------------------------------
    def _drop(self, recording: str, speaker: str | None, sources: tuple[str, ...] | None = None) -> list[str]:
        """Remove contributions of ``recording`` (and ``speaker``); empty profiles are deleted."""
        changed: list[str] = []
        profiles = self._load()
        for p in list(profiles.values()):
            keep = [c for c in p.contributions
                    if not (c.recording == recording and (speaker is None or c.speaker == speaker)
                            and (sources is None or c.source in sources))]
            if len(keep) != len(p.contributions):
                changed.append(p.name)
                p.contributions = keep
                if not keep:
                    del profiles[p.id]  # learned only from what was just undone
                else:
                    p.recompute()
                    p.updated_at = _now()
        return changed

    def learn(self, name: str, email: str, embedding: np.ndarray, model_id: str, *, recording: str,
              speaker: str, source: str, seconds: float = 0.0, title: str = "") -> VoiceProfile:
        """Record what speaker ``speaker`` of ``recording`` sounds like, for the person ``name``.

        An earlier contribution of the same recording+speaker (to any profile) is replaced, so a
        correction undoes the earlier update and re-running never counts one recording twice."""
        if source not in SOURCE_WEIGHT:
            raise ValueError(f"Unbekannte Herkunft {source!r}")
        emb = _normalize(embedding)
        with self._lock:
            if recording:
                # a user decision replaces anything this speaker taught before; an automatic match
                # only replaces an earlier automatic one
                self._drop(recording, speaker, None if source in USER_SOURCES else ("auto",))
            existing = self.find(name, email, model_id)
            now = _now()
            contrib = Contribution(recording, speaker, source, emb, SOURCE_WEIGHT[source], seconds, now, title)
            if existing is None:
                if source not in USER_SOURCES:
                    raise ValueError("Ein Stimmprofil wird nur aus einer Zuordnung durch Sie angelegt")
                prof = VoiceProfile(
                    id=uuid.uuid4().hex, name=name, email=email, model_id=model_id,
                    embedding=emb, samples=1, created_at=now, updated_at=now, contributions=[contrib],
                )
                self._load()[prof.id] = prof
            else:
                if existing.embedding.shape != emb.shape:
                    raise ValueError("Embedding-Dimension passt nicht zum Profil")
                existing.contributions.append(contrib)
                self._fold_old(existing)
                existing.updated_at = now
                if email and not existing.email:
                    existing.email = email
                prof = existing
            prof.recompute()
            self._save()
            return prof

    @staticmethod
    def _fold_old(p: VoiceProfile) -> None:
        extra = len(p.contributions) - MAX_CONTRIBUTIONS
        if extra <= 0:
            return
        movable = [c for c in p.contributions if c.source != "legacy"][:extra]
        legacy = next((c for c in p.contributions if c.source == "legacy"), None)
        acc = (legacy.embedding * legacy.weight) if legacy is not None else np.zeros_like(movable[0].embedding)
        weight = legacy.weight if legacy is not None else 0.0
        for c in movable:
            acc = acc + c.weight * _normalize(c.embedding)
            weight += c.weight
        new_legacy = Contribution("", "", "legacy", _normalize(acc), min(weight, float(MAX_CENTROID_WEIGHT)), at=_now())
        p.contributions = [new_legacy] + [c for c in p.contributions if c is not legacy and c not in movable]

    def add_sample(self, name: str, email: str, embedding: np.ndarray, model_id: str) -> VoiceProfile:
        """A user-made assignment without a known origin (CLI/tests)."""
        return self.learn(name, email, embedding, model_id, recording="", speaker=uuid.uuid4().hex[:8], source="manual")

    def forget(self, recording: str, speaker: str | None = None, sources: tuple[str, ...] | None = None) -> list[str]:
        """Undo what ``recording`` (one ``speaker`` or all) taught the profiles. Returns profile names."""
        with self._lock:
            changed = self._drop(recording, speaker, sources)
            if changed:
                self._save()
            return changed

    def remove_contribution(self, profile_id: str, index: int) -> VoiceProfile | None:
        """Remove one contribution; returns the profile, or None if it became empty and was deleted."""
        with self._lock:
            p = self._load().get(profile_id)
            if p is None or not 0 <= index < len(p.contributions):
                return p
            del p.contributions[index]
            if not p.contributions:
                del self._load()[profile_id]
                self._save()
                return None
            p.recompute()
            p.updated_at = _now()
            self._save()
            return p

    def reset_to_confirmed(self, profile_id: str) -> VoiceProfile | None:
        """Keep only what you assigned or confirmed (drops automatic and legacy contributions)."""
        with self._lock:
            p = self._load().get(profile_id)
            if p is None:
                return None
            keep = [c for c in p.contributions if c.source in USER_SOURCES]
            if not keep:
                del self._load()[profile_id]
                self._save()
                return None
            p.contributions = keep
            p.recompute()
            p.updated_at = _now()
            self._save()
            return p

    def replace_contributions(self, profile_id: str, contributions: list[Contribution]) -> VoiceProfile | None:
        with self._lock:
            p = self._load().get(profile_id)
            if p is None:
                return None
            if not contributions:
                del self._load()[profile_id]
                self._save()
                return None
            p.contributions = list(contributions)
            self._fold_old(p)
            p.recompute()
            p.updated_at = _now()
            self._save()
            return p

    def delete(self, key: str) -> list[VoiceProfile]:
        """Delete by id, name (case-insensitive) or email. Returns the deleted profiles."""
        k = key.strip().casefold()
        with self._lock:
            profiles = self._load()
            victims = [p for p in profiles.values() if k in (p.id.casefold(), p.name.casefold(), p.email.casefold())]
            for p in victims:
                del profiles[p.id]
            if victims:
                self._save()
            return victims

    def delete_all(self) -> int:
        with self._lock:
            n = len(self._load())
            self._profiles = {}
            self._save()
            return n

    # -- matching ----------------------------------------------------------
    def decide(
        self,
        embeddings: dict[str, np.ndarray],
        model_id: str,
        *,
        participants: list[Person] | None,
        threshold: float,
        margin: float = 0.0,
        exclude: list[Person] | None = None,
        not_eligible: dict[str, str] | None = None,
        bonus: float = 0.0,
        preferred: list[Person] | None = None,
        ignore_recording: str = "",
    ) -> dict[str, Decision]:
        """Decide for each diarized speaker: automatic assignment, suggestion, or nothing.

        ``participants`` = the people of this call; only their profiles may be assigned
        automatically. ``None`` = every profile may be assigned (relaxed mode of earlier
        versions; participants then get ``bonus``). The best profile is assigned only if

        * its similarity reaches ``threshold``,
        * it is better than every *other* profile (participant or not) by at least ``margin`` -
          a voice that sounds as much like somebody else is not decided automatically,
        * no other speaker of this call is closer to it (one profile per call; diarization
          splitting one person into two clusters is left to the user, who sees a suggestion).

        A non-participant profile that is the closest one is returned as ``suggestion`` only.
        ``not_eligible`` maps speakers to a reason (e.g. too little speech) - no assignment.
        Profiles matching ``exclude`` (the user, on the mic track) are never considered.
        ``ignore_recording``: compare against what the profiles learned from *other* recordings
        only - a recording must not confirm itself with its own earlier contribution.
        """
        import copy

        excluded = exclude or []
        with self._lock:
            profiles = []
            for p in self._load().values():
                if p.model_id != model_id or any(p.matches_person(x) for x in excluded):
                    continue
                if ignore_recording and any(c.recording == ignore_recording for c in p.contributions):
                    rest = [c for c in p.contributions if c.recording != ignore_recording]
                    if not rest:
                        continue
                    p = copy.copy(p)
                    p.contributions = rest
                    p.recompute()
                profiles.append(p)
        allowed_ids = {p.id for p in profiles
                       if participants is None or any(p.matches_person(x) for x in participants)}
        preferred_ids = {p.id for p in profiles if participants is None and any(
            p.matches_person(x) for x in (preferred or []))}
        out: dict[str, Decision] = {}
        claims: list[tuple[float, str, Match]] = []
        for spk, emb in embeddings.items():
            d = Decision()
            out[spk] = d
            vec = np.asarray(emb).reshape(-1)
            sims = sorted(((cosine(vec, p.embedding), p) for p in profiles if p.embedding.shape == vec.shape),
                          key=lambda x: (-x[0], x[1].name))
            d.candidates = [(p.name, round(s, 4)) for s, p in sims[:3]]
            if not sims:
                d.reason = "keine passenden Stimmprofile"
                continue
            if not_eligible and spk in not_eligible:
                d.reason = not_eligible[spk]
                continue
            best_sim, best = sims[0]
            allowed = [(s, p) for s, p in sims if p.id in allowed_ids]
            if best.id not in allowed_ids and best_sim >= threshold:
                d.suggestion = Match(best, best_sim, best_sim)
                d.reason = f"klingt wie {best.name} (kein Teilnehmer dieses Termins)"
                continue
            if not allowed:
                d.reason = "keine Stimmprofile von Teilnehmenden dieses Termins"
                continue
            sim, prof = allowed[0]
            score = sim + (bonus if prof.id in preferred_ids else 0.0)
            if score < threshold:
                d.reason = f"keine sichere Übereinstimmung (beste: {prof.name} {sim:.2f})"
                continue
            others = [s for s, p in sims if p.id != prof.id]
            if others and sim - others[0] < margin:
                rival = next(p for s, p in sims if p.id != prof.id)
                d.reason = f"nicht eindeutig ({prof.name} {sim:.2f} / {rival.name} {others[0]:.2f})"
                d.suggestion = Match(prof, sim, score)
                continue
            claims.append((score, spk, Match(prof, sim, score)))
        used: dict[str, str] = {}
        for _score, spk, m in sorted(claims, key=lambda x: (-x[0], x[1])):
            if m.profile.id in used:
                out[spk].suggestion = m
                out[spk].reason = f"gleiche Stimme wie {used[m.profile.id]}? ({m.profile.name} {m.similarity:.2f})"
                continue
            used[m.profile.id] = spk
            out[spk].match = m
        return out

    def match(
        self,
        embeddings: dict[str, np.ndarray],
        model_id: str,
        candidates: list[Person],
        threshold: float,
        invitee_bonus: float = 0.0,
        exclude: list[Person] | None = None,
        margin: float = 0.0,
        participants_only: bool = False,
    ) -> dict[str, Match]:
        """Automatic assignments only (see :meth:`decide`). ``participants_only`` restricts the
        profiles to ``candidates``; otherwise ``candidates`` only get ``invitee_bonus``."""
        dec = self.decide(embeddings, model_id, participants=list(candidates) if participants_only else None,
                          threshold=threshold, margin=margin, exclude=exclude, bonus=invitee_bonus,
                          preferred=None if participants_only else list(candidates))
        return {k: d.match for k, d in dec.items() if d.match is not None}


def person_in(person: Person, people: list[Person]) -> bool:
    return any(persons_match(person, p) for p in people)
