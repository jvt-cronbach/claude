"""Wires config, controller, Tk UI, tray icon, hotkey and call detection together."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import UTC, datetime
from typing import Any

from callrec import paths
from callrec.config import Config, load_config, save_config
from callrec.controller import Controller
from callrec.detect import CallDetector
from callrec.factory import (
    build_calendar_service,
    build_pipeline,
    graph_auth,
    perform_handoff,
    profile_store,
    reload_config_in_place,
)
from callrec.hotkey import GlobalHotkey, HotkeyError, parse_hotkey
from callrec.jobs import ProcessingQueue
from callrec.logging_setup import setup_logging
from callrec.models import Person
from callrec.recording import Recording, list_recordings, recordings_to_resume

log = logging.getLogger(__name__)
_MUTEX_HANDLE: Any = None


def acquire_single_instance() -> bool:
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes

    global _MUTEX_HANDLE
    # use_last_error: ctypes saves GetLastError() right after the call; calling
    # kernel32.GetLastError() separately may see a value clobbered in between.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _MUTEX_HANDLE = kernel32.CreateMutexW(None, False, "Local\\CallRecorderSingleInstance")
    return ctypes.get_last_error() != 183  # type: ignore[attr-defined]  # ERROR_ALREADY_EXISTS


def open_path(path: Any) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
    else:
        log.info("Öffnen: %s", path)


class MeProvider:
    """User's own name: config > cached Graph /me > Graph /me > 'Ich'."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.cache = paths.data_dir() / "me.json"

    def __call__(self) -> Person:
        g = self.cfg.general
        if g.user_name.strip():
            return Person(name=g.user_name.strip(), email=g.user_email.strip(), role="self")
        try:
            d = json.loads(self.cache.read_text(encoding="utf-8"))
            return Person(name=d["name"], email=d.get("email", ""), role="self")
        except (OSError, ValueError, KeyError):
            pass
        return self.refresh() or Person(name="Ich", role="self")

    def refresh(self) -> Person | None:
        if not self.cfg.calendar.client_id.strip():
            return None
        try:
            from callrec.calendar.graph import GraphClient

            auth = graph_auth(self.cfg)
            me = GraphClient(auth.token_silent, timeout=self.cfg.calendar.request_timeout_s).get_me()
        except Exception as exc:
            log.info("/me nicht abrufbar: %s", exc)
            return None
        if me.name:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            self.cache.write_text(json.dumps(me.to_dict(), ensure_ascii=False), encoding="utf-8")
        return me


def detection_loop(cfg: Config, controller: Controller, ui: Any, stop: threading.Event) -> None:
    """Poll Teams' capture sessions (and the calendar) and ask - never auto-start."""
    from datetime import timedelta

    from callrec.audio.wasapi import E_CAPTURE, com_initialized, endpoints_with_active_session

    detector = CallDetector(cooldown=timedelta(minutes=cfg.detection.cooldown_minutes))
    events: list[Any] = []
    last_calendar = 0.0
    import time

    with com_initialized():
        while not stop.wait(cfg.detection.poll_interval_s):
            try:
                active = bool(endpoints_with_active_session(E_CAPTURE, set(cfg.detection.teams_processes)))
            except Exception:
                log.debug("Sitzungsabfrage fehlgeschlagen", exc_info=True)
                active = False
            if cfg.detection.calendar_reminder and controller.calendar.source is not None and time.monotonic() - last_calendar > 300:
                last_calendar = time.monotonic()
                try:
                    events = controller.calendar.upcoming_online_meetings()
                except Exception:
                    log.debug("Kalenderabfrage fehlgeschlagen", exc_info=True)
            prompt = detector.update(datetime.now(UTC), active, controller.is_recording, events)
            if prompt is not None:
                log.info("Erinnerung: %s", prompt.reason)
                ui.post(lambda p=prompt: controller.on_reminder(p))


