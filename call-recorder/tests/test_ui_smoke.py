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
    rec.turns_path.write_text('[{"start": 0, "end": 25, "speaker": "S1"}]', encoding="utf-8")
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


def test_review_dialog_shows_own_track_and_profile_origins(root, tmp_path: Path):
    from callrec.models import AsrResult, AsrSegment, SpeakerInfo, Word
    from callrec.speakers.profiles import PlainFilePersistence, ProfileStore
    from tests.conftest import make_recording

    rec = make_recording(tmp_path / "r", with_parts=False)
    seg = AsrResult("de", 30, [AsrSegment(0, 4, "Guten Morgen", [Word(0, 4, " Guten Morgen")])])
    rec.save_asr("mic", seg)
    rec.save_asr("remote", seg)
    rec.save_speakers({"ME": SpeakerInfo("ME", "Ich", name="Joost", source="self"),
                       "S1": SpeakerInfo("S1", "Sprecher 1", suggested_name="Karl Fremd", note="klingt wie Karl")})
    rec.turns_path.write_text('[{"start": 0, "end": 25, "speaker": "S1"}]', encoding="utf-8")
    from callrec.models import Utterance

    rec.save_utterances([Utterance(0, 4, "ME", "Guten Morgen", "mic"), Utterance(5, 9, "S1", "Hallo", "remote")])
    store = ProfileStore(PlainFilePersistence(tmp_path / "vp.json"))
    store.learn("Anna", "", np.ones(8), "m", recording=rec.uid, speaker="S1", source="manual", seconds=25,
                title="Jour fixe", folder=rec.folder.name)
    seen = {}

    def drive():
        win = toplevel(root)
        seen["me"] = find(win, ttk.Label, "Ich – Joost (Mikrofonspur)").cget("text")
        seen["values"] = find(win, ttk.Combobox).cget("values")
        win.destroy()

    root.after(200, drive)
    assert dialogs.review_speakers(root, rec, store, lambda: None, lambda: None) is False
    assert "1 Beiträge, 00:04 Sprechzeit" in seen["me"]
    assert "klingt wie Karl Fremd (kein Teilnehmer dieses Termins)" in seen["values"]
    assert "Das bin ich (meine Stimme auf der Spur der Gegenseite)" in seen["values"]

    def drive_profiles():
        win = toplevel(root)
        trees = [w for w in walk(win) if isinstance(w, ttk.Treeview)]
        trees[0].selection_set(trees[0].get_children()[0])
        root.update()
        seen["origins"] = [trees[1].item(i, "values") for i in trees[1].get_children()]
        win.destroy()

    root.after(200, drive_profiles)
    dialogs.manage_profiles(root, store, rebuild=lambda pid: "ok")
    assert seen["origins"][0][0] == f"Jour fixe ({rec.folder.name})" and seen["origins"][0][2] == "selbst zugeordnet"

    def drive_participants():
        win = toplevel(root)
        txt = find(win, tk.Text)
        txt.delete("1.0", "end")
        txt.insert("1.0", "Müller, Anna <anna@firma.de>\nKarl Fremd")
        find(win, ttk.Button, "Speichern").invoke()

    root.after(200, drive_participants)
    people = dialogs.edit_participants(root, rec)
    assert [p.name for p in people or []] == ["Müller, Anna", "Karl Fremd"]


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

    from callrec import paths

    snap = paths.models_dir() / "whisper" / "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo" / "snapshots" / "c1"
    snap.mkdir(parents=True)
    for f in ("config.json", "tokenizer.json", "vocabulary.json"):
        (snap / f).write_text("{}")
    (snap / "model.bin").write_bytes(b"x" * 2_000_000)
    dlg = st.SettingsDialog(root, cfg, ["Headset Mikrofon (Jabra)"], ["Kopfhörer (Jabra)"],
                            lambda c: save_config(c, path), provider_factory=lambda c: FakeProvider())
    tabs = [dlg.nb.tab(t, "text") for t in dlg.nb.tabs()]
    assert tabs == ["Allgemein", "Aufnahme-Ende", "Transkription", "Sprache & Qualität", "Sprecher", "Darstellung", "Zusammenfassung",
                    "Microsoft-Konto", "SharePoint", "Löschen"]
    assert dlg.vars[("transcription", "language_candidates")].get() == "de, en"
    dlg.vars[("transcription", "language_candidates")].set("de, en; fr")
    dlg.vars[("audio", "mic_device")].set("Headset Mikrofon (Jabra)")
    dlg.vars[("transcription", "beam_size")].set("7")
    dlg.vars[("transcription", "vad_threshold")].set("0,35")  # German decimal comma accepted
    dlg.vars[("transcript", "timestamp_format")].set("mm:ss")
    dlg.vars[("handoff", "folder")].set(str(tmp_path))
    dlg.refresh_models()
    dlg.model_var.set("gemma3:4b")
    dlg.pull_model()

    def wait_then_save():
        if not pulled or "Ollama-Version 0.13.1" not in dlg.status_var.get() or dlg.models_list.size() == 0:
            root.after(100, wait_then_save)
            return
        assert "gemma3:4b" in dlg.model_combo.cget("values")
        assert dlg.models_list.get(0).startswith("large-v3-turbo") and "2 MB" in dlg.models_list.get(0)
        assert "vollständig" in dlg.models_list.get(0)
        find(dlg.win, ttk.Button, "Speichern").invoke()

    root.after(100, wait_then_save)
    assert dlg.run() is True
    c = load_config(path)
    assert c.audio.mic_device == "Headset Mikrofon (Jabra)" and c.audio.loopback_device == "auto"
    assert c.transcription.beam_size == 7 and c.transcription.vad_threshold == 0.35
    assert c.transcript.timestamp_format == "mm:ss" and c.summary.model == "gemma3:4b"
    assert c.handoff.folder == str(tmp_path) and c.general.preset == ""
    assert pulled == ["gemma3:4b"]
    assert c.transcription.language_candidates == ["de", "en", "fr"] and c.transcription.language == "auto"

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


