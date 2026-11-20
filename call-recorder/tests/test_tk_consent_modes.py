"""Classic Tk UI: the start question in consent mode "ask" (one question) and the call-detected prompt
that IS that question (needs tkinter and a display, e.g. Xvfb)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")
if sys.platform != "win32" and not os.environ.get("DISPLAY"):
    pytest.skip("kein Display (Xvfb) verfügbar", allow_module_level=True)

from tkinter import ttk  # noqa: E402

from callrec.calendar.service import Suggestion  # noqa: E402
from callrec.consent import reminder_statement, require_consent  # noqa: E402
from callrec.detect import Prompt  # noqa: E402
from callrec.models import Person  # noqa: E402
from callrec.ui import dialogs  # noqa: E402
from tests.conftest import make_event  # noqa: E402
from tests.test_ui_smoke import calendar, find, root, toplevel, walk  # noqa: E402,F401


@pytest.mark.parametrize("provider", ["", "anthropic"])
def test_ask_mode_start_is_one_question(root, tmp_path: Path, provider: str):  # noqa: F811
    ev = make_event("Jour fixe", is_online=True)
    seen = {}

    def drive():
        win = toplevel(root)
        seen["checkbox"] = any(isinstance(w, ttk.Checkbutton) for w in walk(win))
        seen["texts"] = [str(w.cget("text")) for w in walk(win) if isinstance(w, ttk.Label)]
        b = find(win, ttk.Button, "Aufnehmen")
        seen["enabled"] = not b.instate(["disabled"])
        find(win, ttk.Button, "Nicht aufnehmen")
        b.invoke()

    root.after(200, drive)
    dec = dialogs.ask_start(root, Suggestion(ev, ev.participants(), source="graph"), Person("Ich"), calendar(tmp_path),
                            summary_provider=provider, mode="ask")
    assert seen["checkbox"] is False and seen["enabled"] is True
    assert reminder_statement(provider) in seen["texts"]
    assert dec is not None and dec.consent.mode == "ask" and dec.consent.confirmed is False
    assert dec.consent.statement == reminder_statement(provider)
    require_consent(dec.consent, summary_provider=provider or None)


def test_ask_mode_not_recording(root, tmp_path: Path):  # noqa: F811
    root.after(200, lambda: find(toplevel(root), ttk.Button, "Nicht aufnehmen").invoke())
    assert dialogs.ask_start(root, Suggestion(None, [], ""), Person("Ich"), calendar(tmp_path), mode="ask") is None


def test_call_prompt_choice(root):  # noqa: F811
    p = Prompt("calendar", make_event("Kundentermin"))
    root.after(200, lambda: find(toplevel(root), ttk.Button, "Aufnehmen").invoke())
    assert dialogs.ask_record_prompt_choice(root, p) == "record"
    root.after(200, lambda: find(toplevel(root), ttk.Button, "Details").invoke())
    assert dialogs.ask_record_prompt_choice(root, p) == "details"
    root.after(200, lambda: find(toplevel(root), ttk.Button, "Nicht aufnehmen").invoke())
    assert dialogs.ask_record_prompt_choice(root, p) == ""
    assert dialogs.ask_record_prompt_choice(root, p, timeout_s=0) == ""  # expiry never starts


def test_tkui_prompt_record_starts_without_second_question(tmp_path: Path, monkeypatch):
    from callrec.ui.tkui import TkUi

    ui = TkUi(calendar(tmp_path), show_overlay=False)
    try:
        ui.consent_mode = lambda: "ask"
        ui.summary_provider = lambda: "anthropic"
        ui.me_provider = lambda: Person("Ich Selbst")
        ev = make_event("Kundentermin")
        monkeypatch.setattr(dialogs, "ask_record_prompt_choice", lambda root, prompt, provider: "record")
        monkeypatch.setattr(dialogs, "ask_start", lambda *a, **k: pytest.fail("keine zweite Frage"))
        assert ui.ask_record_prompt(Prompt("calendar", ev)) is True
        dec = ui.ask_start(Suggestion(None, [], ""), Person("Ich Selbst"))
        assert dec.title == "Kundentermin" and dec.consent.mode == "ask"
        assert dec.consent.statement == reminder_statement("anthropic")
        monkeypatch.setattr(dialogs, "ask_record_prompt_choice", lambda root, prompt, provider: "")
        assert ui.ask_record_prompt(Prompt("teams")) is False and ui.prepared.take() is None
    finally:
        ui.root.destroy()
