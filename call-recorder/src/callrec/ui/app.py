"""Wires config, controller, Tk UI, tray icon, hotkey and call detection together."""

from __future__ import annotations

import functools
import json
import logging
import sys
import threading
import time
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
            client = GraphClient(auth.token_silent, timeout=self.cfg.calendar.request_timeout_s)
            raw = client.get_me_raw()
        except Exception as exc:
            log.info("/me nicht abrufbar: %s", exc)
            return None
        me = Person(name=str(raw.get("displayName") or ""),
                    email=str(raw.get("mail") or raw.get("userPrincipalName") or ""), role="self")
        if me.name:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            self.cache.write_text(json.dumps({**me.to_dict(), "given_name": str(raw.get("givenName") or "")},
                                             ensure_ascii=False), encoding="utf-8")
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


UPDATE_CHECK_INTERVAL_S = 24 * 3600


def schedule_daily_update_check(cfg: Config, root: Any, actions: Any) -> None:
    """Once a day (first time 5 minutes after the start) look for a release and only notify."""
    state = paths.data_dir() / "update_check.json"

    def due() -> bool:
        try:
            last = float(json.loads(state.read_text(encoding="utf-8")).get("at", 0))
        except (OSError, ValueError, AttributeError):
            last = 0.0
        return time.time() - last >= UPDATE_CHECK_INTERVAL_S

    def tick() -> None:
        if cfg.update.check_daily and due():
            try:
                state.parent.mkdir(parents=True, exist_ok=True)
                state.write_text(json.dumps({"at": time.time()}), encoding="utf-8")
            except OSError:
                pass
            actions.check_updates(quiet=True)
        root.after(3600 * 1000, tick)

    root.after(5 * 60 * 1000, tick)


def run_tray_app(cfg: Config | None = None, start_minimized: bool = False) -> int:
    log_file = setup_logging(console=sys.stderr is not None)
    if not acquire_single_instance():
        log.error("CallRecorder läuft bereits")
        return 1
    cfg = cfg or load_config()
    from callrec.updater import version_text

    log.info("Start: %s, Python %s", version_text(), sys.version.split()[0])
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
    from callrec.ui.startflow import consent_mode

    ui.consent_mode = lambda: consent_mode(cfg)
    ui.me_provider = me_provider
    actions = AppActions(cfg, ui, controller, queue, profiles, me_provider, log_file)
    ui.review_handler = actions.review
    window = MainWindow(ui.root, actions)
    ui.notify_listeners.append(window.notify)
    actions.show_window_handler = window.show
    ui.root.after(3000, actions.show_update_result)
    schedule_daily_update_check(cfg, ui.root, actions)

    # automatic end of a recording (its own thread; stops go through controller.stop on the Tk thread)
    from callrec import autostop as autostop_mod

    probe = autostop_mod.windows_probe(lambda: cfg.detection.teams_processes) if sys.platform == "win32" else None
    autostop = autostop_mod.AutoStopRunner(
        lambda: cfg.autostop, controller, ui.post, ui.notify, probe=probe,
        thread_context=autostop_mod.windows_thread_context if sys.platform == "win32" else autostop_mod._no_context,
    )

    def autostop_attention() -> None:
        # the overlay shows the countdown; without it, bring up the window (without taking the focus)
        if not ui.show_overlay:
            ui.post(lambda: window.show(activate=False))

    autostop.attention = autostop_attention
    controller.stop_info = autostop.info
    ui.autostop = autostop
    actions.autostop = autostop
    autostop.start()

    stop_event = threading.Event()
    hotkey: GlobalHotkey | None = None

    def quit_app() -> None:
        autostop.shutdown()  # no new automatic stop while exiting
        if controller.is_recording:
            controller.stop("app_quit")
        # do not leave a Whisper worker / download running after exit; the interrupted job is
        # resumed at the next start
        queue.shutdown()
        from callrec.cli import cancel_model_downloads

        cancel_model_downloads()  # "Jetzt herunterladen" from the setup tab
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

    # "Mit Windows starten": repair an entry that points at a moved/old installation
    try:
        msg = actions.autostart.repair_if_stale()
        if msg:
            ui.notify("Autostart", msg)
    except Exception:
        log.exception("Autostart-Eintrag nicht prüfbar")

    # after a stop: the question "jetzt vollständig verarbeiten?" (or mode "full")
    controller.after_stop = actions.after_recording
    # crash recovery / restart: continue chains, ask for undecided recordings, finish old processing
    from callrec.postcall import startup_work

    for rec, kind in startup_work(cfg.recordings_path()):
        if kind == "resume":
            log.info("Setze Verarbeitungskette fort: %s", rec.folder.name)
            actions.chain.resume(rec)
        elif kind == "ask":
            ui.notify("Aufnahme noch nicht verarbeitet", rec.folder.name)
            ui.post(functools.partial(actions.ask_after_recording, rec))
        else:
            log.info("Setze Verarbeitung fort: %s", rec.folder.name)
            controller.process(rec)

    if cfg.detection.enabled and sys.platform == "win32":
        threading.Thread(target=detection_loop, args=(cfg, controller, ui, stop_event), name="detect", daemon=True).start()

    def health() -> None:
        controller.tick()
        ui.root.after(2000, health)

    ui.root.after(2000, health)
    if cfg.ui.start_minimized or start_minimized:
        ui.notify("CallRecorder bereit", f"Fenster: Klick auf das Symbol. Start/Stopp: {cfg.hotkey.toggle}")
    else:
        ui.root.after(50, window.show)
    ui.mainloop()
    queue.stop()
    queue.join(timeout=10)  # let a cancelled job record its state before the process ends
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
