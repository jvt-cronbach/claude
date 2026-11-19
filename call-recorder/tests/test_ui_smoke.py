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


def test_settings_dialog_all_tabs_and_llm(root, tmp_path: Path):
    from callrec.config import Config, load_config, save_config
    from callrec.ui import settings as st

    cfg = Config()
    path = tmp_path / "c.toml"
    st.messagebox.showinfo = lambda *a, **k: None  # type: ignore[assignment]
    st.messagebox.askyesno = lambda *a, **k: True  # type: ignore[assignment]
    pulled = []

    class FakeProvider:
        can_pull = True

        def list_models(self):
            return ["gemma3:12b", "gemma3:4b"]

        def version(self):
            return "0.13.1"

        def pull(self, model, progress):
            progress("pulling manifest", None)
            progress("pulling abc", 0.5)
            progress("success", 1.0)
            pulled.append(model)

    dlg = st.SettingsDialog(root, cfg, ["Headset Mikrofon (Jabra)"], ["Kopfhörer (Jabra)"],
                            lambda c: save_config(c, path), provider_factory=lambda c: FakeProvider())
    tabs = [dlg.nb.tab(t, "text") for t in dlg.nb.tabs()]
    assert tabs == ["Allgemein", "Transkription", "Sprecher", "Darstellung", "Zusammenfassung", "SharePoint"]
    dlg.vars[("audio", "mic_device")].set("Headset Mikrofon (Jabra)")
    dlg.vars[("transcription", "beam_size")].set("7")
    dlg.vars[("transcription", "vad_threshold")].set("0,35")  # German decimal comma accepted
    dlg.vars[("transcript", "timestamp_format")].set("mm:ss")
    dlg.vars[("handoff", "folder")].set(str(tmp_path))
    dlg.refresh_models()
    dlg.model_var.set("gemma3:4b")
    dlg.pull_model()

    def wait_then_save():
        if not pulled or "Ollama-Version 0.13.1" not in dlg.status_var.get():
            root.after(100, wait_then_save)
            return
        assert "gemma3:4b" in dlg.model_combo.cget("values")
        find(dlg.win, ttk.Button, "Speichern").invoke()

    root.after(100, wait_then_save)
    assert dlg.run() is True
    c = load_config(path)
    assert c.audio.mic_device == "Headset Mikrofon (Jabra)" and c.audio.loopback_device == "auto"
    assert c.transcription.beam_size == 7 and c.transcription.vad_threshold == 0.35
    assert c.transcript.timestamp_format == "mm:ss" and c.summary.model == "gemma3:4b"
    assert c.handoff.folder == str(tmp_path) and c.general.preset == ""
    assert pulled == ["gemma3:4b"]

    bad = st.SettingsDialog(root, Config(), [], [], lambda c: None)
    bad.vars[("transcription", "beam_size")].set("viele")
    import pytest as _pytest

    from callrec.config import ConfigError
    with _pytest.raises(ConfigError):
        bad.collect()
    bad.win.destroy()


def test_handoff_dialog(root, tmp_path: Path):
    from tests.test_handoff import SECRET, processed_recording

    rec = processed_recording(tmp_path / "recs")
    from callrec.config import Config

    cfg = Config()
    cfg.handoff.folder = str(tmp_path)
    dialogs.messagebox.showinfo = lambda *a, **k: None  # type: ignore[assignment]
    calls = []

    def drive():
        win = toplevel(root)
        preview = find(win, tk.Text)
        text = preview.get("1.0", "end")
        assert "## Kurzfassung" in text and SECRET not in text
        assert any("verlässt bewusst" in str(w.cget("text")) for w in walk(win) if isinstance(w, ttk.Label))
        find(win, ttk.Button, "An SharePoint übergeben").invoke()

    root.after(200, drive)
    ok = dialogs.ask_handoff(root, rec, cfg, lambda sha: calls.append(sha) or {"location": "X"})
    assert ok and len(calls) == 1 and len(calls[0]) == 64  # the sha256 of the previewed document


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
