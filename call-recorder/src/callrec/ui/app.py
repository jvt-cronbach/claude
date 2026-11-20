"""Wires config, controller, Tk UI, tray icon, hotkey and call detection together."""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import UTC, datetime
from typing import Any

from callrec import paths
from callrec.config import Config, load_config
from callrec.controller import Controller
from callrec.detect import CallDetector
from callrec.factory import build_calendar_service, build_pipeline, graph_auth, profile_store
from callrec.hotkey import GlobalHotkey, HotkeyError, parse_hotkey
from callrec.jobs import ProcessingQueue
from callrec.logging_setup import setup_logging
from callrec.models import Person
from callrec.recording import recordings_to_resume

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
    from callrec.ui.actions import AppActions
    from callrec.ui.mainwindow import MainWindow
    from callrec.ui.tkui import TkUi
    from callrec.ui.tray import TrayIcon

    enable_dpi_awareness()
    profiles = profile_store()
    calendar = build_calendar_service(cfg)
    me_provider = MeProvider(cfg)
    ui = TkUi(calendar, show_overlay=cfg.ui.show_overlay)

    def pipeline_for(preset: str | None = None) -> Any:
        # one-off preset (re-transcription from the main window): overlay it on config.toml
        return build_pipeline(load_config(preset=preset) if preset else cfg, profiles)

    queue = ProcessingQueue(pipeline_for, status=ui.set_status)
    controller = Controller(
        cfg, ui, lambda: WasapiBackend(cfg.detection.teams_processes), calendar, queue, me_provider
    )
    ui.on_stop_clicked = lambda: ui.post(controller.stop)
    from callrec.consent import active_summary_provider

    ui.summary_provider = lambda: active_summary_provider(cfg.summary)
    actions = AppActions(cfg, ui, controller, queue, profiles, me_provider, log_file)
    ui.review_handler = actions.review
    window = MainWindow(ui.root, actions)
    ui.notify_listeners.append(window.notify)
    actions.show_window_handler = window.show

    stop_event = threading.Event()
    hotkey: GlobalHotkey | None = None

    def quit_app() -> None:
        if controller.is_recording:
            controller.stop()
        from callrec.asr.transcriber import cancel_running_workers

        cancel_running_workers()  # do not leave a Whisper worker / download running after exit
        stop_event.set()
        if hotkey:
            hotkey.stop()
        window.save_state()
        tray.stop()
        ui.root.quit()

    actions.quit_handler = quit_app
    tray_actions: dict[str, Any] = {
        "show_window": window.show,
        "toggle": actions.toggle,
        "handoff_last": lambda: actions.handoff(None),
        "cancel_processing": lambda: actions.cancel_processing(None),
        "settings": actions.settings,
        "quit": quit_app,
    }
    tray = TrayIcon(tray_actions, ui.post)
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
    if cfg.ui.start_minimized:
        ui.notify("CallRecorder bereit", f"Fenster: Klick auf das Symbol. Start/Stopp: {cfg.hotkey.toggle}")
    else:
        ui.root.after(50, window.show)
    ui.mainloop()
    queue.stop()
    return 0


def enable_dpi_awareness() -> None:
    """Sharp text at 125-150 % scaling. Must run before the first Tk window exists.

    Per-monitor v2 would need Tk to rescale on WM_DPICHANGED, which Tk 8.6 does not do; the
    system-aware mode makes Windows scale the whole window bitmap only when it is moved to a
    monitor with another scaling."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]  # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:
            log.debug("DPI-Awareness nicht setzbar", exc_info=True)
