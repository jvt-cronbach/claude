"""Parsing/formatting of participant lists entered by hand."""

from __future__ import annotations

import re

from callrec.models import Person

_ANGLE = re.compile(r"^\s*(?P<name>.*?)\s*<(?P<email>[^<>\s]+@[^<>\s]+)>\s*$")
_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def parse_participant(line: str) -> Person | None:
    line = line.strip().strip(",;")
    if not line:
        return None
    m = _ANGLE.match(line)
    if m:
        email = m.group("email")
        name = m.group("name").strip().strip('"') or email
        return Person(name=name, email=email, role="manual")
    if _EMAIL.match(line):
        return Person(name=line, email=line, role="manual")
    return Person(name=line, role="manual")


def parse_participants_text(text: str) -> list[Person]:
    """One person per line (also ';' separated): ``Name``, ``mail@x`` or ``Name <mail@x>``."""
    out: list[Person] = []
    seen: set[str] = set()
    for raw in re.split(r"[\n;]", text):
        p = parse_participant(raw)
        if p is None or p.key() in seen:
            continue
        seen.add(p.key())
        out.append(p)
    return out


def format_participants(people: list[Person]) -> str:
    lines = []
    for p in people:
        if p.email and p.name and p.name != p.email:
            lines.append(f"{p.name} <{p.email}>")
        else:
            lines.append(p.name or p.email)
    return "\n".join(lines)
