"""Recording folder names like ``2026-11-19_1400_Projekt-Jour-fixe``."""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_PREFIXES = re.compile(r"^\s*((AW|WG|RE|FW|FWD|Antw|Antwort)\s*:\s*)+", re.IGNORECASE)
MAX_SLUG_LEN = 60


def slugify_title(title: str, max_len: int = MAX_SLUG_LEN) -> str:
    """Turn a meeting subject into a Windows-safe, readable file name part.

    Umlauts are kept (NTFS handles them fine), whitespace becomes '-',
    forbidden characters are removed.
    """
    text = unicodedata.normalize("NFC", title or "")
    text = _PREFIXES.sub("", text)
    text = _INVALID_CHARS.sub(" ", text)
    text = text.replace("'", "").replace("`", "")
    text = re.sub(r"[\s_]+", "-", text.strip())
    text = re.sub(r"[^\w\-.,()&+äöüÄÖÜß]", "", text)
    text = re.sub(r"-{2,}", "-", text)
    text = re.sub(r"\.{2,}", ".", text)
    text = text.strip("-. ")
    if len(text) > max_len:
        cut = text[:max_len]
        # avoid cutting mid-word if a dash is reasonably close
        dash = cut.rfind("-")
        if dash >= max_len * 0.6:
            cut = cut[:dash]
        text = cut.rstrip("-. ")
    if not text:
        text = "Anruf"
    if text.split(".")[0].upper() in _WINDOWS_RESERVED:
        text = f"{text}_"
    return text


def recording_basename(start: datetime, title: str) -> str:
    return f"{start:%Y-%m-%d_%H%M}_{slugify_title(title)}"


def unique_folder(parent: Path, basename: str) -> Path:
    """Return a not-yet-existing folder path (appends _2, _3, ...)."""
    candidate = parent / basename
    n = 2
    while candidate.exists():
        candidate = parent / f"{basename}_{n}"
        n += 1
    return candidate


def create_recording_folder(parent: Path, start: datetime, title: str) -> Path:
    """Create the folder atomically (safe against concurrent creation)."""
    parent.mkdir(parents=True, exist_ok=True)
    base = recording_basename(start, title)
    n = 1
    while True:
        name = base if n == 1 else f"{base}_{n}"
        path = parent / name
        try:
            os.mkdir(path)
            return path
        except FileExistsError:
            n += 1