def run_tray_app(cfg: Config | None = None) -> int:
    log_file = setup_logging(console=sys.stderr is not None)
    if not acquire_single_instance():
        log.error("CallRecorder läuft bereits")
        return 1
    cfg = cfg or load_config()
    from callrec.audio.wasapi import WasapiBackend
    from callrec.ui import dialogs
    from callrec.ui.tkui import TkUi
    from callrec.ui.tray import TrayIcon

    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        except Exception:
            pass

    profiles = profile_store()
    calendar = build_calendar_service(cfg)
    me_provider = MeProvider(cfg)
    ui = TkUi(calendar, show_overlay=cfg.ui.show_overlay)
    queue = ProcessingQueue(lambda: build_pipeline(cfg, profiles), status=ui.set_status)
    controller = Controller(
        cfg, ui, lambda: WasapiBackend(cfg.detection.teams_processes), calendar, queue, me_provider
    )
    ui.on_stop_clicked = lambda: ui.post(controller.stop)

    def last_recording() -> Recording | None:
        """Most recent recording that is not currently being recorded."""
        current = controller.current.folder if controller.current is not None else None
        recs = [r for r in list_recordings(cfg.recordings_path()) if r.folder != current]
        return recs[0] if recs else None

    def review(rec: Recording | None = None) -> None:
        rec = rec or last_recording()
        if rec is None:
            ui.notify("Keine Aufnahme", "Es gibt noch keine abgeschlossene Aufnahme.")
            return
        if not rec.speakers_path.exists():
            ui.notify("Noch nicht verarbeitet", "Die Aufnahme wird noch verarbeitet.")
            return
        pipeline = build_pipeline(cfg, profiles)
        dialogs.review_speakers(
            ui.root, rec, profiles,
            rerender=lambda: pipeline.run(rec, {"render"}),
            resummarize=lambda: controller.process(rec, frozenset({"summarize"})),
            handoff=lambda: handoff(rec),
        )

    ui.review_handler = review

    def handoff(rec: Recording | None = None) -> None:
        """Explicit user action: preview + confirm, then hand over the summary only."""
        rec = rec or last_recording()
        if rec is None:
            ui.notify("Keine Aufnahme", "Keine abgeschlossene Aufnahme gefunden.")
            return
        dialogs.ask_handoff(ui.root, rec, cfg, lambda sha: perform_handoff(cfg, rec, sha))

    def with_last(steps: frozenset[str]) -> None:
        rec = last_recording()
        if rec is None:
            ui.notify("Keine Aufnahme", "Keine abgeschlossene Aufnahme gefunden.")
            return
        controller.process(rec, steps)
        ui.notify("Verarbeitung gestartet", rec.folder.name)

    def settings() -> None:
        mics: list[str] = []
        outs: list[str] = []
        try:
            b = WasapiBackend()
            mics = [d.name for d in b.capture_devices()]
            outs = b.render_device_names()
            b.close()
        except Exception:
            log.exception("Geräteliste nicht verfügbar")
        from callrec.ui.settings import edit_settings

        base = load_config(preset="")  # edit config.toml itself, not the preset overlay
        if edit_settings(ui.root, base, mics, outs, save_config):
            try:
                reload_config_in_place(cfg)
            except Exception as exc:
                ui.notify("Konfiguration", str(exc))

    def login() -> None:
        if not cfg.calendar.client_id.strip():
            ui.notify("Keine Client-ID", f"Bitte calendar.client_id in {paths.config_file()} eintragen (siehe README).")
            return

        def worker() -> None:
            try:
                graph_auth(cfg).login_interactive()
                me = me_provider.refresh()
                ui.notify("Angemeldet", f"Angemeldet als {me.display if me else 'unbekannt'}.")
            except Exception as exc:
                ui.notify("Anmeldung fehlgeschlagen", str(exc))

        threading.Thread(target=worker, name="login", daemon=True).start()

    stop_event = threading.Event()
    hotkey: GlobalHotkey | None = None

    def quit_app() -> None:
        if controller.is_recording:
            controller.stop()
        stop_event.set()
        if hotkey:
            hotkey.stop()
        tray.stop()
        ui.root.quit()

    actions = {
        "toggle": controller.toggle,
        "review_last": lambda: review(None),
        "resummarize_last": lambda: with_last(frozenset({"summarize"})),
        "retranscribe_last": lambda: with_last(frozenset({"transcribe", "identify", "render", "summarize"})),
        "handoff_last": lambda: handoff(None),
        "reprocess_last": lambda: with_last(frozenset({"finalize", "transcribe", "diarize", "identify", "render", "summarize"})),
        "open_folder": lambda: open_path(cfg.recordings_path()),
        "profiles": lambda: dialogs.manage_profiles(ui.root, profiles),
        "settings": settings,
        "login": login,
        "open_log": lambda: open_path(log_file),
        "quit": quit_app,
    }
    tray = TrayIcon(actions, ui.post)
    ui.tray = tray
    tray.start()

    try:
        hotkey = GlobalHotkey(parse_hotkey(cfg.hotkey.toggle), lambda: ui.post(controller.toggle))
        if not hotkey.start():
            ui.notify("Tastenkürzel", hotkey.error)
    except HotkeyError as exc:
        ui.notify("Tastenkürzel ungültig", str(exc))

    for w in cfg.warnings:
        ui.notify("Konfiguration", w)

    # crash recovery: finish recordings that were interrupted
    for rec, interrupted in recordings_to_resume(cfg.recordings_path()):
        if interrupted:
            ui.notify("Unterbrochene Aufnahme", f"{rec.folder.name} wird wiederhergestellt und verarbeitet.")
        else:
            log.info("Setze Verarbeitung fort: %s", rec.folder.name)
        controller.process(rec)

    if cfg.detection.enabled and sys.platform == "win32":
        threading.Thread(target=detection_loop, args=(cfg, controller, ui, stop_event), name="detect", daemon=True).start()

    def health() -> None:
        controller.tick()
        ui.root.after(2000, health)

    ui.root.after(2000, health)
    ui.notify("CallRecorder bereit", f"Start/Stopp: {cfg.hotkey.toggle}")
    ui.mainloop()
    queue.stop()
    return 0
