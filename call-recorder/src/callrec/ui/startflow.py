"""The start question in both UIs (Tk and web) - toolkit-free, so both build the same record.

Two consent modes (``[consent] mode`` in config.toml, see :mod:`callrec.consent`):

* ``ask``: ONE question. The dialog shows the reminder ``reminder_statement(provider)`` and has a
  single primary button "Aufnehmen"; only that click (for this one recording) produces the
  record via :func:`callrec.consent.start_by_user`. The record says truthfully "started by the
  user, reminder shown" - never "consent confirmed".
* ``confirm``: the explicit confirmation - an unticked checkbox that the user must tick, then
  "Aufnahme starten" (:func:`callrec.consent.confirm_consent`).

Nothing here starts a recording: the record is handed to ``Controller.start`` (via the UI port),
which validates it with ``require_consent`` exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from callrec.consent import (
    ConsentRecord,
    confirm_consent,
    consent_statement,
    reminder_statement,
    start_by_user,
)
from callrec.models import Person

MODES = ("ask", "confirm")


def consent_mode(cfg: Any) -> str:
    mode = str(getattr(getattr(cfg, "consent", None), "mode", "confirm") or "confirm")
    return mode if mode in MODES else "confirm"


@dataclass(frozen=True)
class StartTexts:
    mode: str
    statement: str  # exactly the text the record will carry (shown in the dialog)
    checkbox: str  # only mode "confirm"
    start_label: str
    cancel_label: str
    cloud_note: str  # shown when the transcript leaves this computer (Anthropic)
    record_note: str  # what meta.json will say


def start_texts(mode: str, provider: str) -> StartTexts:
    cloud = provider == "anthropic"
    cloud_note = ("Die Zusammenfassung erstellt Claude (Anthropic). Das Transkript verlässt dafür diesen Rechner."
                  if cloud else "")
    if mode == "ask":
        return StartTexts(
            mode="ask", statement=reminder_statement(provider), checkbox="",
            start_label="Aufnehmen", cancel_label="Nicht aufnehmen", cloud_note=cloud_note,
            record_note="Vermerkt wird: vom Nutzer gestartet, Hinweis angezeigt (Zeit und Name) – "
                        "keine Bestätigung der Einwilligung.",
        )
    return StartTexts(
        mode="confirm", statement=consent_statement(provider),
        checkbox=("Ja, alle Teilnehmenden haben der Aufnahme und der Übertragung an Anthropic zugestimmt." if cloud
                  else "Ja, alle Teilnehmenden haben der Aufnahme zugestimmt."),
        start_label="Aufnahme starten", cancel_label="Abbrechen", cloud_note=cloud_note,
        record_note="Die Bestätigung wird mit Zeit und Name in der Aufnahme vermerkt.",
    )


def build_record(mode: str, *, user_action: bool, me: Person, participants: list[Person], provider: str,
                 method: str = "dialog") -> ConsentRecord:
    """``user_action``: in mode "ask" the click on "Aufnehmen", in mode "confirm" the ticked box -
    straight from the control the user operated. False raises :class:`callrec.consent.ConsentError`."""
    who = me.display or "Benutzer"
    if mode == "ask":
        return start_by_user(user_clicked_record=bool(user_action), started_by=who, participants=participants,
                             method=method, summary_provider=provider)
    return confirm_consent(user_checked_box=bool(user_action), confirmed_by=who, participants=participants,
                           method=method, summary_provider=provider)


def consent_summary(meta: dict[str, Any]) -> str:
    """What a recording's meta.json actually records about the start (for detail views)."""
    c = meta.get("consent") or {}
    if not c:
        return "Keine Angabe zur Einwilligung gespeichert."
    if c.get("mode") == "ask":
        return "Vom Nutzer gestartet, Hinweis auf die Zustimmung aller Teilnehmenden angezeigt."
    if c.get("confirmed") is True:
        return "Einwilligung aller Teilnehmenden vor dem Start bestätigt."
    return "Keine bestätigte Einwilligung gespeichert."


def people_shown(participants: list[Person], limit: int = 12) -> list[str]:
    """The participants as the call-detected prompt lists them - all of them are recorded, so a cut
    list says how many more there are."""
    names = [p.display for p in participants]
    if len(names) <= limit:
        return names
    return names[:limit] + [f"und {len(names) - limit} weitere"]


def decision_from_prompt(prompt: Any, me: Person, provider: str) -> Any:
    """Mode "ask": the click on "Aufnehmen" in the call-detected prompt, for exactly the detected
    event and its participants (what the prompt showed). Returns a ``StartDecision``."""
    from callrec.controller import StartDecision

    event = getattr(prompt, "event", None)
    participants = list(event.participants()) if event is not None else []
    title = event.subject if event is not None else "Anruf"
    consent = build_record("ask", user_action=True, me=me, participants=participants, provider=provider)
    return StartDecision(title, participants, event, consent)


class PreparedStart:
    """Hands the decision made in the call-detected prompt to the ``ask_start`` that
    ``Controller.on_reminder`` triggers right after it (one-shot, valid for a few seconds only).
    It never creates a decision itself: only :func:`decision_from_prompt` after a click does."""

    VALID_S = 30.0

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._decision: Any = None
        self._at = 0.0

    def put(self, decision: Any) -> None:
        import time

        with self._lock:
            self._decision, self._at = decision, time.monotonic()

    def take(self) -> Any:
        import time

        with self._lock:
            d, at = self._decision, self._at
            self._decision = None
        if d is None or time.monotonic() - at > self.VALID_S:
            return None
        return d
