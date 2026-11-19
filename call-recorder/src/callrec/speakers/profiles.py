"""Local voice-profile store (biometric data!) and matching.

Profiles are stored in one file under %LOCALAPPDATA%\\CallRecorder, encrypted
with DPAPI on Windows (via msal-extensions' FilePersistenceWithDataProtection),
so only the same Windows user on the same machine can read them.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from callrec.models import Person

log = logging.getLogger(__name__)

MAX_CENTROID_WEIGHT = 20  # new samples always move the centroid by >= 1/21


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
        tmp.replace(self.path)

    def load(self) -> str:
        return self.path.read_text(encoding="utf-8")


def secure_persistence(path: Path) -> Persistence:
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        from msal_extensions import FilePersistenceWithDataProtection

        return FilePersistenceWithDataProtection(str(path), entropy="callrec-voiceprints")  # type: ignore[no-any-return]
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
class VoiceProfile:
    id: str
    name: str
    email: str
    model_id: str
    embedding: np.ndarray
    samples: int
    created_at: str
    updated_at: str

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
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> VoiceProfile:
        return VoiceProfile(
            id=str(d["id"]),
            name=str(d["name"]),
            email=str(d.get("email", "")),
            model_id=str(d["model_id"]),
            embedding=np.asarray(d["embedding"], dtype=np.float32),
            samples=int(d.get("samples", 1)),
            created_at=str(d.get("created_at", "")),
            updated_at=str(d.get("updated_at", "")),
        )

    def matches_person(self, person: Person) -> bool:
        if person.email and self.email and person.email.lower() == self.email.lower():
            return True
        return bool(person.name) and person.name.casefold() == self.name.casefold()


@dataclass
class Match:
    profile: VoiceProfile
    similarity: float  # raw cosine similarity
    score: float  # similarity incl. invitee bonus


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
        payload = {"version": 1, "profiles": [p.to_dict() for p in profiles.values()]}
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

    # -- mutation ----------------------------------------------------------
    def add_sample(self, name: str, email: str, embedding: np.ndarray, model_id: str) -> VoiceProfile:
        """Create the profile or move its centroid towards the new embedding."""
        emb = _normalize(embedding)
        with self._lock:
            existing = self.find(name, email, model_id)
            now = _now()
            if existing is None:
                prof = VoiceProfile(
                    id=uuid.uuid4().hex, name=name, email=email, model_id=model_id,
                    embedding=emb, samples=1, created_at=now, updated_at=now,
                )
                self._load()[prof.id] = prof
            else:
                if existing.embedding.shape != emb.shape:
                    raise ValueError("Embedding-Dimension passt nicht zum Profil")
                w = min(existing.samples, MAX_CENTROID_WEIGHT)
                existing.embedding = _normalize(existing.embedding * w + emb)
                existing.samples += 1
                existing.updated_at = now
                if email and not existing.email:
                    existing.email = email
                prof = existing
            self._save()
            return prof

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
    def match(
        self,
        embeddings: dict[str, np.ndarray],
        model_id: str,
        candidates: list[Person],
        threshold: float,
        invitee_bonus: float = 0.05,
        exclude: list[Person] | None = None,
        reuse_margin: float = 0.1,
    ) -> dict[str, Match]:
        """Assign diarized speakers to profiles.

        Greedy one-to-one by score first. Because diarization sometimes splits
        one person into two clusters, a remaining cluster may still reuse an
        already assigned profile if its score exceeds ``threshold + reuse_margin``.
        Profiles of invited people get ``invitee_bonus`` added to their
        similarity; profiles matching ``exclude`` (the user, who is on the
        mic track) are never assigned to remote speakers.
        """
        excluded: list[Person] = exclude or []
        with self._lock:
            profiles = [
                p for p in self._load().values()
                if p.model_id == model_id and not any(p.matches_person(x) for x in excluded)
            ]
        pairs: list[tuple[float, float, str, VoiceProfile]] = []
        for spk, emb in embeddings.items():
            for p in profiles:
                if p.embedding.shape != np.asarray(emb).reshape(-1).shape:
                    continue
                sim = cosine(np.asarray(emb).reshape(-1), p.embedding)
                bonus = invitee_bonus if any(p.matches_person(c) for c in candidates) else 0.0
                score = sim + bonus
                if score >= threshold:
                    pairs.append((score, sim, spk, p))
        pairs.sort(key=lambda x: (-x[0], x[2], x[3].name))
        result: dict[str, Match] = {}
        used: set[str] = set()
        for score, sim, spk, p in pairs:
            if spk in result or p.id in used:
                continue
            result[spk] = Match(profile=p, similarity=sim, score=score)
            used.add(p.id)
        for score, sim, spk, p in pairs:  # second pass: split clusters of one person
            if spk not in result and score >= threshold + reuse_margin:
                result[spk] = Match(profile=p, similarity=sim, score=score)
        return result