def test_start_dialog_names_anthropic_when_active(root, tmp_path: Path):
    from callrec.consent import CONSENT_STATEMENT_ANTHROPIC

    def drive():
        win = toplevel(root)
        texts = [str(w.cget("text")) for w in walk(win) if isinstance(w, ttk.Label)]
        assert CONSENT_STATEMENT_ANTHROPIC in texts and any("verlässt" in t for t in texts)
        find(win, ttk.Checkbutton, "Übertragung an Anthropic zugestimmt").invoke()
        find(win, ttk.Button, "Aufnahme starten").invoke()

    root.after(200, drive)
    decision = dialogs.ask_start(root, Suggestion(None, [], ""), Person("Ich"), calendar(tmp_path), summary_provider="anthropic")
    assert decision is not None and decision.consent.summary_provider == "anthropic"
    require_consent(decision.consent, summary_provider="anthropic")


def test_settings_anthropic_needs_confirmation_and_shows_effective_device(root, tmp_path: Path):
    from callrec.asr.gpu import GpuStatus
    from callrec.config import Config, load_config, save_config
    from callrec.ui import settings as st

    st.messagebox.showinfo = lambda *a, **k: None  # type: ignore[assignment]
    asked: list[str] = []
    answer = {"v": False}

    def askyesno(title, message, **k):
        asked.append(message)
        return answer["v"]

    st.messagebox.askyesno = askyesno  # type: ignore[assignment]
    path = tmp_path / "c.toml"
    tested: list[tuple[str, str]] = []

    class FakeClaude:
        def test_connection(self):
            return "Verbindung zu Anthropic OK: Modell Claude Opus 5.5"

    def factory(cfg, typed):
        tested.append((cfg.summary.anthropic_model, typed))
        return FakeClaude()

    dlg = st.SettingsDialog(root, Config(), [], [], lambda c: save_config(c, path),
                            gpu_status_fn=lambda force: GpuStatus(False, 0, "kein NVIDIA-Treiber (nvcuda.dll) gefunden"),
                            anthropic_factory=factory)
    assert dlg.vars[("summary", "anthropic_model")].get() == "claude-opus-5-5"
    assert "Standard" in dlg.anthropic_model_info.get()

    def wait_gpu():
        if "Effektiv" not in dlg.effective_var.get() or "CPU" not in dlg.effective_var.get():
            root.after(50, wait_gpu)
            return
        assert "large-v3-turbo (int8)" in dlg.effective_var.get()
        dlg.vars[("transcription", "model")].set("large-v3")
        assert "Modell large-v3 (int8)" in dlg.effective_var.get()
        dlg.vars[("summary", "provider")].set("anthropic")
        dlg.key_var.set("sk-ant-api03-typed-xxxxxxxxxxxxxxxxxxxx")
        dlg.test_anthropic()
        root.after(50, wait_test)

    def wait_test():
        if "OK" not in dlg.key_status.get():
            root.after(50, wait_test)
            return
        find(dlg.win, ttk.Button, "Speichern").invoke()  # answer "Nein": nothing saved
        assert not path.exists() and "an Anthropic" in asked[-1]
        answer["v"] = True
        find(dlg.win, ttk.Button, "Speichern").invoke()

    root.after(50, wait_gpu)
    assert dlg.run() is True
    c = load_config(path)
    assert c.summary.provider == "anthropic" and c.summary.anthropic_transcripts_leave_this_computer_confirmed
    assert tested == [("claude-opus-5-5", "sk-ant-api03-typed-xxxxxxxxxxxxxxxxxxxx")]
    assert "sk-ant" not in path.read_text(encoding="utf-8")

    # switching back to a local provider revokes the confirmation
    dlg2 = st.SettingsDialog(root, c, [], [], lambda x: save_config(x, path),
                             gpu_status_fn=lambda force: GpuStatus(True, 1))
    dlg2.vars[("summary", "provider")].set("ollama")
    root.after(100, lambda: find(dlg2.win, ttk.Button, "Speichern").invoke())
    assert dlg2.run() is True
    assert not load_config(path).summary.anthropic_transcripts_leave_this_computer_confirmed


