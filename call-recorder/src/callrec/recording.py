"""One recording folder: audio/, meta.json, transcript.md, summary.md, data/."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from callrec import __version__
from callrec.consent import ConsentRecord
from callrec.models import AsrResult, CalendarEvent, Person, SpeakerInfo, Utterance

META_SCHEMA = 1
_meta_lock = threading.Lock()


def replace_file(src: Path, dst: Path, attempts: int = 20, delay_s: float = 0.05) -> None:
    """os.replace with retries: on Windows, virus scanners, the search indexer or OneDrive
    briefly open freshly written files without FILE_SHARE_DELETE, and os.replace then fails
    with PermissionError (WinError 5/32) although nothing is wrong."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay_s * (i + 1))


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    replace_file(tmp, path)


def new_meta(
    *,
    title: str,
    event: CalendarEvent | None,
    participants: list[Person],
    self_person: Person,
    consent: ConsentRecord,
    devices: dict[str, Any],
    started_at: datetime,
) -> dict[str, Any]:
    return {
        "schema": META_SCHEMA,
        "app_version": __version__,
        "title": title,
        "recording": {"status": "recording", "started_at": started_at.isoformat(), "stopped_at": None, "duration_s": None},
        "event": event.to_dict() if event else None,
        "participants": [p.to_dict() for p in participants],
        "self": self_person.to_dict(),
        "consent": consent.to_dict(),
        "devices": devices,
        "audio": {},
        "processing": {},
    }


