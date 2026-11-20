"""User actions shared by the tray menu and the main window (Tk thread only).

Each action goes through the existing building blocks - the controller (recording, queueing
pipeline steps, rename/delete), the existing dialogs (consent, speaker review, hand-over
preview, settings) and the factory - so tray, window and CLI cannot drift apart. Nothing in
here runs long work on the Tk thread except where the existing dialogs already did so.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from tkinter import messagebox
from typing import Any

from callrec import paths
from callrec.calendar.service import Suggestion
from callrec.config import Config, load_config, save_config
from callrec.controller import Controller
from callrec.factory import (
    build_calendar_service,
    build_pipeline,
    graph_auth,
    perform_handoff,
    reload_config_in_place,
)
from callrec.jobs import ProcessingQueue
from callrec.models import Person
from callrec.pipeline import STEP_PRESETS
from callrec.recording import Recording, list_recordings
from callrec.speakers.profiles import ProfileStore
from callrec.ui import dialogs

log = logging.getLogger(__name__)


TEXT_SUFFIXES = (".md", ".log", ".txt", ".toml", ".json")


def open_path(path: Any, startfile: Callable[[str], None] | None = None,
              popen: Callable[..., Any] | None = None) -> None:
    """Open a file/folder with its Windows default application.

    ``.md`` has no default application on a fresh Windows 11 (startfile then raises
    "keine Anwendung zugeordnet"): text files fall back to Notepad, everything else to the
    Explorer with the file selected - a click must never silently do nothing."""
    if sys.platform != "win32" and startfile is None:
        log.info("Öffnen: %s", path)
        return
    import subprocess

    start = startfile or os.startfile  # type: ignore[attr-defined]
    run = popen or subprocess.Popen
    p = Path(str(path))
    try:
        start(str(p))
        return
    except OSError as exc:
        log.info("Öffnen von %s mit der Standardanwendung nicht möglich: %s", p, exc)
        if p.is_dir() or not p.exists():
            raise
    if p.suffix.lower() in TEXT_SUFFIXES:
        run(["notepad.exe", str(p)])  # noqa: S603, S607
    else:
        run(["explorer.exe", f"/select,{p}"])  # noqa: S603, S607


def language_label(code: str) -> str:
    from callrec.asr.language import language_name

    return "Sprache automatisch erkennen" if code == "auto" else language_name(code)


class AppActions:
    def __init__(self, cfg: Config, ui: Any, controller: Controller, queue: ProcessingQueue, profiles: ProfileStore,
                 me_provider: Callable[[], Person], log_file: Path | None = None) -> None:
        self.cfg = cfg
        self.ui = ui
        self.controller = controller
        self.queue = queue
        self.profiles = profiles
        self.me_provider = me_provider
        self.log_file = log_file
        self.opener: Callable[[Any], None] = open_path
        self.on_changed: list[Callable[[Recording | None], None]] = []  # recording data changed (refresh views)
        self.on_config_changed: list[Callable[[], None]] = []
        self.quit_handler: Callable[[], None] = lambda: None
        self.show_window_handler: Callable[[], None] = lambda: None
        self.autostop: Any = None  # AutoStopRunner (countdown banner in the main window), set by the app
        from callrec.autostart import Autostart
        from callrec.postcall import ProcessingChain

        self.autostart = Autostart.for_this_pc()

        # guided processing after a recording (question -> transcript -> speakers -> summary -> hand-over)
        self.chain = ProcessingChain(
            submit=lambda rec, steps, on_done: self.queue.submit(rec, steps, on_done=on_done),
            post=self.ui.post,
            review=self.review_in_chain,
            handoff=lambda rec, note: perform_handoff(self.cfg, rec, None, note),
            handoff_ready=self.handoff_ready,
            notify=self.ui.notify,
            changed=lambda rec: self.ui.post(lambda: self._changed(rec)),
            shutting_down=lambda: bool(getattr(self.queue, "_shutting_down", False)),
        )
        if hasattr(self.queue, "add_listener"):
            self.queue.add_listener(self.chain.on_queue_event)

    @property
    def root(self) -> Any:
        return self.ui.root

    def _changed(self, rec: Recording | None) -> None:
        for fn in list(self.on_changed):
            try:
                fn(rec)
            except Exception:
                log.exception("Aktualisierung fehlgeschlagen")

    # -- recording ----------------------------------------------------
    def toggle(self) -> None:
        self.controller.toggle()

    def start_recording(self, suggestion: Suggestion | None = None) -> bool:
        """Always through Controller.start -> consent dialog (no other path to a recording)."""
        return self.controller.start(suggestion)

    def stop_recording(self) -> None:
        if self.controller.stop() is not None:
            self._changed(None)

    # -- after a recording ----------------------------------------------
    def after_recording(self, rec: Recording, reason: str = "manual") -> None:
        """Controller hook after every stop (manual or automatic): ask once, or process right away
        in mode "full". At the app exit nothing is asked - the question comes at the next start."""
        from callrec.postcall import default_plan

        if reason == "app_quit":
            log.info("Aufnahme beim Beenden gestoppt - Rückfrage zur Verarbeitung beim nächsten Start")
            return
        pc = self.cfg.processing
        if pc.after_recording == "full":
            plan = default_plan(self.cfg)
            plan.handoff = bool(pc.handoff_without_asking)  # only this explicit setting, never a preset
            self.chain.start(rec, plan, "setting")
            self._changed(rec)
            return
        self.ui.post(lambda: self.ask_after_recording(rec))

    def ask_after_recording(self, rec: Recording) -> str:
        from callrec.postcall import default_plan, mark_deferred

        if not rec.meta_path.exists():
            return ""
        ready, why = self.handoff_ready()
        summary_problem = "" if self.cfg.summary.enabled else "in den Einstellungen ausgeschaltet"
        choice, plan = dialogs.ask_after_recording(self.root, rec, default_plan(self.cfg), "" if ready else why,
                                                   summary_problem)
        if choice == dialogs.AFTER_FULL:
            if not self.cloud_ok(rec, STEP_PRESETS["all"] if plan.summary else frozenset()):
                plan.summary = False
            self.chain.start(rec, plan, "dialog")
        elif choice == dialogs.AFTER_DISCARD:
            try:
                where = self.controller.delete(rec)
                self.ui.notify("Aufnahme verworfen", where)
            except Exception as exc:
                self.ui.notify("Verwerfen fehlgeschlagen", str(exc))
                mark_deferred(rec)
        else:
            mark_deferred(rec)
            self.ui.notify("Aufnahme gespeichert", f"{rec.folder.name}: noch nicht verarbeitet – im Hauptfenster "
                           "„Jetzt verarbeiten“.")
        self._changed(rec)
        return str(choice)

    def process_now(self, rec: Recording | None = None) -> bool:
        """One click for a deferred/unprocessed recording: the chain with the default steps - but never
        the hand-over (that needs the choice in the question after the recording)."""
        from callrec.postcall import default_plan

        rec = self._resolve(rec)
        if rec is None or self.controller.is_busy(rec):
            return False
        plan = default_plan(self.cfg)
        plan.handoff = False
        if not self.cloud_ok(rec, STEP_PRESETS["all"] if plan.summary else frozenset()):
            plan.summary = False
        self.chain.start(rec, plan, "button")
        self.ui.notify("Verarbeitung gestartet", rec.folder.name)
        self._changed(rec)
        return True

    def review_in_chain(self, rec: Recording) -> bool:
        pipeline = build_pipeline(self.cfg, self.profiles)
        return bool(dialogs.review_speakers(self.root, rec, self.profiles, rerender=lambda: pipeline.run(rec, {"render"}),
                                            resummarize=lambda: None, in_chain=True))

    # -- helpers ------------------------------------------------------
    def last_recording(self) -> Recording | None:
        """Most recent recording that is not currently being recorded."""
        current = self.controller.current.folder if self.controller.current is not None else None
        recs = [r for r in list_recordings(self.cfg.recordings_path()) if r.folder != current]
        return recs[0] if recs else None

    def _resolve(self, rec: Recording | None) -> Recording | None:
        rec = rec or self.last_recording()
        if rec is None:
            self.ui.notify("Keine Aufnahme", "Es gibt noch keine abgeschlossene Aufnahme.")
        return rec

    def cloud_ok(self, rec: Recording, steps: frozenset[str]) -> bool:
        """Older recording + Anthropic active: ask before its transcript is sent."""
        from callrec.consent import active_summary_provider, cloud_summary_covered

        if "summarize" not in steps or cloud_summary_covered(rec.load_meta(), active_summary_provider(self.cfg.summary)):
            return True
        if dialogs.confirm_cloud_summary(self.root, rec, self.me_provider()):
            return True
        self.ui.notify("Keine Übertragung", "Ohne Bestätigung wird die Zusammenfassung nicht von Claude erstellt.")
        return False

    # -- processing ---------------------------------------------------
    def resummarize(self, rec: Recording | None = None) -> bool:
        rec = self._resolve(rec)
        if rec is None or not self.cloud_ok(rec, STEP_PRESETS["resummarize"]):
            return False
        self.controller.resummarize(rec)
        self.ui.notify("Zusammenfassung wird neu erstellt", rec.folder.name)
        self._changed(rec)
        return True

    def retranscribe(self, rec: Recording | None = None, language: str | None = None, preset: str | None = None) -> bool:
        rec = self._resolve(rec)
        if rec is None or not self.cloud_ok(rec, STEP_PRESETS["retranscribe"]):
            return False
        self.controller.retranscribe(rec, language=language, preset=preset)
        what = language_label(language) if language else "aktuelle Einstellungen"
        self.ui.notify("Neu-Transkription gestartet", f"{rec.folder.name}: {what}" + (f", Preset {preset}" if preset else ""))
        self._changed(rec)
        return True

    def reprocess(self, rec: Recording | None = None) -> bool:
        rec = self._resolve(rec)
        if rec is None or not self.cloud_ok(rec, STEP_PRESETS["all"]):
            return False
        self.controller.reprocess(rec)
        self.ui.notify("Verarbeitung gestartet", rec.folder.name)
        self._changed(rec)
        return True

    def cancel_processing(self, job_id: int | None = None) -> str:
        text = self.queue.cancel(job_id)
        self.ui.notify("Verarbeitung abbrechen", text)
        return text

    # -- dialogs ------------------------------------------------------
    def review(self, rec: Recording | None = None) -> None:
        rec = self._resolve(rec)
        if rec is None:
            return
        if self.controller.is_busy(rec):
            # the dialog re-renders on this thread while the processing thread would write the
            # same files (speakers.json, transcript, meta.json) - wait for the job instead
            self.ui.notify("Wird noch verarbeitet", f"{rec.folder.name}: Sprecher zuordnen ist nach der Verarbeitung möglich.")
            return
        if not rec.speakers_path.exists():
            self.ui.notify("Noch nicht verarbeitet", "Die Aufnahme wird noch verarbeitet.")
            return
        pipeline = build_pipeline(self.cfg, self.profiles)
        dialogs.review_speakers(
            self.root, rec, self.profiles,
            rerender=lambda: pipeline.run(rec, {"render"}),
            resummarize=lambda: self.resummarize(rec),
            handoff=lambda: self.handoff(rec),
        )
        self._changed(rec)

    def handoff_ready(self) -> tuple[bool, str]:
        h = self.cfg.handoff
        if h.mode == "graph":
            if not h.graph_drive_id.strip() or not self.cfg.calendar.client_id.strip():
                return False, "Der Graph-Upload ist noch nicht vollständig eingerichtet (Laufwerks-ID und Microsoft-Anmeldung)."
            return True, ""
        if not h.folder.strip():
            return False, ("Es ist noch kein SharePoint-Übergabeordner eingestellt. Alles andere funktioniert ohne ihn; "
                           "für die Übergabe in SharePoint die Bibliothek mit OneDrive synchronisieren und den Ordner "
                           "unter Einstellungen → SharePoint eintragen (README Abschnitt 10).")
        if not Path(h.folder).expanduser().is_dir():
            return False, f"Der Übergabeordner {h.folder} ist nicht erreichbar (OneDrive pausiert oder Ordner verschoben?)."
        return True, ""

    def handoff(self, rec: Recording | None = None) -> bool:
        """Explicit user action: preview + confirm, then hand over the summary only."""
        rec = self._resolve(rec)
        if rec is None:
            return False
        ready, why = self.handoff_ready()
        if not ready:
            if messagebox.askyesno("SharePoint-Übergabe einrichten", why + "\n\nEinstellungen jetzt öffnen?", parent=self.root):
                self.settings("SharePoint")
            return False
        done = dialogs.ask_handoff(self.root, rec, self.cfg, lambda sha: perform_handoff(self.cfg, rec, sha))
        self._changed(rec)
        return done

    # -- calendar afterwards ------------------------------------------
    def assign_event(self, rec: Recording | None = None, done: Callable[[], None] | None = None) -> None:
        """"Termin zuordnen": look up the events in the recording's time window (worker thread),
        let the user pick one, attach it (participants for the speaker matching / assignment)."""
        from datetime import timedelta

        rec = self._resolve(rec)
        if rec is None:
            return
        calendar = self.controller.calendar
        if calendar.source is None:
            self.ui.notify("Kein Kalender verbunden", "Termin zuordnen braucht die Microsoft-Anmeldung: im Reiter "
                           "„Einrichtung“ Client-ID eintragen und „Bei Microsoft anmelden …“ klicken.")
            return
        start = rec.started_at()
        if start is None:
            self.ui.notify("Termin zuordnen", "Für diese Aufnahme ist keine Startzeit bekannt.")
            return
        end = start + timedelta(seconds=max(rec.duration(), 60.0))

        def worker() -> None:
            try:
                events = calendar.events_for_recording(start, end)
            except Exception as exc:
                self.ui.notify("Termin zuordnen", f"Kalender nicht abrufbar: {exc}")
                return
            self.ui.post(lambda: self._choose_event(rec, events, done))

        threading.Thread(target=worker, name="assign-event", daemon=True).start()

    def _choose_event(self, rec: Recording, events: list[Any], done: Callable[[], None] | None) -> None:
        from callrec.library import LibraryError, assign_event

        if not events:
            self.ui.notify("Termin zuordnen", f"Im Kalender gibt es keinen Termin zur Zeit von {rec.folder.name}.")
            return
        choice = dialogs.ask_event_for_recording(self.root, rec, events)
        if choice is None:
            return
        if self.controller.is_busy(rec):
            self.ui.notify("Wird noch verarbeitet", "Termin zuordnen ist nach der laufenden Verarbeitung möglich.")
            return
        try:
            msgs = assign_event(rec, choice[0], take_title=choice[1])
        except (LibraryError, OSError, ValueError) as exc:
            self.ui.notify("Termin zuordnen fehlgeschlagen", str(exc))
            return
        if rec.speakers_path.exists():
            # recognise voices again with the invitees as preferred candidates (manual assignments stay)
            self.controller.process(rec, frozenset({"identify", "render"}))
            msgs.append("Sprechererkennung läuft erneut mit den Eingeladenen.")
        self.ui.notify("Termin zugeordnet", " ".join(msgs))
        self._changed(rec)
        if done is not None:
            done()

    def ask_retranscribe(self, rec: Recording | None = None) -> bool:
        from callrec.config import list_presets

        rec = self._resolve(rec)
        if rec is None:
            return False
        choice = dialogs.ask_retranscribe(self.root, rec, list_presets())
        if choice is None:
            return False
        language, preset = choice
        return self.retranscribe(rec, language=language, preset=preset)

    def settings(self, tab: str = "") -> bool:
        mics: list[str] = []
        outs: list[str] = []
        if sys.platform == "win32" and not self.controller.is_recording:
            try:
                from callrec.audio.wasapi import WasapiBackend

                b = WasapiBackend()
                mics = [d.name for d in b.capture_devices()]
                outs = b.render_device_names()
                b.close()
            except Exception:
                log.exception("Geräteliste nicht verfügbar")
        from callrec.ui.settings import edit_settings

        base = load_config(preset="")  # edit config.toml itself, not the preset overlay
        if not edit_settings(self.root, base, mics, outs, save_config, initial_tab=tab, autostart=self.autostart):
            return False
        try:
            reload_config_in_place(self.cfg)
            # the calendar source depends on calendar.client_id: rebuild it in the shared service
            self.controller.calendar.source = build_calendar_service(self.cfg).source
            self.ui.show_overlay = self.cfg.ui.show_overlay
        except Exception as exc:
            self.ui.notify("Konfiguration", str(exc))
        for fn in list(self.on_config_changed):
            fn()
        return True

    def set_autostart(self, on: bool) -> str:
        try:
            st = self.autostart.enable() if on else self.autostart.disable()
        except OSError as exc:
            self.ui.notify("Autostart", f"Nicht möglich: {exc}")
            return str(exc)
        self.ui.notify("Autostart", st.detail)
        for fn in list(self.on_config_changed):
            fn()
        return st.detail

    def login(self, done: Callable[[str], None] | None = None) -> None:
        if not self.cfg.calendar.client_id.strip():
            self.ui.notify("Keine Client-ID", "Bitte zuerst die Anwendungs-ID unter Einstellungen → Microsoft-Konto "
                           "eintragen (README Abschnitt 5).")
            return

        def worker() -> None:
            try:
                graph_auth(self.cfg).login_interactive()
                me = getattr(self.me_provider, "refresh", lambda: None)()
                text = f"Angemeldet als {me.display if me else 'unbekannt'}."
                self.ui.notify("Angemeldet", text)
            except Exception as exc:
                text = f"Anmeldung fehlgeschlagen: {exc}"
                self.ui.notify("Anmeldung fehlgeschlagen", str(exc))
            if done is not None:
                done(text)

        threading.Thread(target=worker, name="login", daemon=True).start()

    def manage_profiles(self) -> None:
        dialogs.manage_profiles(self.root, self.profiles)

    # -- files --------------------------------------------------------
    def _open(self, path: Any) -> bool:
        try:
            self.opener(path)
            return True
        except OSError as exc:
            log.warning("Öffnen fehlgeschlagen: %s (%s)", path, exc)
            self.ui.notify("Öffnen nicht möglich", f"{path}: {exc}")
            return False

    def open_folder(self, rec: Recording | None = None) -> None:
        self._open(rec.folder if rec is not None else self.cfg.recordings_path())

    def play(self, rec: Recording) -> bool:
        from callrec.audio.finalize import find_audio

        fa = find_audio(rec.folder)
        if fa is None:
            self.ui.notify("Kein Audio", "Für diese Aufnahme gibt es (noch) keine fertigen Audiodateien.")
            return False
        return self._open(fa.mix or fa.mic)

    def open_log(self) -> None:
        if self.log_file is not None:
            self._open(self.log_file)

    def open_readme(self) -> None:
        from callrec.setupcheck import readme_path

        p = readme_path()
        self._open(p if p is not None else paths.config_dir())

    def rename(self, rec: Recording, title: str) -> str:
        new = self.controller.rename(rec, title)
        self._changed(rec)
        return new

    def delete(self, rec: Recording, permanent: bool = False) -> str:
        how = self.controller.delete(rec, permanent=permanent)
        self._changed(None)
        return how

    # -- models -------------------------------------------------------
    def download_whisper(self, report: Callable[[str], None], done: Callable[[bool, str], None]) -> None:
        """Download the selected Whisper model in a child process (as `callrec models --whisper`)."""
        from callrec.setupcheck import real_selected_whisper

        class Status:
            def message(self, text: str) -> None:
                report(text)

            def update(self, text: str) -> None:
                report(text)

            def finish(self) -> None:
                pass

        def worker() -> None:
            from callrec.cli import run_whisper_download

            try:
                model, _note = real_selected_whisper(self.cfg)()
                lines: list[str] = []
                rc = run_whisper_download(model, paths.models_dir() / "whisper", out=lines.append, status=Status())
                done(rc == 0, "\n".join(lines) or ("Fertig." if rc == 0 else f"Download fehlgeschlagen (Code {rc})."))
            except Exception as exc:
                log.exception("Modell-Download fehlgeschlagen")
                done(False, str(exc))

        threading.Thread(target=worker, name="whisper-download", daemon=True).start()

    def quit(self) -> None:
        self.quit_handler()

    def show_window(self) -> None:
        self.show_window_handler()
