"""Mode "ask": one question instead of an explicit consent confirmation (owner's decision)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from callrec import consent as c
from callrec.config import PRESET_SECTIONS, Config
from callrec.models import Person

NOW = datetime(2026, 11, 20, 14, 0, tzinfo=UTC)
PEOPLE = [Person(name="Anna Beispiel", email="anna@example.org")]


def _ask(provider: str = "") -> c.ConsentRecord:
    return c.start_by_user(
        user_clicked_record=True, started_by="Tester", participants=PEOPLE, method="dialog", now=NOW,
        summary_provider=provider,
    )


def test_default_mode_is_ask_and_not_a_preset_section() -> None:
    assert Config().consent.mode == "ask"
    assert "consent" not in PRESET_SECTIONS


def test_start_needs_the_users_click() -> None:
    with pytest.raises(c.ConsentError):
        c.start_by_user(user_clicked_record=False, started_by="Tester", participants=PEOPLE, method="dialog")


def test_ask_record_is_accepted_but_claims_no_confirmation() -> None:
    rec = _ask()
    assert c.require_consent(rec, NOW) is rec
    d = rec.to_dict()
    assert d["mode"] == "ask" and d["confirmed"] is False
    assert d["started_by_user"] is True and d["reminder_shown"] is True
    assert d["statement"] == c.REMINDER_STATEMENT
    assert c.ConsentRecord.from_dict(d) == rec


def test_ask_record_expires_and_cannot_be_forged() -> None:
    rec = _ask()
    with pytest.raises(c.ConsentError):
        c.require_consent(rec, NOW + c.MAX_CONSENT_AGE + timedelta(seconds=1))
    forged = c.ConsentRecord(
        confirmed=False, confirmed_at=NOW, statement="irgendwas", confirmed_by="x", method="dialog", mode="ask"
    )
    with pytest.raises(c.ConsentError):
        c.require_consent(forged, NOW)
    # an unconfirmed record in mode "confirm" is still refused
    unconfirmed = c.ConsentRecord(
        confirmed=False, confirmed_at=NOW, statement=c.CONSENT_STATEMENT, confirmed_by="x", method="dialog"
    )
    with pytest.raises(c.ConsentError):
        c.require_consent(unconfirmed, NOW)
    with pytest.raises(c.ConsentError):
        c.require_consent(None, NOW)


def test_anthropic_needs_the_reminder_that_names_it() -> None:
    with pytest.raises(c.ConsentError):
        c.require_consent(_ask(""), NOW, "anthropic")
    rec = _ask("anthropic")
    assert c.require_consent(rec, NOW, "anthropic") is rec
    assert c.cloud_summary_covered({"consent": rec.to_dict()}, "anthropic")
    assert not c.cloud_summary_covered({"consent": _ask("").to_dict()}, "anthropic")


def test_cli_start_needs_a_terminal_and_a_yes() -> None:
    out: list[str] = []
    with pytest.raises(c.ConsentError):
        c.cli_start(lambda _p: "j", out.append, "Tester", PEOPLE, interactive=False)
    with pytest.raises(c.ConsentError):
        c.cli_start(lambda _p: "", out.append, "Tester", PEOPLE)
    rec = c.cli_start(lambda _p: "j", out.append, "Tester", PEOPLE)
    assert rec.mode == "ask" and rec.method == "cli"
    assert any("zugestimmt" in line for line in out)


def test_confirm_mode_still_works() -> None:
    rec = c.confirm_consent(
        user_checked_box=True, confirmed_by="Tester", participants=PEOPLE, method="dialog", now=NOW
    )
    assert rec.mode == "confirm" and c.require_consent(rec, NOW) is rec
