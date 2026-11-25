"""Backup and one-click restore before a bulk change of speaker data (0.4.1 review).

The one-time profile repair and "Sprecher in allen Aufnahmen neu erkennen" change the real voice
profiles and the speaker data of many recordings at once. Before that, :func:`create` saves

* the whole voice-profile store - encrypted like the store itself (DPAPI on Windows), and
* for every recording that the change will touch, a copy of its ``speakers.json`` (in the recording's
  own ``data/archive`` folder, like the single "Sprecher neu erkennen" always did),

and :func:`restore` puts the newest backup back with one click. Only the last few backups are kept.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from callrec import paths

KEEP = 5
PROFILES_FILE = "voiceprints.bin"
INDEX_FILE = "backup.json"


def backups_dir() -> Path:
    return paths.data_dir() / "speaker-backups"


def _persistence(path: Path) -> Any:
    from callrec.speakers.profiles import secure_persistence

    return secure_persistence(path)


def create(store: Any, recordings: list[Any], reason: str) -> Path:
    """Back up the profile store and the speaker data of ``recordings``; returns the backup folder."""
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    folder = backups_dir() / stamp
    folder.mkdir(parents=True, exist_ok=False)
    _persistence(folder / PROFILES_FILE).save(store.snapshot())
    recs = []
    for rec in recordings:
        if not rec.speakers_path.exists():
            continue
        dst = rec.data_dir / "archive" / f"speakers.backup-{stamp}.json"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rec.speakers_path, dst)
        recs.append({"folder": str(rec.folder), "copy": str(dst)})
    (folder / INDEX_FILE).write_text(json.dumps({"reason": reason, "at": datetime.now().astimezone().isoformat(
        timespec="seconds"), "profiles": len(store.all_profiles()), "recordings": recs}, ensure_ascii=False,
        indent=1), encoding="utf-8")
    _prune()
    return folder


def _prune() -> None:
    all_ = sorted(p for p in backups_dir().iterdir() if p.is_dir()) if backups_dir().exists() else []
    for old in all_[:-KEEP]:
        shutil.rmtree(old, ignore_errors=True)


def latest() -> dict[str, Any] | None:
    """The newest complete backup ({"folder", "reason", "at", "profiles", "recordings"}) or None."""
    if not backups_dir().exists():
        return None
    for folder in sorted((p for p in backups_dir().iterdir() if p.is_dir()), reverse=True):
        try:
            idx = json.loads((folder / INDEX_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (folder / PROFILES_FILE).exists():
            return {**idx, "folder": str(folder)}
    return None


def describe(b: dict[str, Any] | None) -> str:
    if not b:
        return "Keine Sicherung vorhanden."
    return (f"Sicherung vom {str(b.get('at', ''))[:16].replace('T', ' ')} ({b.get('reason', '')}): "
            f"{b.get('profiles', 0)} Stimmprofil(e), Sprecherdaten von {len(b.get('recordings') or [])} Aufnahme(n).")


def restore(store: Any, b: dict[str, Any] | None = None) -> tuple[int, list[Path]]:
    """Put the newest (or the given) backup back. Returns (profiles restored, recording folders whose
    speakers.json was restored - their transcript must be rendered again)."""
    b = b or latest()
    if b is None:
        raise FileNotFoundError("Keine Sicherung vorhanden.")
    folder = Path(b["folder"])
    n = store.restore_snapshot(_persistence(folder / PROFILES_FILE).load())
    restored: list[Path] = []
    for r in b.get("recordings") or []:
        src, rec_folder = Path(r["copy"]), Path(r["folder"])
        if not src.exists() or not rec_folder.exists():
            continue  # deleted by retention since - nothing to restore
        from callrec.recording import Recording

        rec = Recording(rec_folder)
        if not rec.data_dir.exists():
            continue
        shutil.copy2(src, rec.speakers_path)
        restored.append(rec_folder)
    return n, restored