def test_overlay_shows_autostop_countdown(root):
    from callrec.autostop import AutoStopView
    from callrec.ui.tkui import RecordingOverlay

    class FakeRunner:
        def __init__(self):
            self.v = None
            self.kept = 0
            self.stopped = 0

        def view(self):
            return self.v

        def keep_recording(self):
            self.kept += 1
            self.v = None

        def stop_now(self):
            self.stopped += 1

    r = FakeRunner()
    ov = RecordingOverlay(root, lambda: None, r)
    ov.show("Test")
    root.update()
    assert not ov._prompt_shown
    r.v = AutoStopView("countdown", "silence", "Call scheint beendet – Aufnahme endet in 30 s", "", 30)
    ov._update_prompt()
    root.update()
    assert ov._prompt_shown and "endet in 30 s" in ov._prompt_label.cget("text")  # type: ignore[union-attr]
    ov._stop_btn.invoke()  # type: ignore[union-attr]
    ov._keep_btn.invoke()  # type: ignore[union-attr]
    assert r.stopped == 1 and r.kept == 1 and not ov._prompt_shown
    ov.hide()


def test_after_recording_dialog_full_later_discard(root, tmp_path: Path, monkeypatch):
    from callrec.postcall import Plan
    from tests.conftest import make_recording

    rec = make_recording(tmp_path / "recs", with_parts=False)

    def drive_full():
        win = toplevel(root)
        texts = [str(w.cget("text")) for w in walk(win) if isinstance(w, ttk.Label)]
        assert "Aufnahme beendet – jetzt vollständig verarbeiten?" in texts
        assert any("kein Übergabeordner" in t for t in texts)
        boxes = [w for w in walk(win) if isinstance(w, ttk.Checkbutton)]
        assert len(boxes) == 4 and "SharePoint" in str(boxes[3].cget("text"))
        boxes[3].invoke()  # tick the hand-over
        find(win, ttk.Button, "Vollständig verarbeiten").invoke()

    root.after(200, drive_full)
    choice, plan = dialogs.ask_after_recording(root, rec, Plan(), "kein Übergabeordner eingestellt")
    assert choice == "full" and plan == Plan(True, True, True, True)

    root.after(200, lambda: find(toplevel(root), ttk.Button, "Später").invoke())
    assert dialogs.ask_after_recording(root, rec, Plan()) == ("later", None)

    asked = []
    monkeypatch.setattr(dialogs.messagebox, "askyesno", lambda *a, **k: asked.append(a) or True)
    root.after(200, lambda: find(toplevel(root), ttk.Button, "Verwerfen").invoke())
    assert dialogs.ask_after_recording(root, rec, Plan()) == ("discard", None) and asked  # confirmed first


