from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from callrec.audio.capture import DeviceSpec, DualTrackRecorder
from callrec.consent import (
    CONSENT_STATEMENT,
    ConsentError,
    ConsentRecord,
    cli_confirm,
    confirm_consent,
    require_consent,
)
from callrec.models import Person

PEOPLE = [Person("Anna Beispiel", "anna@firma.de")]


def test_confirm_requires_checkbox():
    with pytest.raises(ConsentError):
        confirm_consent(user_checked_box=False, confirmed_by="Ich", participants=PEOPLE, method="dialog")


def test_confirm_and_require():
    rec = confirm_consent(user_checked_box=True, confirmed_by="Ich", participants=PEOPLE, method="dialog")
    assert rec.confirmed and rec.statement == CONSENT_STATEMENT
    assert rec.confirmed_at.tzinfo is not None
    assert require_consent(rec) is rec
    d = rec.to_dict()
    assert d["participants"][0]["email"] == "anna@firma.de"
    assert ConsentRecord.from_dict(d) == rec


def test_unknown_method_rejected():
    with pytest.raises(ConsentError):
        confirm_consent(user_checked_box=True, confirmed_by="Ich", participants=[], method="config")


@pytest.mark.parametrize(
    "change",
    [
        {"confirmed": False},
        {"statement": "egal"},
        {"method": "auto"},
        {"confirmed_at": datetime.now(UTC) - timedelta(hours=1)},
        {"confirmed_at": datetime.now()},  # naive
    ],
)
def test_require_rejects_tampered(change):
    rec = confirm_consent(user_checked_box=True, confirmed_by="Ich", participants=PEOPLE, method="dialog")
    with pytest.raises(ConsentError):
        require_consent(dataclasses.replace(rec, **change))


def test_require_none():
    with pytest.raises(ConsentError):
        require_consent(None)
    with pytest.raises(ConsentError):
        require_consent({"confirmed": True})  # type: ignore[arg-type]


def test_cli_confirm():
    out: list[str] = []
    rec = cli_confirm(lambda prompt: " Ja ", out.append, "Ich", PEOPLE)
    assert rec.method == "cli"
    assert any("Anna Beispiel" in line for line in out)
    with pytest.raises(ConsentError):
        cli_confirm(lambda prompt: "j", out.append, "Ich", PEOPLE)
    asked: list[str] = []
    with pytest.raises(ConsentError, match="Konsolenfenster"):  # piped stdin: never asked, never accepted
        cli_confirm(lambda prompt: asked.append(prompt) or "ja", out.append, "Ich", PEOPLE, interactive=False)
    assert asked == []


class ExplodingBackend:
    opened = 0

    def resolve_devices(self, a, b):  # pragma: no cover
        raise AssertionError

    def open_stream(self, device, on_data, fpb):
        ExplodingBackend.opened += 1
        raise AssertionError("stream must not be opened without consent")

    def close(self):
        pass


def test_recorder_refuses_without_consent(tmp_path: Path):
    mic = DeviceSpec("1", "Mic", 48000, 1, "mic")
    loop = DeviceSpec("2", "Spk [Loopback]", 48000, 2, "loopback")
    r = DualTrackRecorder(ExplodingBackend(), tmp_path, mic, loop)
    with pytest.raises(ConsentError):
        r.start(None)
    rec = confirm_consent(user_checked_box=True, confirmed_by="Ich", participants=[], method="dialog")
    with pytest.raises(ConsentError):
        r.start(dataclasses.replace(rec, confirmed=False))
    assert ExplodingBackend.opened == 0
    assert not (tmp_path / "audio").exists()
