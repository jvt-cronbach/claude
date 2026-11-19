"""Drives the Tk dialogs programmatically (needs tkinter and a display, e.g. Xvfb)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

tk = pytest.importorskip("tkinter")
if sys.platform != "win32" and not os.environ.get("DISPLAY"):
    pytest.skip("kein Display (Xvfb) verfügbar", allow_module_level=True)

from tkinter import ttk  # noqa: E402

from callrec.calendar.service import CalendarService, Suggestion  # noqa: E402
from callrec.consent import CONSENT_STATEMENT, require_consent  # noqa: E402
from callrec.models import Person  # noqa: E402
from callrec.ui import dialogs  # noqa: E402
from tests.conftest import make_event  # noqa: E402


@pytest.fixture
def root():
    r = tk.Tk()
    r.withdraw()
    yield r
    r.destroy()


def walk(w):
    yield w
    for c in w.winfo_children():
        yield from walk(c)


def find(root, cls, text=None):
    for w in walk(root):
        if isinstance(w, cls) and (text is None or text in str(w.cget("text"))):
            return w
    raise LookupError(f"{cls.__name__} {text!r} nicht gefunden")


def toplevel(root):
    tops = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
    assert tops, "kein Dialog offen"
    return tops[-1]


def calendar(tmp_path: Path) -> CalendarService:
    from datetime import timedelta

    return CalendarService(None, tmp_path / "c.json", timedelta(minutes=10), timedelta(minutes=10))


def test_start_dialog_requires_checkbox(root, tmp_path: Path):
    ev = make_event("Projekt Jour fixe", is_online=True)
    sugg = Suggestion(ev, ev.participants(), source="graph")
    state = {}

    def drive():
        win = toplevel(root)
        start = find(win, ttk.Button, "Aufnahme starten")
        state["disabled_initially"] = start.instate(["disabled"])
        assert CONSENT_STATEMENT in [str(w.cget("text")) for w in walk(win) if isinstance(w, ttk.Label)]
        find(win, ttk.Checkbutton, "zugestimmt").invoke()
        state["enabled_after"] = not start.instate(["disabled"])
        start.invoke()

    root.after(200, drive)
    decision = dialogs.ask_start(root, sugg, Person("Ich Selbst"), calendar(tmp_path))
    assert state == {"disabled_initially": True, "enabled_after": True}
    assert decision is not None and decision.title == "Projekt Jour fixe"
    assert [p.name for p in decision.participants] == ["Olga Organisatorin", "Anna Beispiel", "Bernd Muster"]
    require_consent(decision.consent)
    assert decision.consent.method == "dialog"


def test_start_dialog_cancel(root, tmp_path: Path):
    root.after(200, lambda: find(toplevel(root), ttk.Button, "Abbrechen").invoke())
    assert dialogs.ask_start(root, Suggestion(None, [], "Kein Termin"), Person("Ich"), calendar(tmp_path)) is None


def test_reminder_dialog(root):
    from callrec.detect import Prompt

    root.after(200, lambda: find(toplevel(root), ttk.Button, "Aufnahme vorbereiten").invoke())
    assert dialogs.ask_record_prompt(root, Prompt("teams")) is True
    root.after(200, lambda: find(toplevel(root), ttk.Button, "Nein").invoke())
    assert dialogs.ask_record_prompt(root, Prompt("teams")) is False


def test_review_and_profiles_dialogs(root, tmp_path: Path):
    from callrec.models import AsrResult, AsrSegment, SpeakerInfo, Word
    from callrec.speakers.profiles import PlainFilePersistence, ProfileStore
    from tests.conftest import make_recording

    rec = make_recording(tmp_path / "r", with_parts=False)
    seg = AsrResult("de", 3, [AsrSegment(0, 2, "Hallo", [Word(0, 2, " Hallo zusammen")])])
    rec.save_asr("mic", seg)
    rec.save_asr("remote", seg)
    rec.save_speakers({"ME": SpeakerInfo("ME", "Ich", name="Ich Selbst", source="self"), "S1": SpeakerInfo("S1", "Sprecher 1")})
    rec.turns_path.write_text('[{"start": 0, "end": 2, "speaker": "S1"}]', encoding="utf-8")
    rec.save_embeddings({"S1": np.ones(8, dtype=np.float32)}, "m")
    from callrec.pipeline import Pipeline

    store = ProfileStore(PlainFilePersistence(tmp_path / "vp.json"))
    from tests.test_pipeline import make_pipeline

    pipeline: Pipeline = make_pipeline(store)[0]
    resum = []

    def drive():
        win = toplevel(root)
        combo = find(win, ttk.Combobox)
        assert "Anna Beispiel" in combo.cget("values")
        combo.set("Anna Beispiel")
        find(win, ttk.Button, "Übernehmen").invoke()
        # info box after saving
        root.after(300, lambda: [w.destroy() for w in root.winfo_children() if isinstance(w, tk.Toplevel)])

    root.after(200, drive)
    # messagebox.showinfo blocks; replace it for the test
    dialogs.messagebox.showinfo = lambda *a, **k: None  # type: ignore[assignment]
    assert dialogs.review_speakers(root, rec, store, lambda: pipeline.run(rec, {"render"}), lambda: resum.append(1)) is True
    assert rec.load_speakers()["S1"].name == "Anna Beispiel"
    assert "Anna Beispiel:** Hallo zusammen" in rec.transcript_path.read_text(encoding="utf-8")
    assert resum == [1] and store.find("Anna Beispiel", "", "m") is not None

    def drive_profiles():
        win = toplevel(root)
        tree = find(win, ttk.Treeview)
        assert len(tree.get_children()) == 1
        tree.selection_set(tree.get_children()[0])
        dialogs.messagebox.askyesno = lambda *a, **k: True  # type: ignore[assignment]
        find(win, ttk.Button, "Ausgewählte löschen").invoke()
        assert len(tree.get_children()) == 0
        find(win, ttk.Button, "Schließen").invoke()

    root.after(200, drive_profiles)
    dialogs.manage_profiles(root, store)
    assert store.all_profiles() == []


def test_settings_dialog(root, tmp_path: Path):
    from callrec.config import Config, load_config, save_config

    cfg = Config()
    path = tmp_path / "c.toml"
    dialogs.messagebox.showinfo = lambda *a, **k: None  # type: ignore[assignment]

    def drive():
        win = toplevel(root)
        combos = [w for w in walk(win) if isinstance(w, ttk.Combobox)]
        combos[0].set("Headset Mikrofon (Jabra)")
        find(win, ttk.Button, "Speichern").invoke()

    root.after(200, drive)
    assert dialogs.edit_settings(root, cfg, ["Headset Mikrofon (Jabra)"], ["Kopfhörer (Jabra)"], lambda c: save_config(c, path))
    assert load_config(path).audio.mic_device == "Headset Mikrofon (Jabra)"
    assert load_config(path).audio.loopback_device == "auto"


def test_tkui_keeps_pumping_while_modal_dialog_open(tmp_path: Path):
    from callrec.ui.tkui import TkUi

    ui = TkUi(calendar(tmp_path), show_overlay=True)
    seen: list[str] = []

    def modal():
        win = tk.Toplevel(ui.root)
        ui.post(lambda: seen.append("während Dialog"))
        ui.root.after(600, win.destroy)
        ui.root.wait_window(win)
        seen.append("Dialog zu")
        ui.root.after(100, ui.root.quit)

    ui.post(modal)
    ui.set_recording_state(True, "Test")
    ui.mainloop()
    assert seen == ["während Dialog", "Dialog zu"]
    assert ui.overlay is not None and ui.overlay.win is not None
    assert "AUFNAHME" in ui.overlay._label.cget("text")  # type: ignore[union-attr]
    ui.overlay.hide()
    ui.root.destroy()