def test_settings_autostart_checkbox_and_handoff_without_asking(root, tmp_path: Path, monkeypatch):
    from callrec.autostart import Autostart
    from callrec.config import Config, load_config, save_config
    from callrec.ui import settings as st

    exe = tmp_path / "v" / "pythonw.exe"
    exe.parent.mkdir()
    exe.write_text("")

    class Reg:
        value = None

        def read(self):
            return self.value

        def write(self, c):
            self.value = c

        def delete(self):
            self.value = None

    reg = Reg()
    auto = Autostart(reg, str(exe), startup_dir=tmp_path / "startup")
    path = tmp_path / "config.toml"
    monkeypatch.setattr(st.messagebox, "showinfo", lambda *a, **k: None)
    asked = []
    monkeypatch.setattr(st.messagebox, "askyesno", lambda *a, **k: asked.append(a[0]) or False)
    dlg = st.SettingsDialog(root, Config(), [], [], lambda c: save_config(c, path), autostart=auto)
    assert dlg.autostart_var.get() is False
    dlg.autostart_var.set(True)
    dlg.vars[("processing", "after_recording")].set("full")
    dlg.vars[("processing", "handoff_without_asking")].set(True)
    dlg.do_save()  # the hand-over without asking needs an explicit "Ja" -> declined: nothing saved
    assert asked == ["Ohne Rückfrage übergeben?"] and not path.exists() and reg.value is None
    dlg.vars[("processing", "handoff_without_asking")].set(False)
    dlg.vars[("autostop", "countdown_s")].set("45")
    dlg.do_save()
    assert reg.value is not None and "--minimized" in reg.value
    cfg = load_config(path)
    assert cfg.processing.after_recording == "full" and cfg.autostop.countdown_s == 45


def test_prompt_editor_validates_restores_and_tests(root, tmp_path: Path):
    from callrec import paths
    from callrec.ui import settings as st
    from callrec.userfiles import SUMMARY_FINAL_TEMPLATE

    tested: list[int] = []
    asked: list[str] = []

    def drive():
        win = toplevel(root)
        nb = find(win, ttk.Notebook)
        nb.select(1)
        texts = [w for w in walk(win) if isinstance(w, tk.Text)]
        texts[1].delete("1.0", "end")
        texts[1].insert("1.0", "Nur {title}")
        from tkinter import messagebox

        messagebox.askyesno = lambda title, msg, **k: asked.append(msg) or False  # type: ignore[assignment]
        find(win, ttk.Button, "Mit dieser Aufnahme testen").invoke()  # refused: {material} missing
        assert tested == [] and "{material}" in asked[-1]
        messagebox.askyesno = lambda *a, **k: True  # type: ignore[assignment]
        find(win, ttk.Button, "Standard wiederherstellen").invoke()
        find(win, ttk.Button, "Mit dieser Aufnahme testen").invoke()
        assert tested == [1]
        find(win, ttk.Button, "Speichern").invoke()

    root.after(200, drive)
    assert st.edit_summary_prompts(root, test=lambda: tested.append(1)) is True
    assert (paths.prompts_dir() / "summary_final.txt").read_text(encoding="utf-8").strip() == SUMMARY_FINAL_TEMPLATE.strip()


