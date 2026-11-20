"""Installed Whisper models in CallRecorder's model folder: list with sizes, remove.

Layout (huggingface_hub cache, ``cache_dir`` = ``%LOCALAPPDATA%\\CallRecorder\\models\\whisper``)::

    models--Systran--faster-whisper-large-v3/
        blobs/<hash>                 file contents (+ ``*.incomplete`` leftovers of aborted downloads)
        snapshots/<commit>/model.bin  symlink to the blob - or the file itself where Windows does
                                      not allow symlinks (huggingface_hub then stores it here)
        refs/main

Sizes count every regular file once (symlinks are not followed), so a folder with a complete
blob *and* a leftover ``.incomplete`` copy shows up with both - that is exactly the "5.75 GB for a
3 GB model" case after an interrupted download.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from callrec.asr.download import REQUIRED_FILES, repo_folder, tree_size

PREFIX = "models--"


@dataclass
class InstalledModel:
    name: str  # faster-whisper name ("large-v3-turbo") or the repository id
    repo: str
    folder: Path
    size: int
    partial_size: int  # bytes in *.incomplete files (aborted downloads)
    complete: bool

    def describe(self) -> str:
        from callrec.asr.download import fmt_bytes

        state = "vollständig" if self.complete else "UNVOLLSTÄNDIG"
        extra = f", davon {fmt_bytes(self.partial_size)} Reste abgebrochener Downloads" if self.partial_size else ""
        return f"{self.name:<18} {fmt_bytes(self.size):>9}  {state}{extra}  ({self.repo})"


def _names_by_repo() -> dict[str, str]:
    try:
        from faster_whisper.utils import _MODELS
    except ImportError:  # pragma: no cover
        return {}
    out: dict[str, str] = {}
    for name, repo in _MODELS.items():
        # prefer the explicit name ("large-v3", "large-v3-turbo") over aliases ("large", "turbo")
        if repo not in out or len(name) > len(out[repo]):
            out[repo] = name
    return out


def _complete(folder: Path) -> bool:
    snaps = folder / "snapshots"
    if not snaps.is_dir():
        return False
    for snap in snaps.iterdir():
        if all((snap / f).exists() for f in REQUIRED_FILES) and any(snap.glob("vocabulary.*")):
            return True
    return False


def installed_models(root: Path) -> list[InstalledModel]:
    if not root.is_dir():
        return []
    names = _names_by_repo()
    out = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or not folder.name.startswith(PREFIX):
            continue
        repo = folder.name[len(PREFIX):].replace("--", "/", 1)
        partial = sum(p.stat().st_size for p in (folder / "blobs").glob("*.incomplete")) if (folder / "blobs").is_dir() else 0
        out.append(InstalledModel(names.get(repo, repo), repo, folder, tree_size(folder), partial, _complete(folder)))
    return out


def find_model(root: Path, name: str) -> InstalledModel | None:
    name = name.strip()
    repo = name
    if "/" not in name:
        try:
            from faster_whisper.utils import _MODELS

            repo = _MODELS.get(name, name)
        except ImportError:  # pragma: no cover
            pass
    for m in installed_models(root):
        if name in (m.name, m.repo) or m.repo == repo or m.folder == repo_folder(root, repo):
            return m
    return None


def remove_model(model: InstalledModel, root: Path) -> int:
    """Delete the model folder (only inside ``root``); returns the bytes freed."""
    folder = model.folder.resolve()
    if folder.parent != root.resolve() or not folder.name.startswith(PREFIX):
        raise ValueError(f"{folder} liegt nicht im Modellordner {root}")
    size = model.size
    shutil.rmtree(folder)
    return size
