"""The user's own name: for the mic track in transcript and summary, and to keep the user out of
remote-speaker matching."""

from __future__ import annotations

import json
from typing import Any

from callrec import paths
from callrec.models import Person
from callrec.speakers.names import first_name

FALLBACK = "Ich"


def cached_me() -> dict[str, Any]:
    """Graph /me as cached at the last sign-in ({} when never signed in)."""
    try:
        d = json.loads((paths.data_dir() / "me.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def display_name(cfg: Any, rec_self: Person | None = None, me: dict[str, Any] | None = None) -> str:
    """``transcript.self_display_name`` if set; else the first name from the Microsoft account
    (/me, when signed in); else the first name of ``general.user_name`` (or of the name stored
    with the recording); else "Ich"."""
    explicit = str(getattr(cfg.transcript, "self_display_name", "") or "").strip()
    if explicit:
        return explicit
    me = cached_me() if me is None else me
    given = str(me.get("given_name") or "").strip()
    if given:
        return given
    if me.get("name"):
        n = first_name(str(me["name"]))
        if n:
            return n
    for candidate in (cfg.general.user_name, rec_self.name if rec_self is not None else ""):
        n = first_name(str(candidate or "").strip())
        if n and n.casefold() != FALLBACK.casefold():
            return n
    return FALLBACK


def self_people(cfg: Any, rec_self: Person | None = None, me: dict[str, Any] | None = None) -> list[Person]:
    """Every identity of the user we know - none of them may be matched to a remote speaker."""
    me = cached_me() if me is None else me
    out: list[Person] = []
    if rec_self is not None and (rec_self.name or rec_self.email) and rec_self.name != FALLBACK:
        out.append(rec_self)
    if cfg.general.user_name.strip() or cfg.general.user_email.strip():
        out.append(Person(cfg.general.user_name.strip(), cfg.general.user_email.strip(), "self"))
    if me.get("name") or me.get("email"):
        out.append(Person(str(me.get("name") or ""), str(me.get("email") or ""), "self"))
    return out