def test_update_dialog_offers_update_or_waiting(root):
    from callrec.updater import Release, UpdateCheck, UpdateError

    rel = Release("0.2.0", "a" * 40, "2026-11-20", ["Neu: Updates"], ">=3.12")
    res = UpdateCheck("0.1.0", "b" * 40, rel, True, "Version 0.2.0 ist verfügbar.", requirements_changed=True)
    for busy, button, expected in (("", "Jetzt aktualisieren", "now"), ("Es läuft gerade eine Aufnahme.",
                                                                      "Nach Abschluss aktualisieren", "after")):
        root.after(200, lambda b=button: find(toplevel(root), ttk.Button, b).invoke())
        assert dialogs.ask_update(root, res, busy, {"previous_commit": "c" * 40}) == expected
    root.after(200, lambda: find(toplevel(root), ttk.Button, "Vorherige Version").invoke())
    assert dialogs.ask_update(root, UpdateError("GitHub nicht erreichbar"), "", {"previous_commit": "c" * 40}) == "restore"


def test_settings_transcription_profile_and_speed_measurement(root, tmp_path: Path, monkeypatch):
    """0.4: profile with its description, call behaviour, and "Geschwindigkeit messen" in the classic dialog."""
    import callrec.ui.settings as st
    from callrec.config import Config, load_config, save_config

    path = tmp_path / "config.toml"
    measured: list[str] = []

    def fake_bench(cfg, progress):
        progress(0.5, "misst ausgewogen")
        measured.append(cfg.transcription.profile)
        return {"runs": [], "clip": {"seconds": 90, "speech_s": 80}, "cores": 8, "model": "large-v3-turbo",
                "at": "2026-11-23T10:00", "tuned": {"lanes": 2, "threads": 4}}

    monkeypatch.setattr(st.messagebox, "askyesno", lambda *a, **k: True)
    dlg = st.SettingsDialog(root, Config(), [], [], lambda c: save_config(c, path), benchmark_fn=fake_bench)
    assert "Noch nicht gemessen" in dlg.speed_var.get() and "Whisper rechnet mit" in dlg.speed_var.get()
    assert dlg.profile_info.get().startswith("Empfohlen")
    dlg.vars[("transcription", "profile")].set("schnell")
    assert dlg.profile_info.get().startswith("Etwa 10–20 % schneller")
    dlg.vars[("processing", "during_call")].set("pause")
    dlg.run_benchmark()

    def wait_then_save():
        if "Gewählt: 2 Spur(en) × 4 Threads" not in dlg.speed_var.get():
            root.after(50, wait_then_save)
            return
        find(dlg.win, ttk.Button, "Speichern").invoke()

    root.after(50, wait_then_save)
    assert dlg.run() is True
    c = load_config(path)
    assert c.transcription.profile == "schnell" and c.processing.during_call == "pause" and measured == ["ausgewogen"]


def test_retranscribe_dialog_offers_the_profiles(root, tmp_path: Path):
    from callrec.ui import dialogs
    from tests.conftest import make_recording

    rec = make_recording(tmp_path / "r")

    def answer():
        win = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)][-1]
        radios = [w for w in walk(win) if isinstance(w, ttk.Radiobutton)]
        texts = [str(r.cget("text")) for r in radios]
        assert "Wie in den Einstellungen" in texts and any(t.startswith("Genau – ") for t in texts)
        next(r for r in radios if str(r.cget("text")).startswith("Genau – ")).invoke()
        find(win, ttk.Button, "Neu transkribieren").invoke()

    root.after(100, answer)
    assert dialogs.ask_retranscribe(root, rec, []) == ("auto", None, "genau")
