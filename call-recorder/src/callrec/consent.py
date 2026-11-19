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

# Used instead of CONSENT_STATEMENT when the summary is created by Claude via the Anthropic API.
CONSENT_STATEMENT_ANTHROPIC = (
    "Ich bestätige, dass alle Teilnehmenden dieses Gesprächs darüber informiert wurden, dass es "
    "aufgezeichnet und lokal transkribiert wird und dass das Transkript (Text mit den Namen der "
    "Teilnehmenden) zur Erstellung der Zusammenfassung an Anthropic (Claude-API) übertragen wird, "
    "und dass alle der Aufnahme und dieser Übertragung ausdrücklich zugestimmt haben."
)
ANTHROPIC_RECIPIENT = "Anthropic (api.anthropic.com, Claude-API)"


def consent_statement(summary_provider: str = "") -> str:
    """Consent text for the summary provider active when the recording starts."""
    return CONSENT_STATEMENT_ANTHROPIC if summary_provider == "anthropic" else CONSENT_STATEMENT


def active_summary_provider(summary_cfg: Any) -> str:
    """Provider that will receive the transcript ("" when summaries are disabled)."""
    return str(summary_cfg.provider) if getattr(summary_cfg, "enabled", True) else ""


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
    summary_provider: str = ""  # summary provider active when consent was given (ollama/openai/anthropic)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "confirmed": self.confirmed,
            "confirmed_at": self.confirmed_at.isoformat(),
            "statement": self.statement,
            "confirmed_by": self.confirmed_by,
            "method": self.method,
            "participants": [p.to_dict() for p in self.participants],
            "summary_provider": self.summary_provider,
        }
        if self.summary_provider == "anthropic":
            d["transcript_sent_to"] = ANTHROPIC_RECIPIENT
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> ConsentRecord:
        return ConsentRecord(
            confirmed=bool(d.get("confirmed")),
            confirmed_at=datetime.fromisoformat(d["confirmed_at"]),
            statement=str(d.get("statement", "")),
            confirmed_by=str(d.get("confirmed_by", "")),
            method=str(d.get("method", "")),
            participants=tuple(Person.from_dict(p) for p in d.get("participants", [])),
            summary_provider=str(d.get("summary_provider", "")),
        )


def confirm_consent(
    *,
    user_checked_box: bool,
    confirmed_by: str,
    participants: list[Person],
    method: str,
    now: datetime | None = None,
    summary_provider: str = "",
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
        statement=consent_statement(summary_provider),
        confirmed_by=confirmed_by,
        method=method,
        participants=tuple(participants),
        summary_provider=summary_provider,
    )


def require_consent(
    record: ConsentRecord | None, now: datetime | None = None, summary_provider: str | None = None
) -> ConsentRecord:
    """Validate a consent record right before recording starts.

    With ``summary_provider == "anthropic"`` the record must contain the statement that names the
    transfer of the transcript to Anthropic.
    """
    if record is None:
        raise ConsentError("Keine Einwilligungsbestätigung vorhanden - Aufnahme nicht gestartet.")
    if not isinstance(record, ConsentRecord):
        raise ConsentError("Ungültige Einwilligungsbestätigung.")
    if record.confirmed is not True:
        raise ConsentError("Einwilligung wurde nicht bestätigt - Aufnahme nicht gestartet.")
    if record.statement != consent_statement(record.summary_provider):
        raise ConsentError("Einwilligungstext stimmt nicht - Aufnahme nicht gestartet.")
    if record.method not in _VALID_METHODS:
        raise ConsentError("Ungültige Bestätigungsart.")
    if record.confirmed_at.tzinfo is None:
        raise ConsentError("Zeitstempel der Einwilligung ohne Zeitzone.")
    if summary_provider == "anthropic" and record.summary_provider != "anthropic":
        raise ConsentError(
            "Die Zusammenfassung wird von Claude (Anthropic) erstellt, die Bestätigung erwähnt diese "
            "Übertragung aber nicht - Aufnahme nicht gestartet."
        )
    now = now or datetime.now(UTC)
    age = now - record.confirmed_at
    if age > MAX_CONSENT_AGE or age < -timedelta(minutes=1):
        raise ConsentError("Die Einwilligungsbestätigung ist abgelaufen - bitte erneut bestätigen.")
    return record


CLI_CONSENT_ANSWER = "ja"


def cli_confirm(
    input_fn: Any, print_fn: Any, confirmed_by: str, participants: list[Person], interactive: bool = True,
    summary_provider: str = "",
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
    print_fn(consent_statement(summary_provider))
    if summary_provider == "anthropic":
        print_fn("ACHTUNG: Das Transkript wird zur Zusammenfassung an Anthropic übertragen (verlässt diesen Rechner).")
    if participants:
        print_fn("Teilnehmende: " + ", ".join(p.display for p in participants))
    answer = str(input_fn(f"Bitte '{CLI_CONSENT_ANSWER}' eingeben, um zu bestätigen: ")).strip().lower()
    return confirm_consent(
        user_checked_box=answer == CLI_CONSENT_ANSWER,
        confirmed_by=confirmed_by,
        participants=participants,
        method="cli",
        summary_provider=summary_provider,
    )


# --- later confirmation for recordings made before Anthropic was selected --------------------
def cloud_summary_covered(meta: dict[str, Any], provider: str) -> bool:
    """True if the consent of this recording covers sending its transcript to ``provider``."""
    if provider != "anthropic":
        return True
    consent = meta.get("consent") or {}
    if consent.get("summary_provider") == provider and consent.get("statement") == CONSENT_STATEMENT_ANTHROPIC:
        return True
    return any(
        c.get("provider") == provider and c.get("confirmed") is True
        for c in consent.get("additional_confirmations", []) or []
    )


LATER_STATEMENT_ANTHROPIC = (
    "Ich bestätige, dass alle Teilnehmenden dieses Gesprächs zugestimmt haben, dass sein Transkript "
    "(Text mit den Namen der Teilnehmenden) zur Erstellung der Zusammenfassung an Anthropic (Claude-API) "
    "übertragen wird."
)


def later_confirmation(confirmed_by: str, method: str, user_confirmed: bool, provider: str = "anthropic",
                       now: datetime | None = None) -> dict[str, Any]:
    """Explicit, separately recorded confirmation for an older recording (dialog or typed 'ja')."""
    if not user_confirmed:
        raise ConsentError("Ohne Bestätigung wird das Transkript nicht an Anthropic übertragen.")
    if method not in _VALID_METHODS:
        raise ConsentError(f"Unbekannte Bestätigungsart: {method!r}")
    return {
        "confirmed": True,
        "provider": provider,
        "statement": LATER_STATEMENT_ANTHROPIC,
        "confirmed_at": (now or datetime.now(UTC)).astimezone().isoformat(),
        "confirmed_by": confirmed_by,
        "method": method,
        "transcript_sent_to": ANTHROPIC_RECIPIENT,
    }


def record_later_confirmation(rec: Any, confirmation: dict[str, Any]) -> None:
    meta = rec.load_meta()
    consent = dict(meta.get("consent") or {})
    consent["additional_confirmations"] = [*(consent.get("additional_confirmations") or []), confirmation]
    rec.update_meta(["consent"], consent)
