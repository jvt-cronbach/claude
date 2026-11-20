"""Drives the main window end to end (needs tkinter and a display, e.g. Xvfb).

Real controller, processing queue, AppActions and recording folders; fake audio backend,
fake pipeline and patched modal dialogs (the consent dialog itself is covered in test_ui_smoke).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

tk = pytest.importorskip("tkinter")
if sys.platform != "win32" and not os.environ.get("DISPLAY"):
    pytest.skip("kein Display (Xvfb) verfügbar", allow_module_level=True)

from tkinter import ttk  # noqa: E402

from callrec.calendar.service import CalendarService, Suggestion  # noqa: E402
from callrec.config import Config  # noqa: E402
from callrec.consent import confirm_consent  # noqa: E402
from callrec.controller import Controller, StartDecision  # noqa: E402
from callrec.jobs import ProcessingQueue  # noqa: E402
from callrec.models import Person  # noqa: E402
from callrec.pipeline import ProcessingCancelled, ProcessResult  # noqa: E402
from callrec.recording import Recording  # noqa: E402
from callrec.ui import dialogs  # noqa: E402
from tests.conftest import make_event  # noqa: E402
from tests.test_controller import FakeBackend  # noqa: E402
from tests.test_mainwindow_logic import processed  # noqa: E402

NOW = datetime.now(UTC)


class FakePipeline:
    """Records the jobs; blocks while ``gate`` is cleared; result per recording configurable."""

    def __init__(self, world: World) -> None:
        self.w = world

    def run(self, rec, steps, progress, language=None, should_cancel=None):
        self.w.jobs.append((rec.folder.name, frozenset(steps), language))
        progress("transcribe", 0.4, "Spur mic")
        self.w.gate.wait(10)
        if should_cancel is not None and should_cancel():
            raise ProcessingCancelled()
        if self.w.fail_summary:
            return ProcessResult(steps_done=[s for s in steps if s != "summarize"], summary_failed=True,
                                 summary_skipped="Zeitüberschreitung bei der Verbindung zu api.anthropic.com.")
        return ProcessResult(steps_done=list(steps))


class World:
    def __init__(self, tmp_path: Path) -> None:
        from callrec.ui.actions import AppActions
        from callrec.ui.mainwindow import MainWindow
        from callrec.ui.tkui import TkUi

        self.tmp = tmp_path
        self.root_dir = tmp_path / "recs"
        processed(self.root_dir, "2026-11-19_1400_Projekt-Jour-fixe", "Projekt Jour fixe")
        processed(self.root_dir, "2026-11-18_1000_Kunde-Mueller", "Kunde Müller", summary=False, unassigned=2)
        processed(self.root_dir, "2026-11-17_0900_Team", "Team-Runde")
        self.sp = tmp_path / "sharepoint"
        self.sp.mkdir()
        cfg = Config()
        cfg.general.recordings_dir = str(self.root_dir)
        cfg.handoff.folder = str(self.sp)
        self.cfg = cfg
        self.gate = threading.Event()
        self.gate.set()
        self.jobs: list = []
        self.fail_summary = False
        ev = make_event("Projekt Jour fixe", start=NOW - timedelta(minutes=5), is_online=True)
        ev2 = make_event("Kundentermin", id="k", start=NOW - timedelta(minutes=8))

        class Src:
            def calendar_view(self, s, e):
                return [ev2, ev]

        self.calendar = CalendarService(Src(), tmp_path / "cal.json", timedelta(minutes=10), timedelta(minutes=10))
        self.ui = TkUi(self.calendar, show_overlay=False)
        from callrec.consent import active_summary_provider

        self.ui.summary_provider = lambda: active_summary_provider(cfg.summary)  # as run_tray_app wires it
        self.queue = ProcessingQueue(lambda preset=None: FakePipeline(self))
        self.controller = Controller(cfg, self.ui, FakeBackend, self.calendar, self.queue, lambda: Person("Ich Selbst"))
        self.actions = AppActions(cfg, self.ui, self.controller, self.queue, profiles=None,  # type: ignore[arg-type]
                                  me_provider=lambda: Person("Ich Selbst"), log_file=tmp_path / "log.txt")
        self.opened: list = []
        self.actions.opener = self.opened.append
        self.win = MainWindow(self.ui.root, self.actions, state_path=tmp_path / "window.json")
        self.ui.notify_listeners.append(self.win.notify)

    def pump(self, cond, timeout: float = 8.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.ui.root.update()
            if cond():
                return
            time.sleep(0.02)
        raise AssertionError("Bedingung nicht erreicht")

    def rows(self) -> list[tuple]:
        t = self.win.tree
        return [tuple(t.item(i, "values")) for i in t.get_children()]

    def close(self) -> None:
        self.gate.set()
        self.queue.stop()
        self.win.destroy()
        self.ui.root.destroy()


@pytest.fixture
def world(tmp_path: Path, monkeypatch):
    from callrec import setupcheck

    monkeypatch.setattr(setupcheck, "real_selected_whisper", lambda cfg: (lambda: ("large-v3-turbo", "")))
    monkeypatch.setattr(setupcheck, "real_find_whisper", lambda m: None)
    monkeypatch.setattr(setupcheck, "probe_summary_provider", lambda cfg: ("Server erreichbar, Modell gemma3:12b ist installiert.", True))
    w = World(tmp_path)
    yield w
    w.close()


def mainwindow_columns():
    from callrec.ui.mainwindow import COLUMNS

    return COLUMNS


def test_window_lists_filters_and_shows_details(world: World):
    w = world
    w.win.show()
    w.pump(lambda: len(w.rows()) == 3)
    rows = w.rows()
    assert [r[1] for r in rows] == ["Projekt Jour fixe", "Kunde Müller", "Team-Runde"]  # newest first
    assert rows[0][4:] == ("✓", "✓", "✓", "–") and rows[1][5:7] == ("2 offen", "–")
    assert rows[0][3] == "Deutsch" and w.win.count_var.get() == "3 Aufnahmen"
    # narrow list: language and duration are hidden (shown in the details), status columns stay
    w.win._fit_columns(w.win._columns_width(["date", "title", "t", "s", "z", "h"]))
    assert list(w.win.tree.cget("displaycolumns")) == ["date", "title", "t", "s", "z", "h"]
    w.win._fit_columns(5000)
    assert list(w.win.tree.cget("displaycolumns")) == [c[0] for c in mainwindow_columns()]

    w.win.search_var.set("müller")
    assert [r[1] for r in w.rows()] == ["Kunde Müller"] and w.win.count_var.get() == "1 von 3 Aufnahmen"
    w.win.search_var.set("")
    w.win.filter_var.set("Ohne Zusammenfassung")
    w.win.apply_filter()
    assert [r[1] for r in w.rows()] == ["Kunde Müller"]
    w.win.filter_var.set("Alle Aufnahmen")
    w.win.apply_filter()

    w.win.select("2026-11-19_1400_Projekt-Jour-fixe")
    w.pump(lambda: "Alles gut." in w.win.text_content("summary") and "Hallo" in w.win.text_content("transcript"))
    summary = w.win.text_content("summary")
    assert "##" not in summary and "Kurzfassung" in summary
    assert w.win.views["summary"]["var"].get().startswith("Version 1 (neueste)")
    assert w.win.d_title.get() == "Projekt Jour fixe" and "Donnerstag, 19.11.2026" in w.win.d_meta.get()
    assert "Noch nicht übergeben" in w.win.d_handoff.get()
    assert not w.win.buttons["handoff"].instate(["disabled"])

    w.win.select("2026-11-18_1000_Kunde-Mueller")
    w.pump(lambda: "Noch keine Zusammenfassung" in w.win.text_content("summary"))
    assert w.win.buttons["handoff"].instate(["disabled"])
    assert "2 offen" in str(w.win.buttons["review"].cget("text"))

    # folder / audio buttons go through the opener (os.startfile on Windows)
    w.win.invoke_action("folder")
    assert w.opened[-1] == w.root_dir / "2026-11-18_1000_Kunde-Mueller"


def test_record_start_goes_through_consent_dialog_and_stop_queues_processing(world: World, monkeypatch):
    w = world
    w.win.show()
    w.pump(lambda: len(w.win.event_combo.cget("values")) == 3)
    values = list(w.win.event_combo.cget("values"))
    assert "Projekt Jour fixe (Teams)" in values[0] and values[-1].startswith("Ohne Termin")
    w.win.event_var.set(values[1])  # pick the other current event
    asked: list[Suggestion] = []

    def fake_ask_start(root, suggestion, me, calendar, summary_provider=""):
        asked.append(suggestion)
        consent = confirm_consent(user_checked_box=True, confirmed_by=me.name, participants=suggestion.participants,
                                  method="dialog")
        return StartDecision(suggestion.event.subject, suggestion.participants, suggestion.event, consent)

    monkeypatch.setattr(dialogs, "ask_start", fake_ask_start)
    w.win.rec_btn.invoke()
    assert len(asked) == 1 and asked[0].event is not None and asked[0].event.subject == "Kundentermin"
    assert w.controller.is_recording
    w.pump(lambda: "Aufnahme läuft" in w.win.rec_state.get())
    assert "Kundentermin" in w.win.rec_state.get() and str(w.win.rec_btn.cget("text")).startswith("■")
    assert w.win.event_combo.instate(["disabled"])

    w.gate.clear()
    w.win.rec_btn.invoke()  # stop -> processing job
    assert not w.controller.is_recording
    w.pump(lambda: "Transkription 40 %" in w.win.job_var.get())
    assert not w.win.cancel_btn.instate(["disabled"])
    w.pump(lambda: len(w.rows()) == 4)
    w.gate.set()
    w.pump(lambda: w.win.job_var.get() == "Keine Verarbeitung aktiv." and w.win.msg_var.get().startswith("Fertig"))

    # a second click right after "stop" (double click) does not open a new consent dialog
    declined: list[int] = []
    monkeypatch.setattr(dialogs, "ask_start", lambda *a, **k: declined.append(1))
    w.controller._stopped_at = time.monotonic()
    w.win.rec_btn.invoke()
    assert declined == [] and not w.controller.is_recording

    # declining consent: nothing is recorded
    w.controller._stopped_at = float("-inf")
    w.win.event_var.set(values[-1])
    w.win.rec_btn.invoke()
    assert declined == [1] and not w.controller.is_recording


def test_actions_retranscribe_cancel_error_hint_handoff_rename_delete(world: World, monkeypatch):
    from tkinter import messagebox, simpledialog

    from callrec.ui import mainwindow

    w = world
    w.win.show()
    w.pump(lambda: len(w.rows()) == 3)
    w.win.select("2026-11-19_1400_Projekt-Jour-fixe")

    # re-transcribe with language choice -> queue job; then cancel it while it runs
    monkeypatch.setattr(dialogs, "ask_retranscribe", lambda root, rec, presets: ("en", None))
    w.gate.clear()
    w.win.invoke_action("retranscribe")
    w.pump(lambda: w.jobs and "Transkription" in w.win.job_var.get())
    assert w.jobs[-1] == ("2026-11-19_1400_Projekt-Jour-fixe", frozenset({"transcribe", "identify", "render", "summarize"}),
                          "en")
    w.win.cancel_btn.invoke()
    assert "Abbruch angefordert" in w.win.msg_var.get()
    w.gate.set()
    w.pump(lambda: "abgebrochen" in w.win.msg_var.get())

    # a summary that failed (Anthropic timeout) -> plain message, next step and a fix button
    w.fail_summary = True
    w.win.buttons["resummarize"].invoke()
    w.pump(lambda: "Problem" in w.win.msg_icon.get())
    msg = w.win.msg_var.get()
    assert "Zeitüberschreitung" in msg and "Nächster Schritt" in msg and "Zusammenfassung neu erstellen" in msg
    assert w.win.msg_btn.winfo_ismapped() and str(w.win.msg_btn.cget("text")) == "Zusammenfassung neu erstellen"
    w.fail_summary = False
    n = len(w.jobs)
    w.win.msg_btn.invoke()  # the fix button queues the re-summarisation
    w.pump(lambda: len(w.jobs) == n + 1)
    w.pump(lambda: w.win.job_var.get() == "Keine Verarbeitung aktiv.")

    # SharePoint hand-over through the (patched) preview dialog and the real hand-over code
    from callrec.handoff import build_summary_document

    def fake_ask_handoff(root, rec, cfg, perform):
        perform(build_summary_document(rec).sha256)
        return True

    monkeypatch.setattr(dialogs, "ask_handoff", fake_ask_handoff)
    w.win.buttons["handoff"].invoke()
    assert (w.sp / "2026-11-19_1400_Projekt-Jour-fixe.md").exists()
    w.pump(lambda: "Zuletzt übergeben" in w.win.d_handoff.get() and w.rows()[0][7].startswith("✓"))
    assert "unverändert" in w.win.d_handoff.get()

    # rename (title only, folder stays)
    monkeypatch.setattr(simpledialog, "askstring", lambda *a, **k: "Jour fixe neu")
    w.win.invoke_action("rename")
    w.pump(lambda: w.rows()[0][1] == "Jour fixe neu")
    assert (w.root_dir / "2026-11-19_1400_Projekt-Jour-fixe").is_dir()
    w.pump(lambda: w.win.job_var.get() == "Keine Verarbeitung aktiv.")

    # delete: no recycle bin here -> clearly worded permanent delete
    asked: list[str] = []

    def yes(title, message, **k):
        asked.append(title + "|" + message)
        return True

    monkeypatch.setattr(messagebox, "askyesno", yes)
    monkeypatch.setattr(mainwindow, "recycle_bin_available", lambda: False)
    w.win.select("2026-11-17_0900_Team")
    w.pump(lambda: w.win.is_enabled("delete"))
    w.win.invoke_action("delete")
    assert "ENDGÜLTIG" in asked[-1] and "unwiderruflich" in asked[-1]
    w.pump(lambda: len(w.rows()) == 2)
    assert not (w.root_dir / "2026-11-17_0900_Team").exists() and w.win.selected is None


def test_setup_tab_keyboard_and_window_state(world: World):
    w = world
    w.win.show()
    w.win.nb.select(w.win.tab_setup)
    w.pump(lambda: "Startklar" in w.win.setup_summary.get() or "Noch nicht" in w.win.setup_summary.get())
    texts = [str(c.cget("text")) for c in w.win.setup_rows.winfo_children() if isinstance(c, ttk.Label)]
    assert any("Microsoft-Anmeldung" in t for t in texts) and any("funktioniert auch ohne" in t for t in texts)
    assert any(str(b.cget("text")) == "Jetzt herunterladen" for f in w.win.setup_rows.winfo_children()
               if isinstance(f, ttk.Frame) for b in f.winfo_children())
    w.pump(lambda: any("Verbindungstest: Server erreichbar" in str(c.cget("text"))
                       for c in w.win.setup_rows.winfo_children() if isinstance(c, ttk.Label)))
    assert "Startklar" in w.win.setup_summary.get() and "optional" in w.win.setup_summary.get().lower()

    # keyboard: Ctrl+F jumps to the search box, Down moves into the list, Return into the text
    w.win.win.focus_force()
    w.pump(lambda: w.win.win.focus_get() is not None)
    w.win.win.event_generate("<Control-f>")
    w.pump(lambda: w.win.win.focus_get() == w.win.search)
    w.pump(lambda: len(w.rows()) == 3)
    w.win.search.event_generate("<Down>")
    w.pump(lambda: w.win.win.focus_get() == w.win.tree and w.win.selected is not None)
    w.win.tree.event_generate("<Return>")
    w.pump(lambda: w.win.win.focus_get() == w.win.views["summary"]["text"])
    # tab order starts at the record button and reaches the event picker
    chain = []
    widget = w.win.rec_btn
    for _ in range(6):
        chain.append(widget)
        widget = widget.tk_focusNext()
    assert w.win.event_combo in chain

    # closing hides (app keeps running in the tray) and remembers the geometry
    w.win.win.protocol("WM_DELETE_WINDOW")  # handler registered
    w.win.hide()
    w.pump(lambda: not w.win.win.winfo_viewable())
    from callrec.ui.viewmodel import WindowState

    st = WindowState.load(w.tmp / "window.json")
    assert st.geometry and st.tab == "Aufnahmen" or st.tab == "Einrichtung"
    w.win.show()
    w.pump(lambda: w.win.win.winfo_viewable())


def test_notifications_from_controller_appear_with_hint(world: World):
    w = world
    w.win.show()
    w.ui.notify("Aufnahme nicht möglich", "Audiogeräte nicht verfügbar: kein Headset")
    w.pump(lambda: "Audiogeräte" in w.win.msg_var.get())
    assert w.win.msg_icon.get() == "Problem:" and "Geräte" in str(w.win.msg_btn.cget("text"))
    w.ui.notify("Aufnahme läuft", "Titel")  # shown by the record panel itself, not repeated
    w.ui.root.update()
    assert "Audiogeräte" in w.win.msg_var.get()
    w.win.clear_message()
    assert not w.win.msg_frame.winfo_ismapped()


def test_hundreds_of_recordings_load_off_the_tk_thread(tmp_path: Path, monkeypatch):
    import shutil

    from callrec import library

    w = World(tmp_path)
    try:
        proto = w.root_dir / "2026-11-19_1400_Projekt-Jour-fixe"
        for i in range(400):
            shutil.copytree(proto, w.root_dir / f"2025-{i // 28 + 1:02d}-{i % 28 + 1:02d}_1000_Call-{i}")
        threads: set[str] = set()
        real = library.read_info
        monkeypatch.setattr(library, "read_info", lambda f: threads.add(threading.current_thread().name) or real(f))
        w.win.show()
        t0 = time.monotonic()
        w.ui.root.update()
        assert time.monotonic() - t0 < 1.0  # show() itself does not scan
        w.pump(lambda: len(w.rows()) == 403, timeout=60)
        assert threads == {"library"}
        # a refresh with nothing changed re-reads nothing
        threads.clear()
        w.win.refresh_list()
        w.pump(lambda: not w.win._loading)
        assert threads == set()
    finally:
        w.close()


def test_recording_from_tray_while_window_hidden_keeps_single_controller(world: World, monkeypatch):
    """The tray and the window share one controller: a recording started via toggle shows up in the window."""
    w = world

    def fake_ask_start(root, suggestion, me, calendar, summary_provider=""):
        consent = confirm_consent(user_checked_box=True, confirmed_by="Ich", participants=[Person("A")], method="dialog")
        return StartDecision("Spontan", [Person("A")], None, consent)

    monkeypatch.setattr(dialogs, "ask_start", fake_ask_start)
    w.actions.toggle()  # what the tray menu / hotkey call
    assert w.controller.is_recording
    w.win.show()
    w.pump(lambda: "Aufnahme läuft" in w.win.rec_state.get() and "Spontan" in w.win.rec_state.get())
    w.actions.toggle()
    assert not w.controller.is_recording
    rec = Recording(sorted(w.root_dir.iterdir())[-1])
    assert rec.load_meta()["consent"]["confirmed"] is True


def test_menu_bar_reaches_profiles_log_and_settings(world: World, monkeypatch):
    w = world
    w.win.show()
    called: list[str] = []
    monkeypatch.setattr(dialogs, "manage_profiles", lambda root, profiles: called.append("profiles"))
    monkeypatch.setattr(w.actions, "settings", lambda tab="": called.append(f"settings:{tab}") or False)
    extras = w.win.menus["Extras"]
    labels = [extras.entrycget(i, "label") for i in range(extras.index("end") + 1)]
    assert labels[:3] == ["Einstellungen …", "Stimmprofile verwalten …", "Bei Microsoft anmelden …"]
    extras.invoke(0)
    extras.invoke(1)
    w.win.menus["Datei"].invoke(1)  # log file through the opener
    assert called == ["settings:", "profiles"] and w.opened[-1] == w.tmp / "log.txt"


def test_handoff_without_sharepoint_folder_offers_settings(world: World, monkeypatch):
    from tkinter import messagebox

    w = world
    w.cfg.handoff.folder = ""
    w.win.show()
    w.pump(lambda: len(w.rows()) == 3)
    w.win.select("2026-11-19_1400_Projekt-Jour-fixe")
    asked: list[str] = []
    opened: list[str] = []
    monkeypatch.setattr(messagebox, "askyesno", lambda t, m, **k: asked.append(m) or True)
    monkeypatch.setattr(w.actions, "settings", lambda tab="": opened.append(tab) or False)
    monkeypatch.setattr(dialogs, "ask_handoff", lambda *a, **k: pytest.fail("kein Übergabedialog ohne Ziel"))
    w.win.buttons["handoff"].invoke()
    assert "noch kein SharePoint-Übergabeordner" in asked[0] and opened == ["SharePoint"]
    assert list(w.sp.iterdir()) == []


def test_reader_window_shows_formatted_summary(world: World):
    w = world
    w.win.show()
    w.pump(lambda: len(w.rows()) == 3)
    w.win.select("2026-11-19_1400_Projekt-Jour-fixe")
    w.pump(lambda: "Alles gut." in w.win.text_content("summary"))
    top = w.win.open_reader("summary")
    assert top is not None and "Projekt Jour fixe" in top.title()
    text = next(c for c in top.winfo_children() if isinstance(c, tk.Text))
    assert "Kurzfassung" in text.get("1.0", "end") and "##" not in text.get("1.0", "end")
    assert "h2" in text.tag_names("3.0") or any("h2" in text.tag_names(f"{i}.0") for i in range(1, 6))
    top.destroy()


def test_speaker_review_is_refused_while_the_recording_is_processed(world: World, monkeypatch):
    """The review dialog re-renders on the Tk thread; it must not run beside a job on the same recording."""
    w = world
    opened: list = []
    monkeypatch.setattr(dialogs, "review_speakers", lambda *a, **k: opened.append(a[1]))
    monkeypatch.setattr("callrec.ui.actions.build_pipeline", lambda cfg, profiles: None)
    w.win.show()
    w.pump(lambda: len(w.rows()) == 3)
    name = "2026-11-18_1000_Kunde-Mueller"
    rec = Recording(w.root_dir / name)
    w.gate.clear()
    w.actions.resummarize(rec)
    w.pump(lambda: w.queue.is_busy_with(rec.folder))
    w.win.select(name)
    w.pump(lambda: w.win.selected is not None and w.win.selected.id == name)
    assert w.win.buttons["review"].instate(["disabled"])
    w.actions.review(rec)  # e.g. from the "Sprecher zuordnen" hint or open_review_after_processing
    assert opened == []
    w.gate.set()
    w.pump(lambda: not w.queue.is_busy_with(rec.folder))
    rec.speakers_path.parent.mkdir(parents=True, exist_ok=True)
    rec.speakers_path.write_text("{}", encoding="utf-8")
    w.actions.review(rec)
    assert [r.folder for r in opened] == [rec.folder]


def test_autostop_countdown_banner_keep_and_stop_now(world: World, monkeypatch):
    from callrec.autostop import AutoStopRunner

    w = world
    clock = {"t": 1000.0}
    runner = AutoStopRunner(lambda: w.cfg.autostop, w.controller, w.ui.post, w.ui.notify, clock=lambda: clock["t"])
    w.actions.autostop = runner
    w.win.show()

    def fake_ask_start(root, suggestion, me, calendar, summary_provider=""):
        consent = confirm_consent(user_checked_box=True, confirmed_by=me.name, participants=[], method="dialog")
        return StartDecision("Spontan", [], None, consent)

    monkeypatch.setattr(dialogs, "ask_start", fake_ask_start)
    w.controller._stopped_at = float("-inf")
    assert w.actions.start_recording(Suggestion(None))
    w.pump(lambda: "Automatisches Ende:" in w.win.autostop_info.get())
    assert "Stille 3 min" in w.win.autostop_info.get() and "höchstens 4 h" in w.win.autostop_info.get()
    assert not w.win.autostop_frame.winfo_ismapped()

    silent = {"mic": 200.0, "remote": 200.0}
    monkeypatch.setattr(w.controller, "silence_durations", lambda: dict(silent))
    runner.step()
    w.pump(lambda: w.win.autostop_frame.winfo_ismapped())
    assert w.win.autostop_head.get() == "Call scheint beendet – Aufnahme endet in 30 s"
    assert "beiden Spuren" in w.win.autostop_detail.get()
    w.win.autostop_keep_btn.invoke()
    assert w.controller.is_recording and "keine weitere Nachfrage" in w.win.autostop_head.get()
    w.win.autostop_close_btn.invoke()
    w.pump(lambda: not w.win.autostop_frame.winfo_ismapped())

    clock["t"] += 16 * 60  # after the snooze, still silent -> prompt again
    runner.step()
    w.pump(lambda: w.win.autostop_frame.winfo_ismapped() and "endet in" in w.win.autostop_head.get())
    rec = w.controller.current
    w.win.autostop_stop_btn.invoke()
    assert not w.controller.is_recording
    stop = rec.load_meta()["recording"]["stop"]
    assert stop["reason"] == "silence" and stop["by"] == "user"
    w.pump(lambda: not w.win.autostop_frame.winfo_ismapped())


def test_autostop_countdown_expiry_stops_like_manual_stop(world: World, monkeypatch):
    from callrec.autostop import AutoStopRunner

    w = world
    clock = {"t": 0.0}
    runner = AutoStopRunner(lambda: w.cfg.autostop, w.controller, w.ui.post, clock=lambda: clock["t"])
    w.actions.autostop = runner

    def fake_ask_start(root, suggestion, me, calendar, summary_provider=""):
        consent = confirm_consent(user_checked_box=True, confirmed_by=me.name, participants=[], method="dialog")
        return StartDecision("Spontan", [], None, consent)

    monkeypatch.setattr(dialogs, "ask_start", fake_ask_start)
    w.controller._stopped_at = float("-inf")
    assert w.actions.start_recording(Suggestion(None))
    rec = w.controller.current
    monkeypatch.setattr(w.controller, "silence_durations", lambda: {"mic": 999.0, "remote": 999.0})
    runner.step()
    clock["t"] = 31
    runner.step()  # expired -> the stop is posted to the Tk thread
    w.pump(lambda: not w.controller.is_recording)
    meta = rec.load_meta()
    assert meta["recording"]["stop"]["by"] == "automatic" and meta["recording"]["status"] == "stopped"
    w.pump(lambda: any(j[0] == rec.folder.name for j in w.jobs))  # processed like a manual stop


def test_stop_asks_once_then_runs_the_chain_or_defers(world: World, monkeypatch):
    from callrec.postcall import Plan, chain_state

    w = world
    w.controller.after_stop = w.actions.after_recording

    def fake_ask_start(root, suggestion, me, calendar, summary_provider=""):
        consent = confirm_consent(user_checked_box=True, confirmed_by=me.name, participants=[], method="dialog")
        return StartDecision("Kette", [], None, consent)

    answers = [("full", Plan(True, False, True, False)), ("later", None)]
    asked = []

    def fake_after(root, rec, plan, handoff_problem="", summary_problem=""):
        asked.append((rec.folder.name, plan, handoff_problem))
        return answers.pop(0)

    monkeypatch.setattr(dialogs, "ask_start", fake_ask_start)
    monkeypatch.setattr(dialogs, "ask_after_recording", fake_after)
    w.controller._stopped_at = float("-inf")
    assert w.actions.start_recording(Suggestion(None))
    rec = w.controller.current
    w.actions.stop_recording()
    w.pump(lambda: len(asked) == 1)
    assert asked[0][1] == Plan(True, True, True, False)  # pre-set from the settings
    w.pump(lambda: chain_state(rec) is not None and chain_state(rec)["stage"] == "done")
    steps = [s for name, s, _ in w.jobs if name == rec.folder.name]
    assert steps == [frozenset({"finalize", "transcribe", "render"}), frozenset({"summarize"})]

    w.controller._stopped_at = float("-inf")
    assert w.actions.start_recording(Suggestion(None))
    rec2 = w.controller.current
    w.actions.stop_recording()
    w.pump(lambda: len(asked) == 2)
    w.pump(lambda: rec2.load_meta()["processing"].get("status") == "deferred")
    assert not any(name == rec2.folder.name for name, _, _ in w.jobs)
    w.win.refresh_list()
    w.pump(lambda: w.tree_has(rec2.folder.name) if hasattr(w, "tree_has") else w.win.tree.exists(rec2.folder.name))
    w.win.select(rec2.folder.name)
    assert w.win.is_enabled("process_now")
    w.win.invoke_action("process_now")
    w.pump(lambda: chain_state(rec2) is not None and chain_state(rec2)["stage"] == "done")
    assert chain_state(rec2)["handoff_authorized"] is False


def test_compact_view_shows_last_recordings_with_next_action(world: World, monkeypatch):
    w = world
    w.win.set_compact(True)
    w.win.show()
    w.pump(lambda: len(w.win.compact_items) == 3)
    assert w.win.compact_frame.winfo_ismapped() and not w.win.nb.winfo_ismapped()
    by = {name: (status, key) for name, status, key in w.win.compact_items}
    assert by["2026-11-18_1000_Kunde-Mueller"][1] == "review"  # 2 voices open
    buttons = [str(b.cget("text")) for b in w.win.compact_rows.winfo_children() if isinstance(b, ttk.Button)]
    assert "Sprecher zuordnen" in buttons and "Öffnen" in buttons
    reviewed = []
    monkeypatch.setattr(w.actions, "review", lambda rec=None: reviewed.append(rec.folder.name))
    next(b for b in w.win.compact_rows.winfo_children()
         if isinstance(b, ttk.Button) and str(b.cget("text")) == "Sprecher zuordnen").invoke()
    assert reviewed == ["2026-11-18_1000_Kunde-Mueller"]
    w.win.view_btn.invoke()  # "Mehr …" -> full view
    w.pump(lambda: w.win.nb.winfo_ismapped())
    assert not w.win.compact and str(w.win.view_btn.cget("text")) == "Kompakt"
    w.win.save_state()
    from callrec.ui.viewmodel import WindowState

    assert WindowState.load(w.tmp / "window.json").compact is False
