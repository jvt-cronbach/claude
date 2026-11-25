"""Who of a recording's participants can be a remote voice, and who is *expected* to speak.

The participant list of a recording is what the start dialog showed (organizer + attendees of the
calendar entry, or entered by you). For recognising voices it is too coarse:

* a person who **declined** is not in the call;
* a **meeting room** or a **distribution list** invited by address is no voice at all;
* an **optional** attendee who never answered the invitation often does not join.

Such entries used to count for "exactly one other participant" (so the single-participant rule never
applied) and for the limit of remote speakers (so one voice stayed split into two). Here each
participant gets a kind; only ``expected`` people count, every non-excluded person stays a
candidate a voice may be matched with.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from callrec.models import Person
from callrec.speakers.names import looks_like_room_or_list, persons_match

ACCEPTED = ("accepted", "tentativelyaccepted", "tentative", "organizer")


@dataclass
class Participant:
    person: Person
    expected: bool  # counts for the single-participant rule and the speaker limit
    excluded: bool  # cannot be a voice (declined, room, distribution list)
    why: str = ""  # German, for the explanation ("abgesagt", "optional, keine Zusage", ...)


def _status_for(person: Person, status: dict[str, dict[str, str]]) -> dict[str, str]:
    if not status:
        return {}
    hit = status.get(person.key())
    if hit is not None:
        return hit
    for key, st in status.items():
        if persons_match(person, Person(key if "@" not in key else "", key if "@" in key else "")):
            return st
    return {}


def classify(participants: list[Person], event: dict[str, Any] | None) -> list[Participant]:
    """``participants``: the recording's participants without you. ``event``: meta.json ``event``."""
    raw = (event or {}).get("attendee_status") if isinstance(event, dict) else None
    status = {str(k): v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}
    out: list[Participant] = []
    for p in participants:
        st = _status_for(p, status)
        kind, response = st.get("type", ""), st.get("response", "")
        if p.role == "organizer":
            out.append(Participant(p, True, False, "Organisation"))
        elif kind in ("resource", "group") or looks_like_room_or_list(p):
            out.append(Participant(p, False, True, "Raum oder Verteiler"))
        elif response == "declined":
            out.append(Participant(p, False, True, "abgesagt"))
        elif kind == "optional" and response not in ACCEPTED:
            out.append(Participant(p, False, False, "optional, keine Zusage"))
        else:
            out.append(Participant(p, True, False, "optional, zugesagt" if kind == "optional" else ""))
    return out


def candidates(classified: list[Participant]) -> list[Person]:
    return [c.person for c in classified if not c.excluded]


def expected(classified: list[Participant]) -> list[Person]:
    return [c.person for c in classified if c.expected]
