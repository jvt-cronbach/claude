"""User actions shared by the tray menu and the main window (UI thread only: the Tk thread of the
classic UI, the app thread of the web UI).

Each action goes through the existing building blocks - the controller (recording, queueing
pipeline steps, rename/delete), the dialogs (consent, speaker review, hand-over preview,
settings; reached through ``self.dialogs``, a :class:`callrec.ui.dialogport.DialogPort`) and the
factory - so tray, both windows and CLI cannot drift apart. Nothing in here runs long work on the
UI thread except where the existing dialogs already did so.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
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
from callrec.ui.dialogport import AFTER_DISCARD, AFTER_FULL, DialogPort, TkDialogPort

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


def datetime_now() -> Any:
    from datetime import datetime

    return datetime.now().astimezone()


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
        # recordings with an open question or dialog (after-recording question, speaker assignment):
        # never deleted automatically meanwhile (not in the queue, so Controller.is_busy cannot see them)
        self._held: dict[str, int] = {}  # paths.folder_key -> number of open questions/dialogs
        self._held_lock = threading.Lock()
        # the questions to the user (Tk dialogs by default; the web UI sets its in-page dialogs)
        self.dialogs: DialogPort = TkDialogPort(lambda: self.ui.root)
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
            auto_handoff_allowed=lambda: (self.cfg.processing.after_recording == "full"
                                          and self.cfg.processing.handoff_without_asking),
        )
        if hasattr(self.queue, "add_listener"):
            self.queue.add_listener(self.chain.on_queue_event)

    @property
    def root(self) -> Any:
        return self.ui.root

    def _after(self, ms: int, fn: Callable[[], object]) -> None:
        after = getattr(self.ui, "after", None)
        if callable(after):
            after(ms, fn)
        else:
            self.root.after(ms, fn)

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

        if reason in ("app_quit", "system_shutdown"):
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

    @contextlib.contextmanager
    def hold(self, rec: Recording) -> Iterator[None]:
        from callrec.paths import folder_key

        key = folder_key(rec.folder)  # the web UI names folders resolved, the lists do not
        with self._held_lock:
            self._held[key] = self._held.get(key, 0) + 1
        try:
            yield
        finally:
            with self._held_lock:
                n = self._held.get(key, 0) - 1
                if n > 0:
                    self._held[key] = n
                else:
                    self._held.pop(key, None)

    def retention_busy(self, rec: Recording) -> bool:
        """Not to be deleted automatically now: recorded, queued, processed or a dialog about it is open."""
        from callrec.paths import folder_key

        key = folder_key(rec.folder)
        with self._held_lock:
            held = key in self._held
        return held or self.controller.is_busy(rec)

    def ask_after_recording(self, rec: Recording) -> str:
        if not rec.meta_path.exists():
            return ""
        with self.hold(rec):
            return self._ask_after_recording(rec)

    def _ask_after_recording(self, rec: Recording) -> str:
        from callrec.postcall import default_plan, mark_deferred

        ready, why = self.handoff_ready()
        summary_problem = "" if self.cfg.summary.enabled else "in den Einstellungen ausgeschaltet"
        choice, plan = self.dialogs.ask_after_recording(rec, default_plan(self.cfg), "" if ready else why,
                                                        summary_problem)
        if choice == AFTER_FULL:
            if not self.cloud_ok(rec, STEP_PRESETS["all"] if plan.summary else frozenset()):
                plan.summary = False
            self.chain.start(rec, plan, "dialog")
        elif choice == AFTER_DISCARD:
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
        return bool(self.dialogs.review_speakers(rec, self.profiles, rerender=lambda: pipeline.run(rec, {"render"}),
                                                 resummarize=lambda: None, in_chain=True, speaker_cfg=self.cfg.speakers))

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
        if self.dialogs.confirm_cloud_summary(rec, self.me_provider()):
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

    def retranscribe(self, rec: Recording | None = None, language: str | None = None, preset: str | None = None,
                     profile: str | None = None) -> bool:
        rec = self._resolve(rec)
        if rec is None or not self.cloud_ok(rec, STEP_PRESETS["retranscribe"]):
            return False
        self.controller.retranscribe(rec, language=language, preset=preset, **({'profile': profile} if profile else {}))
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
        with self.hold(rec):  # the dialog writes speakers.json and the transcript
            self.dialogs.review_speakers(
                rec, self.profiles,
                rerender=lambda: pipeline.run(rec, {"render"}),
                resummarize=lambda: self.resummarize(rec),
                handoff=lambda: self.handoff(rec),
                speaker_cfg=self.cfg.speakers,
                allow_more=lambda: self.allow_more_speakers(rec),
            )
        self._changed(rec)

    def rerender(self, rec: Recording | None = None) -> bool:
        """Transcript again from the existing recognition (current names/settings, no new transcription)."""
        rec = self._resolve(rec)
        if rec is None or self.controller.is_busy(rec):
            return False
        self.controller.process(rec, STEP_PRESETS["render"])
        self.ui.notify("Transkript wird neu erstellt", rec.folder.name)
        self._changed(rec)
        return True

    def allow_more_speakers(self, rec: Recording | None = None, allow: bool = True) -> bool:
        """ "Weitere Sprecher zulassen": lift (or restore) the limit of remote speakers to the participants
        for this recording; recomputed from the cached diarization (no audio)."""
        rec = self._resolve(rec)
        if rec is None or self.controller.is_busy(rec):
            return False
        rec.update_meta(["speaker_limit"], {"lifted": bool(allow)})
        if rec.speakers_path.exists():
            self.controller.process(rec, frozenset({"identify", "render"}))
        self.ui.notify("Sprecher der Gegenseite", ("Weitere Sprecher zugelassen" if allow else
                       "Wieder auf die Teilnehmenden begrenzt") + f": {rec.folder.name} wird neu erkannt.")
        self._changed(rec)
        return True

    def toggle_speaker_limit(self, rec: Recording | None = None) -> bool:
        rec = self._resolve(rec)
        if rec is None:
            return False
        lifted = bool((rec.load_meta().get("speaker_limit") or {}).get("lifted"))
        return self.allow_more_speakers(rec, allow=not lifted)

    def edit_participants(self, rec: Recording | None = None) -> bool:
        """ "Teilnehmende bearbeiten": who may be recognised automatically; identification runs again
        (manual assignments stay)."""
        from callrec.review import set_participants

        rec = self._resolve(rec)
        if rec is None:
            return False
        if self.controller.is_busy(rec):
            self.ui.notify("Wird noch verarbeitet", f"{rec.folder.name}: Teilnehmende sind danach änderbar.")
            return False
        people = self.dialogs.edit_participants(rec)
        if people is None:
            return False
        msgs = set_participants(rec, people)
        if rec.speakers_path.exists():
            self.controller.process(rec, frozenset({"identify", "render"}))
            msgs.append("Sprechererkennung läuft erneut mit diesen Teilnehmenden (manuelle Zuordnungen bleiben).")
        self.ui.notify("Teilnehmende gespeichert", " ".join(msgs))
        self._changed(rec)
        return True

    def recheck_speakers(self, rec: Recording | None = None) -> bool:
        """ "Sprecherzuordnung neu prüfen": automatic assignments under the participants rule, after a
        preview; manual ones are never touched."""
        from callrec.review import archive_speakers, foreign_old_assignments, rebuild_profile, reset_speakers

        rec = self._resolve(rec)
        if rec is None or not rec.speakers_path.exists():
            return False
        if self.controller.is_busy(rec):
            self.ui.notify("Wird noch verarbeitet", f"{rec.folder.name}: erst nach der Verarbeitung möglich.")
            return False
        pipeline = build_pipeline(self.cfg, self.profiles)
        foreign = foreign_old_assignments(rec, pipeline._participants(rec))
        if foreign and self.dialogs.confirm_reset_foreign(rec, foreign):
            archive_speakers(rec)
            names = {name for _sid, _label, name in foreign}
            reset_speakers(rec, [sid for sid, _l, _n in foreign], self.profiles)
            for p in self.profiles.all_profiles():
                if p.name in names:
                    rebuild_profile(self.profiles, p.id, self.cfg.recordings_path())
        try:
            changes = pipeline.preview_identification(rec)
        except Exception as exc:
            log.exception("Vorschau der Sprecherzuordnung fehlgeschlagen")
            self.ui.notify("Sprecherzuordnung neu prüfen", f"Nicht möglich: {exc}")
            return False
        if not self.dialogs.confirm_recheck(rec, changes):
            return False
        from callrec.review import legacy_profile_updates

        sharpened = [n for n in legacy_profile_updates(rec) if any(before == n for _l, before, _a in changes)]
        archive_speakers(rec)
        self.controller.process(rec, frozenset({"identify", "render"}))
        self.ui.notify("Sprecherzuordnung wird neu geprüft", rec.folder.name + (
            f"\nDie frühere Version hat die Stimmprofile {', '.join(sharpened)} mit dieser Aufnahme nachgeschärft. "
            "Waren diese Zuordnungen falsch: Stimmprofile verwalten → „Neu aufbauen“." if sharpened else ""))
        self._changed(rec)
        return True

    # -- speaker recognition: explanation, diagnostics, re-identification, one-time repair (0.4.1) ----
    def explain_speakers(self, rec: Recording | None = None) -> bool:
        """ "Warum (nicht) erkannt?": for every remote voice the candidates, scores and deciding rule."""
        from callrec.review import recognition_text

        rec = self._resolve(rec)
        if rec is None or not rec.speakers_path.exists():
            return False
        self.dialogs.show_text(f"Sprechererkennung – {rec.title()}", recognition_text(rec))
        return True

    def export_diagnostics(self, rec: Recording | None = None) -> str:
        """A small anonymised file (scores and rule outcomes, no content) to send back; names only if wanted."""
        from callrec.speakers import diagnostics

        rec = self._resolve(rec)
        if rec is None or not rec.speakers_path.exists():
            return ""
        names = self.dialogs.ask_yes_no(
            "Diagnose der Sprechererkennung",
            "Die Datei enthält nur Zahlen und Regelergebnisse (Ähnlichkeiten, Sprechzeiten, welche Regel "
            "entschieden hat) – keinen Ton, keinen Text, keine Stimmmerkmale, keinen Titel.\n\n"
            "Namen der Personen einschließen?\nNein = Personen erscheinen als „Person 1“, „Person 2“ …")
        out = diagnostics.export(rec, diagnostics.default_path(rec), self.cfg, include_names=names)
        self.ui.notify("Diagnose gespeichert", str(out))
        self._open(out.parent)
        return str(out)

    def reidentifiable(self) -> list[Recording]:
        return [r for r in list_recordings(self.cfg.recordings_path())
                if r.speakers_path.exists() and r.embeddings_path.exists() and not self.controller.is_busy(r)
                and not self.retention_busy(r)]

    def reidentify_all(self, ask: bool = True) -> int:
        """ "Sprecher neu erkennen" for every recording that still has speaker data, with the current rules
        and profiles. Your own assignments are never changed; a copy of each speakers.json is archived."""
        from callrec.review import archive_speakers

        recs = self.reidentifiable()
        if not recs:
            self.ui.notify("Sprecher neu erkennen", "Keine Aufnahme mit Sprecherdaten vorhanden (ältere werden nach "
                           f"{self.cfg.retention.max_age_days} Tagen gelöscht).")
            return 0
        if ask and not self.dialogs.ask_yes_no(
                "Sprecher neu erkennen",
                f"{len(recs)} Aufnahme(n) mit den aktuellen Regeln und Stimmprofilen neu erkennen?\n\nEigene "
                "Zuordnungen bleiben unverändert; geänderte Namen markieren eine vorhandene Zusammenfassung als "
                "veraltet."):
            return 0
        for rec in recs:
            archive_speakers(rec)
            self.controller.process(rec, frozenset({"identify", "render"}))
        self.ui.notify("Sprecher neu erkennen", f"{len(recs)} Aufnahme(n) werden neu erkannt.")
        self._changed(None)
        return len(recs)

    def offer_profile_repair(self) -> list[str]:
        """Once after the update to 0.4.1: rebuild the voice profiles from your own assignments, link
        names typed once to the calendar identity, merge the same person under two addresses."""
        from callrec.speakers import repair

        if not repair.is_due():
            return []
        if not self.profiles.all_profiles():
            repair.mark_done("no profiles")
            return []
        rp = repair.plan(self.profiles, self.cfg.recordings_path())
        if rp.empty:
            repair.mark_done("nothing")
            return []
        text = ("Die Sprechererkennung arbeitet ab dieser Version mit neuen Regeln. Die vorhandenen Stimmprofile "
                "lassen sich einmalig aus den eigenen Zuordnungen neu aufbauen:\n\n• " + "\n• ".join(rp.lines())
                + "\n\nJetzt übernehmen? (Später: Stimmprofile verwalten → „Reparieren“)")
        if not self.dialogs.ask_yes_no("Stimmprofile reparieren", text):
            repair.mark_done("declined")
            return []
        done = repair.apply(self.profiles, rp)
        self.dialogs.show_text("Stimmprofile repariert", "\n".join(done))
        self.reidentify_all(ask=True)
        return done

    def repair_profiles(self) -> list[str]:
        """The same repair on demand (profile manager)."""
        from callrec.speakers import repair

        rp = repair.plan(self.profiles, self.cfg.recordings_path())
        if rp.empty:
            self.dialogs.show_text("Stimmprofile reparieren", "\n".join(rp.lines()))
            return []
        if not self.dialogs.ask_yes_no("Stimmprofile reparieren", "\n".join(rp.lines()) + "\n\nÜbernehmen?"):
            return []
        done = repair.apply(self.profiles, rp)
        self.dialogs.show_text("Stimmprofile repariert", "\n".join(done))
        return done

    def schedule_profile_repair(self, delay_ms: int = 25000) -> None:
        self._after(delay_ms, self.offer_profile_repair)

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
            if self.dialogs.ask_yes_no("SharePoint-Übergabe einrichten", why + "\n\nEinstellungen jetzt öffnen?"):
                self.settings("SharePoint")
            return False
        done = self.dialogs.ask_handoff(rec, self.cfg, lambda sha: perform_handoff(self.cfg, rec, sha))
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
        choice = self.dialogs.ask_event_for_recording(rec, events)
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
            msgs.append("Sprechererkennung läuft erneut: automatisch zugeordnet werden nur noch Personen dieses Termins "
                        "(manuelle Zuordnungen bleiben).")
        self.ui.notify("Termin zugeordnet", " ".join(msgs))
        self._changed(rec)
        if done is not None:
            done()

    def ask_retranscribe(self, rec: Recording | None = None) -> bool:
        from callrec.config import list_presets

        rec = self._resolve(rec)
        if rec is None:
            return False
        choice = self.dialogs.ask_retranscribe(rec, list_presets())
        if choice is None:
            return False
        language, preset, *rest = choice
        profile = rest[0] if rest else None
        return self.retranscribe(rec, language=language, preset=preset, profile=profile)

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
        base = load_config(preset="")  # edit config.toml itself, not the preset overlay
        if not self.dialogs.edit_settings(base, mics, outs, save_config, initial_tab=tab, autostart=self.autostart):
            return False
        self.apply_saved_config()
        return True

    def apply_saved_config(self) -> None:
        """After config.toml was saved (either settings UI): take it over in the running app."""
        try:
            reload_config_in_place(self.cfg)
            if not self.cfg.retention.enabled:
                from callrec import retention

                retention.revoke_acknowledgement()  # switched on again later: asked again with the list
            if self.cfg.ui.frontend == "web":  # saved with the new interface chosen: try it again next start
                from callrec.ui.webapp import clear_start_marker

                clear_start_marker()
            # the calendar source depends on calendar.client_id: rebuild it in the shared service
            self.controller.calendar.source = build_calendar_service(self.cfg).source
            self.ui.show_overlay = self.cfg.ui.show_overlay
        except Exception as exc:
            self.ui.notify("Konfiguration", str(exc))
        for fn in list(self.on_config_changed):
            fn()

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

    def edit_prompts(self, rec: Recording | None = None) -> bool:
        """ "Zusammenfassungs-Prompt bearbeiten" (main window); with a selected recording also
        "Mit dieser Aufnahme testen" (a new summary version, the earlier one stays for comparison)."""
        test = None
        if rec is not None and rec.utterances_path.exists():
            test = lambda: self.resummarize(rec)  # noqa: E731
        return self.dialogs.edit_summary_prompts(test=test)

    # -- updates ----------------------------------------------------------
    def busy_reason(self) -> str:
        """Why an update must wait ("" = it may run now)."""
        if self.controller.is_recording:
            return "Es läuft gerade eine Aufnahme."
        snap = self.queue.snapshot() if hasattr(self.queue, "snapshot") else None
        if snap is not None and (snap.current is not None or snap.pending):
            return "Es wird gerade eine Aufnahme verarbeitet."
        return ""

    def updater(self) -> Any:
        from callrec.updater import Updater

        return Updater(self.cfg.update.repo_url, self.cfg.update.branch)

    def check_updates(self, quiet: bool = False, done: Callable[[Any], None] | None = None) -> None:
        """ "Nach Updates suchen": look up the release (worker thread), then show the dialog.
        ``quiet`` (daily check): only a notification when a new version is available."""

        def worker() -> None:
            try:
                res: Any = self.updater().check()
            except Exception as exc:  # UpdateError (git missing, VPN, no release) or unexpected
                res = exc
            if done is not None:
                done(res)
            if quiet:
                if not isinstance(res, Exception) and res.available and res.release is not None:
                    self.ui.notify("Update verfügbar", f"CallRecorder {res.release.version} – im Hauptfenster: Hilfe → "
                                   "Nach Updates suchen …")
                return
            self.ui.post(lambda: self._show_update(res))

        threading.Thread(target=worker, name="update-check", daemon=True).start()

    def _show_update(self, res: Any) -> None:
        from callrec.updater import read_install_info

        info = read_install_info()
        choice = self.dialogs.ask_update(res, self.busy_reason(), info)
        if choice in ("now", "after") and not isinstance(res, Exception):
            self.start_update(res, "update", wait=choice == "after")
        elif choice == "restore":
            self.restore_previous_version()

    def start_update(self, res: Any, mode: str = "update", wait: bool = False) -> bool:
        """Only ever after a click. Refused while recording/processing (``wait``: run when done)."""
        from callrec.updater import UpdateError, helper_plan, launch_helper

        busy = self.busy_reason()
        if busy:
            if not wait:
                self.ui.notify("Update später", f"{busy} Das Update ist erst danach möglich.")
                return False
            self.ui.notify("Update vorgemerkt", f"{busy} CallRecorder aktualisiert sich, sobald das abgeschlossen ist.")
            self._pending_update = (res, mode)
            self._after(15000, self._pending_update_tick)
            return False
        try:
            if mode == "update":
                if self.updater().local_changes():
                    raise UpdateError("Im Programmordner gibt es lokale Änderungen an Programmdateien – Update abgelehnt. "
                                      "Bitte install-callrecorder.ps1 erneut ausführen oder die Änderungen entfernen.")
                plan = helper_plan(res, res.release.commit, res.release.version)
            else:
                plan = helper_plan(res, res.target, res.target_version, mode="restore")
            launch_helper(plan)
        except Exception as exc:
            log.exception("Update nicht gestartet")
            self.ui.notify("Update nicht möglich", str(exc))
            return False
        self.ui.notify("Update", "CallRecorder wird für das Update beendet und danach neu gestartet.")
        self._after(500, self.quit)
        return True

    _pending_update: tuple[Any, str] | None = None

    def _pending_update_tick(self) -> None:
        pending = self._pending_update
        if pending is None:
            return
        if self.busy_reason():
            self._after(15000, self._pending_update_tick)
            return
        self._pending_update = None
        self.start_update(pending[0], pending[1])

    def restore_previous_version(self) -> bool:
        from callrec.updater import read_install_info, restore_check

        info = read_install_info()
        prev = str(info.get("previous_commit") or "")
        try:
            res = restore_check(self.updater(), prev, str(info.get("previous_version") or ""))
        except Exception as exc:
            self.ui.notify("Wiederherstellen nicht möglich", str(exc))
            return False
        if not self.dialogs.ask_yes_no("Vorherige Version wiederherstellen",
                                       f"Zurück auf CallRecorder {res.target_version} ({prev[:10]})? CallRecorder wird "
                                       "dafür beendet und neu gestartet."):
            return False
        return self.start_update(res, "restore", wait=True)

    def show_update_result(self) -> None:
        """At the start: the result of an update that ran while the app was closed."""
        from callrec.updater import take_status

        st = take_status()
        if st is not None:
            self.ui.notify("Update abgeschlossen" if st.get("ok") else "Update fehlgeschlagen", str(st.get("message", "")))

    def manage_profiles(self) -> None:
        from callrec.review import rebuild_profile

        self.dialogs.manage_profiles(self.profiles,
                                     rebuild=lambda pid: rebuild_profile(self.profiles, pid, self.cfg.recordings_path()),
                                     recordings_root=self.cfg.recordings_path(),
                                     reidentify=lambda: self.reidentify_all(), repair=self.repair_profiles)

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

    def remove(self, rec: Recording, scope: str, permanent: bool = True) -> str:
        """Manual "Nur Audio löschen" / "Audio und Transkript löschen" / "vollständig löschen" (after the
        confirmation in the UI) - the same path as the automatic deletion."""
        text = self.controller.remove(rec, scope, "permanent" if permanent else "recycle_bin")
        self._changed(None if scope == "recording" else rec)
        return text

    def set_pinned(self, rec: Recording, on: bool) -> None:
        from callrec.retention import set_pinned

        set_pinned(rec, on)
        self._changed(rec)

    # -- automatic deletion (retention) ------------------------------------------------
    def retention_plan(self) -> Any:
        from callrec import retention

        return retention.plan(self.cfg.recordings_path(), self.cfg, busy=self.retention_busy)

    def _retention_remove(self, rec: Recording, scope: str, mode: str) -> object:
        from callrec.library import LibraryError

        if self.retention_busy(rec):  # checked again right before: a dialog may have opened meanwhile
            raise LibraryError("Die Aufnahme wird gerade bearbeitet - wird später gelöscht.")
        return self.controller.remove(rec, scope, mode, by="retention",
                                      reason=f"älter als {self.cfg.retention.max_age_days} Tage")

    def run_retention(self, confirmed: bool = False, done: Callable[[Any], None] | None = None,
                      only: Any = None, expect: Any = None) -> None:
        """At the start and then daily (worker thread): remove what is due; a run that must ask (first
        activation, clock looks wrong) asks on the UI thread instead of deleting."""
        from callrec import retention

        def worker() -> None:
            rt = self.cfg.retention
            try:
                res = retention.run(self.cfg.recordings_path(), self.cfg, self._retention_remove,
                                    busy=self.retention_busy, confirmed=confirmed, only=only, expect=expect)
                plan = self.retention_plan()
            except Exception:
                log.exception("Automatisches Löschen fehlgeschlagen")
                return
            if res.removed:
                self.ui.notify("Alte Aufnahmen gelöscht", f"Älter als {rt.max_age_days} Tage – "
                               f"{retention.removed_summary(res.scopes)}.")
                self.ui.post(lambda: self._changed(None))
            if res.blocked == "changed":  # the answer was given to other settings: asked again at the next run
                self.ui.notify("Automatisches Löschen", res.blocked_text)
            elif res.blocked:
                self.ui.post(lambda: self.ask_retention(res.blocked, res.blocked_text))
            else:
                text = retention.notify_due_today(plan)
                if text:
                    self.ui.notify("Automatisches Löschen", text + " – „Behalten“ verhindert das.")
                    soon = retention.Plan(plan.enabled, plan.scope, upcoming=plan.next_24h(datetime_now()))
                    retention.mark_surfaced(soon)  # the notification showed these
            if done is not None:
                done(res)

        threading.Thread(target=worker, name="retention", daemon=True).start()

    def schedule_retention(self, first_delay_ms: int = 20000) -> None:
        def tick() -> None:
            if self.cfg.retention.enabled:
                self.run_retention()
            else:  # switched off (also in config.toml): switched on again, the first deletion is asked again
                from callrec import retention

                retention.revoke_acknowledgement()
            self._after(24 * 3600 * 1000, tick)

        self._after(first_delay_ms, tick)

    def ask_retention(self, kind: str, text: str) -> str:
        """The run did not delete: first activation with old recordings, or the clock looks wrong."""
        from callrec import retention

        expect = retention.settings_key(self.cfg, self.cfg.recordings_path())  # what the question describes
        plan = self.retention_plan()
        if not plan.removable or kind == "future":
            if kind == "future":
                self.ui.notify("Automatisches Löschen ausgesetzt", text)
            return ""
        choice = self.dialogs.ask_retention(kind, text, [(i.title, retention.item_note(i, self.cfg.retention))
                                                         for i in plan.removable], self.cfg.retention)
        if choice == "delete":  # exactly the recordings the question listed
            self.run_retention(confirmed=True, only=[i.id for i in plan.removable], expect=expect)
        elif choice == "keep":
            # not an answer to the first deletion: what becomes due later is asked about first, too
            retention.pin_all(plan.removable)
            self._changed(None)
        elif choice == "off":
            base = load_config(preset="")
            base.retention.enabled = False
            save_config(base)
            self.apply_saved_config()
            # not acknowledged: switched on again later, the first deletion is asked again (with the list)
        return choice

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

    def end_session(self) -> bool:
        """Windows shuts down / logs off: finish a running recording through the normal stop path
        (reason "system_shutdown"; the question about processing comes at the next start). True = a
        recording was stopped."""
        if self.autostop is not None:
            try:
                self.autostop.shutdown()
            except Exception:
                log.exception("Auto-Stopp nicht beendet")
        if not self.controller.is_recording:
            return False
        return self.controller.stop("system_shutdown", by="system") is not None

    def quit(self) -> None:
        self.quit_handler()

    def show_window(self) -> None:
        self.show_window_handler()
