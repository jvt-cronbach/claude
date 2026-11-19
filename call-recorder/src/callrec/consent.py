"""Consent gate (§ 201 StGB: recording the non-public spoken word requires consent).

A :class:`ConsentRecord` is produced only by an explicit user confirmation
(dialog or interactive CLI prompt). The recorder refuses to start without a
valid, fresh record. There is no configuration switch to bypass this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from callrec.models import Person

CONSENT_STATEMENT = (
    "Ich bestätige, dass alle Teilnehmenden dieses Gesprächs darüber informiert wurden, "
    "dass es aufgezeichnet, lokal transkribiert und zusammengefasst wird, und dass alle "
    "der Aufnahme ausdrücklich zugestimmt haben."
)

# A confirmation older than this is not accepted for starting a recording.
MAX_CONSENT_AGE = timedelta(minutes=10)

_VALID_METHODS = {"dialog", "cli"}


class ConsentError(PermissionError):
    """Raised when a recording would start without a valid consent confirmation."""


@dataclass(frozen=True)
class ConsentRecord:
    confirmed: bool
    confirmed_at: datetime
    statement: str
    confirmed_by: str
    method: str
    participants: tuple[Person, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "confirmed": self.confirmed,
            "confirmed_at": self.confirmed_at.isoformat(),
            "statement": self.statement,
            "confirmed_by": self.confirmed_by,
            "method": self.method,
            "participants": [p.to_dict() for p in self.participants],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> ConsentRecord:
        return ConsentRecord(
            confirmed=bool(d.get("confirmed")),
            confirmed_at=datetime.fromisoformat(d["confirmed_at"]),
            statement=str(d.get("statement", "")),
            confirmed_by=str(d.get("confirmed_by", "")),
            method=str(d.get("method", "")),
            participants=tuple(Person.from_dict(p) for p in d.get("participants", [])),
        )


def confirm_consent(
    *,
    user_checked_box: bool,
    confirmed_by: str,
    participants: list[Person],
    method: str,
    now: datetime | None = None,
) -> ConsentRecord:
    """Create a consent record from an explicit user action.

    ``user_checked_box`` must come straight from the UI control the user
    ticked (or the typed CLI answer); passing False raises.
    """
    if not user_checked_box:
        raise ConsentError("Ohne Bestätigung der Einwilligung aller Teilnehmenden wird nicht aufgenommen.")
    if method not in _VALID_METHODS:
        raise ConsentError(f"Unbekannte Bestätigungsart: {method!r}")
    return ConsentRecord(
        confirmed=True,
        confirmed_at=(now or datetime.now(UTC)).astimezone(),
        statement=CONSENT_STATEMENT,
        confirmed_by=confirmed_by,
        method=method,
        participants=tuple(participants),
    )


def require_consent(record: ConsentRecord | None, now: datetime | None = None) -> ConsentRecord:
    """Validate a consent record right before recording starts."""
    if record is None:
        raise ConsentError("Keine Einwilligungsbestätigung vorhanden - Aufnahme nicht gestartet.")
    if not isinstance(record, ConsentRecord):
        raise ConsentError("Ungültige Einwilligungsbestätigung.")
    if record.confirmed is not True:
        raise ConsentError("Einwilligung wurde nicht bestätigt - Aufnahme nicht gestartet.")
    if record.statement != CONSENT_STATEMENT:
        raise ConsentError("Einwilligungstext stimmt nicht - Aufnahme nicht gestartet.")
    if record.method not in _VALID_METHODS:
        raise ConsentError("Ungültige Bestätigungsart.")
    if record.confirmed_at.tzinfo is None:
        raise ConsentError("Zeitstempel der Einwilligung ohne Zeitzone.")
    now = now or datetime.now(UTC)
    age = now - record.confirmed_at
    if age > MAX_CONSENT_AGE or age < -timedelta(minutes=1):
        raise ConsentError("Die Einwilligungsbestätigung ist abgelaufen - bitte erneut bestätigen.")
    return record


CLI_CONSENT_ANSWER = "ja"


def cli_confirm(
    input_fn: Any, print_fn: Any, confirmed_by: str, participants: list[Person], interactive: bool = True
) -> ConsentRecord:
    """Interactive console confirmation: the user must type 'ja'.

    ``interactive`` must be False when stdin is not a terminal: an answer piped in by a
    script (``echo ja | callrec record``) is not a fresh confirmation by a person.
    """
    if not interactive:
        raise ConsentError(
            "Die Einwilligung muss in einem Konsolenfenster eingetippt werden (keine umgeleitete Eingabe)."
        )
    print_fn("")
    print_fn("EINWILLIGUNG ERFORDERLICH (§ 201 StGB)")
    print_fn(CONSENT_STATEMENT)
    if participants:
        print_fn("Teilnehmende: " + ", ".join(p.display for p in participants))
    answer = str(input_fn(f"Bitte '{CLI_CONSENT_ANSWER}' eingeben, um zu bestätigen: ")).strip().lower()
    return confirm_consent(
        user_checked_box=answer == CLI_CONSENT_ANSWER,
        confirmed_by=confirmed_by,
        participants=participants,
        method="cli",
    )
