"""Versioned outputs: transcript.vN.md / summary.vN.md plus the settings used (in meta.json).

``transcript.md`` and ``summary.md`` always contain the newest version. A new
version is created only when the *inputs* changed (other ASR run, other
diarization, other rendering settings / prompt / model). Re-rendering after a
speaker assignment keeps the version number and updates that file in place.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from callrec.recording import Recording, write_text_atomic

KINDS = ("transcript", "summary")


def fingerprint(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]


def list_versions(rec: Recording, kind: str) -> list[dict[str, Any]]:
    return list(((rec.load_meta().get("versions") or {}).get(kind)) or [])


def latest(rec: Recording, kind: str) -> dict[str, Any] | None:
    v = list_versions(rec, kind)
    return v[-1] if v else None


def version_path(rec: Recording, kind: str, n: int) -> Path:
    return rec.folder / f"{kind}.v{n}.md"


def write_version(rec: Recording, kind: str, text: str, fp: str, settings: dict[str, Any]) -> tuple[int, bool]:
    """Write ``text`` as a version of ``kind``; returns (version number, created_new)."""
    if kind not in KINDS:
        raise ValueError(kind)
    versions = list_versions(rec, kind)
    now = datetime.now(UTC).astimezone().isoformat(timespec="seconds")
    if versions and versions[-1].get("fingerprint") == fp:
        entry = versions[-1]
        entry["updated"] = now
        new = False
    else:
        entry = {"version": len(versions) + 1, "fingerprint": fp, "created": now, "settings": settings}
        versions.append(entry)
        new = True
    n = int(entry["version"])
    entry["file"] = version_path(rec, kind, n).name
    write_text_atomic(version_path(rec, kind, n), text)
    write_text_atomic(rec.folder / f"{kind}.md", text)
    rec.update_meta(["versions", kind], versions)
    return n, new


def adopt_legacy(rec: Recording, kind: str) -> None:
    """A kind.md written before versioning existed becomes version 1."""
    if list_versions(rec, kind):
        return
    p = rec.folder / f"{kind}.md"
    if p.exists():
        write_version(rec, kind, p.read_text(encoding="utf-8"), "legacy", {"note": "vor Versionierung erstellt"})
