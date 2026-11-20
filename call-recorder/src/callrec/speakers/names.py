"""Robust identity of people across calendar entries, typed names and voice profiles.

Calendar display names come in many shapes for the same person: "Müller, Anna",
"Anna Müller", "Dr. Anna Mueller (extern)", "anna.mueller@firma.de". A voice profile must
be found for every one of these variants, and must *not* be found for a different person.

Rules (in this order):
* both sides have an e-mail address -> the addresses decide (case-insensitive);
* otherwise the normalised names decide: titles and salutations dropped, a parenthesised
  suffix dropped, "Nachname, Vorname" turned around, umlauts transliterated (ä -> ae ...),
  accents removed, hyphens treated as spaces - compared as a set of name tokens, so the
  word order does not matter;
* a side that only has an e-mail address is compared by the local part of the address
  ("anna.mueller@..." has the tokens {"anna", "mueller"}).
"""

from __future__ import annotations

import re
import unicodedata

from callrec.models import Person

_TITLES = {
    "dr", "prof", "professor", "dipl", "ing", "dipl-ing", "mag", "mba", "msc", "bsc", "phd", "med", "rer", "nat",
    "herr", "frau", "hr", "fr", "mr", "mrs", "ms", "miss", "sir", "dr-ing", "habil", "jun", "sen", "jr", "sr",
}
_TRANSLIT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "æ": "ae", "ø": "oe", "œ": "oe", "å": "aa"})
_PARENS = re.compile(r"[(\[{].*?[)\]}]")
_SPLIT = re.compile(r"[^\w]+", re.UNICODE)


def _fold(text: str) -> str:
    text = text.casefold().translate(_TRANSLIT)
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def _reorder(name: str) -> str:
    """ "Nachname, Vorname" -> "Vorname Nachname" (only for exactly one comma)."""
    if name.count(",") == 1:
        last, first = (x.strip() for x in name.split(","))
        if first and last:
            return f"{first} {last}"
    return name


def name_tokens(name: str) -> frozenset[str]:
    """Normalised set of name tokens ("" -> empty set)."""
    if not name:
        return frozenset()
    if "@" in name and " " not in name.strip():
        return email_tokens(name)
    name = _PARENS.sub(" ", name)
    name = _reorder(name)
    tokens = [t for t in _SPLIT.split(_fold(name).replace("_", " ")) if t]
    kept = [t for t in tokens if t not in _TITLES]
    return frozenset(kept or tokens)


def email_tokens(email: str) -> frozenset[str]:
    local = email.split("@", 1)[0]
    local = re.sub(r"\+.*$", "", local)
    return frozenset(t for t in re.split(r"[._\-]+", _fold(local)) if t and not t.isdigit())


def same_person(name_a: str, email_a: str, name_b: str, email_b: str) -> bool:
    ea, eb = email_a.strip().casefold(), email_b.strip().casefold()
    if ea and eb:
        return ea == eb
    ta, tb = name_tokens(name_a), name_tokens(name_b)
    if ta and tb and ta == tb:
        return True
    # one side known only by its address: compare with the other side's name
    if ea and not ta and tb:
        return email_tokens(ea) == tb
    if eb and not tb and ta:
        return email_tokens(eb) == ta
    return False


def persons_match(a: Person, b: Person) -> bool:
    return same_person(a.name, a.email, b.name, b.email)


def first_name(display_name: str) -> str:
    """First name of a display name ("van Treeck, Joost" -> "Joost", "Dr. Anna Müller" -> "Anna")."""
    name = _PARENS.sub(" ", display_name or "").strip()
    if not name or "@" in name and " " not in name:
        local = name.split("@", 1)[0]
        parts = [p for p in re.split(r"[._\-]+", local) if p]
        return parts[0].capitalize() if parts else ""
    name = _reorder(name)
    for tok in name.split():
        if _fold(tok).strip(".") not in _TITLES:
            return tok.strip(",")
    return name.split()[0]
