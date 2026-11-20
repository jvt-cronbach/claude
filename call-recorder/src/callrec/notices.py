"""One-time notices after an update (config migrations), shown exactly once in whichever UI starts next.

A migration rewrites config.toml when it is first loaded - possibly by a CLI command or by
``callrec tray --ui …`` (which loads and saves the file before the UI starts). The note therefore
cannot live only in the ``Config`` object of that load: it is stored here until a UI has shown it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from callrec import paths

log = logging.getLogger(__name__)

TITLE = "Nach dem Update"


def _file() -> Path:
    return paths.data_dir() / "pending_notices.json"


def pending() -> list[str]:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(x) for x in data if isinstance(x, str)] if isinstance(data, list) else []


def add(text: str) -> None:
    items = pending()
    if text in items:
        return
    items.append(text)
    try:
        p = _file()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        log.warning("Hinweis nach dem Update nicht gespeichert", exc_info=True)


def take() -> list[str]:
    """The pending notices, removed from the store (the caller shows them now)."""
    items = pending()
    if items:
        try:
            _file().unlink(missing_ok=True)
        except OSError:
            log.warning("Hinweise nach dem Update nicht entfernbar", exc_info=True)
    return items


def startup_notices(cfg: Any) -> list[tuple[str, str]]:
    """(title, text) to show at the start of a UI: the config warnings of this load (without the
    migration notes, which come from the store) and every pending migration notice, once."""
    from callrec.config import MIGRATION_NOTES

    out = [("Konfiguration", w) for w in cfg.warnings if w not in MIGRATION_NOTES]
    out += [(TITLE, t) for t in take()]
    return out