class Recording:
    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)

    # paths
    @property
    def meta_path(self) -> Path:
        return self.folder / "meta.json"

    @property
    def transcript_path(self) -> Path:
        return self.folder / "transcript.md"

    @property
    def summary_path(self) -> Path:
        return self.folder / "summary.md"

    @property
    def data_dir(self) -> Path:
        return self.folder / "data"

    def asr_path(self, track: str) -> Path:
        return self.data_dir / f"asr_{track}.json"

    @property
    def turns_path(self) -> Path:
        return self.data_dir / "diarization.json"

    @property
    def speakers_path(self) -> Path:
        return self.data_dir / "speakers.json"

    @property
    def utterances_path(self) -> Path:
        return self.data_dir / "utterances.json"

    @property
    def embeddings_path(self) -> Path:
        return self.data_dir / "speaker_embeddings.npz"

    # meta
    def exists(self) -> bool:
        return self.meta_path.exists()

    def load_meta(self) -> dict[str, Any]:
        return dict(json.loads(self.meta_path.read_text(encoding="utf-8")))

    def save_meta(self, meta: dict[str, Any]) -> None:
        write_text_atomic(self.meta_path, json.dumps(meta, ensure_ascii=False, indent=2))

    def update_meta(self, path: list[str], value: Any) -> dict[str, Any]:
        """Set meta[path[0]][path[1]]... = value (thread-safe, atomic write)."""
        with _meta_lock:
            meta = self.load_meta()
            node = meta
            for k in path[:-1]:
                if not isinstance(node.get(k), dict):
                    node[k] = {}
                node = node[k]
            node[path[-1]] = value
            self.save_meta(meta)
            return meta

    # convenient accessors
    def title(self) -> str:
        return str(self.load_meta().get("title") or self.folder.name)

    def started_at(self) -> datetime | None:
        raw = (self.load_meta().get("recording") or {}).get("started_at")
        return datetime.fromisoformat(raw) if raw else None

    def duration(self) -> float:
        return float((self.load_meta().get("recording") or {}).get("duration_s") or 0.0)

    @property
    def uid(self) -> str:
        """Stable unique id of this recording (created on first use, kept in meta.json).

        Folder names are unique only within one recordings folder; voice profiles remember what
        they learned from which recording by this id."""
        meta = self.load_meta()
        uid = str(meta.get("uid") or "")
        if not uid:
            import uuid

            uid = uuid.uuid4().hex
            self.update_meta(["uid"], uid)
        return uid

    def participants(self) -> list[Person]:
        return [Person.from_dict(p) for p in self.load_meta().get("participants", [])]

    def self_person(self) -> Person:
        d = self.load_meta().get("self") or {}
        return Person.from_dict(d) if d else Person(name="Ich", role="self")

    # derived data
    def save_asr(self, track: str, asr: AsrResult) -> None:
        write_text_atomic(self.asr_path(track), json.dumps(asr.to_dict(), ensure_ascii=False))

    def load_asr(self, track: str) -> AsrResult | None:
        p = self.asr_path(track)
        if not p.exists():
            return None
        return AsrResult.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def save_speakers(self, speakers: dict[str, SpeakerInfo]) -> None:
        write_text_atomic(
            self.speakers_path, json.dumps([s.to_dict() for s in speakers.values()], ensure_ascii=False, indent=2)
        )

    def load_speakers(self) -> dict[str, SpeakerInfo]:
        if not self.speakers_path.exists():
            return {}
        return {d["id"]: SpeakerInfo.from_dict(d) for d in json.loads(self.speakers_path.read_text(encoding="utf-8"))}

    def save_utterances(self, utterances: list[Utterance]) -> None:
        write_text_atomic(self.utterances_path, json.dumps([u.to_dict() for u in utterances], ensure_ascii=False))

    def load_utterances(self) -> list[Utterance]:
        if not self.utterances_path.exists():
            return []
        return [Utterance.from_dict(d) for d in json.loads(self.utterances_path.read_text(encoding="utf-8"))]

    def save_embeddings(self, embeddings: dict[str, np.ndarray], model_id: str,
                        self_embedding: np.ndarray | None = None) -> None:
        """Remote speakers' embeddings and (optionally) the embedding of the mic track (the user)."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.embeddings_path.with_name("speaker_embeddings.tmp.npz")
        arrays = {f"spk_{k}": np.asarray(v, dtype=np.float32) for k, v in embeddings.items()}
        if self_embedding is not None:
            arrays["own_mic"] = np.asarray(self_embedding, dtype=np.float32)
        np.savez(tmp, __model_id__=np.array(model_id), **arrays)  # type: ignore[arg-type]
        tmp.replace(self.embeddings_path)

    def load_embeddings(self) -> tuple[dict[str, np.ndarray], str]:
        if not self.embeddings_path.exists():
            return {}, ""
        with np.load(self.embeddings_path, allow_pickle=False) as data:
            model_id = str(data["__model_id__"]) if "__model_id__" in data.files else ""
            embs = {k[4:]: np.asarray(data[k]) for k in data.files if k.startswith("spk_")}
        return embs, model_id

    def load_self_embedding(self) -> np.ndarray | None:
        if not self.embeddings_path.exists():
            return None
        with np.load(self.embeddings_path, allow_pickle=False) as data:
            return np.asarray(data["own_mic"]) if "own_mic" in data.files else None

    @property
    def trackcheck_path(self) -> Path:
        return self.data_dir / "trackcheck.json"

    def load_trackcheck(self) -> dict[str, Any]:
        try:
            return dict(json.loads(self.trackcheck_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {}

    def save_trackcheck(self, data: dict[str, Any]) -> None:
        write_text_atomic(self.trackcheck_path, json.dumps(data, ensure_ascii=False, indent=2))

    def delete_embeddings(self) -> bool:
        if self.embeddings_path.exists():
            self.embeddings_path.unlink()
            return True
        return False


def list_recordings(root: Path) -> list[Recording]:
    if not root.exists():
        return []
    recs = [Recording(p) for p in root.iterdir() if p.is_dir() and (p / "meta.json").exists()]
    return sorted(recs, key=lambda r: r.folder.name, reverse=True)


def find_incomplete(root: Path) -> list[Recording]:
    """Recordings that were interrupted (crash / power loss) and still need finalising."""
    from callrec.audio.finalize import has_unfinished_parts

    out = []
    for rec in list_recordings(root):
        try:
            status = (rec.load_meta().get("recording") or {}).get("status")
        except (OSError, ValueError):
            continue
        if status == "failed":  # streams never started (device busy etc.): nothing to recover
            continue
        if status == "recording" or has_unfinished_parts(rec.folder):
            out.append(rec)
    return out


def recordings_to_resume(root: Path) -> list[tuple[Recording, bool]]:
    """Each recording that needs (re-)processing at startup exactly once: (rec, interrupted).

    A recording that was stopped but whose WAV parts were never converted matches both
    :func:`find_incomplete` and :func:`find_unprocessed`; it must be queued only once.
    """
    out: list[tuple[Recording, bool]] = []
    seen: set[Path] = set()
    for rec in find_incomplete(root):
        seen.add(rec.folder)
        out.append((rec, True))
    for rec in find_unprocessed(root):
        if rec.folder not in seen:
            seen.add(rec.folder)
            out.append((rec, False))
    return out


def find_unprocessed(root: Path) -> list[Recording]:
    """Stopped recordings whose post-processing never finished (app closed meanwhile)."""
    out = []
    for rec in list_recordings(root):
        try:
            meta = rec.load_meta()
        except (OSError, ValueError):
            continue
        status = (meta.get("recording") or {}).get("status")
        pstatus = (meta.get("processing") or {}).get("status")
        # "deferred" ("Später" after the recording) waits for "Jetzt verarbeiten"; an active guided
        # chain (processing.chain) is resumed by the app itself
        if status in ("stopped", "recovered") and pstatus in (None, "running"):
            out.append(rec)
    return out
