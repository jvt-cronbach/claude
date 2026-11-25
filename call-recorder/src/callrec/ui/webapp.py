"""Start of the web UI (preview): pywebview + Edge WebView2 on Windows.

    main thread  : pywebview (webview.start) - main window, recording indicator, call-detected prompt
    app thread   : AppLoop - controller, AppActions, processing chain, dialogs (was the Tk thread)
    other threads: processing queue, auto-stop, tray (pystray), hotkey, detection, ui-events emitter,
                   loopback UI server, pywebview's js_api call threads

``run_web_app`` returns ``FALLBACK`` (and starts nothing) when pywebview/WebView2 is not usable,
so the caller starts the classic Tk window instead. All wiring happens after the GUI is up (in
the ``func`` of ``webview.start``): a failing ``webview.start`` leaves no worker behind.
"""

from __future__ import annotations

import functools
import json
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from callrec import paths
from callrec.config import Config, load_config, save_config

log = logging.getLogger(__name__)

FALLBACK = 2
PAGE_TIMEOUT_S = 45.0


def preflight() -> str:
    """'' when the web UI can start here, else the reason (German) for the fallback notice."""
    try:
        import webview  # noqa: F401
    except Exception as exc:
        return f"pywebview ist nicht installiert ({exc})."
    if sys.platform == "win32":
        try:
            import importlib

            importlib.import_module("webview.guilib").initialize("edgechromium")
            import webview.platforms.winforms as wf
        except Exception as exc:
            return f"WebView2/.NET nicht verfügbar ({exc})."
        if getattr(wf, "renderer", "") != "edgechromium":
            return "Die Microsoft-Edge-WebView2-Laufzeit wurde nicht gefunden."
        return ""
    try:
        import importlib

        importlib.import_module("webview.guilib").initialize()
    except Exception as exc:
        return f"Keine Web-Oberfläche auf diesem System ({exc})."
    return ""


def window_background(cfg: Config) -> str:
    """Window colour before the page is painted (no white flash in dark mode)."""
    dark = cfg.ui.theme == "dark" or (cfg.ui.theme == "system" and windows_dark_mode())
    return "#141414" if dark else "#EBEBEB"


def windows_dark_mode() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,  # type: ignore[attr-defined]
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0  # type: ignore[attr-defined]
    except OSError:
        return False


class WebCore:
    """Everything the bridge needs, wired once the GUI runs."""

    def __init__(self, cfg: Config, log_file: Path | None) -> None:
        from callrec.ui.apploop import AppLoop
        from callrec.ui.webui import Events

        self.cfg = cfg
        self.log_file = log_file
        self.loop = AppLoop()
        self.events = Events()
        self.lock = threading.Lock()
        self.page_ready = threading.Event()
        self.event_cache: list[Any] = []
        self.pending_route = ""
        self.setup_summary: dict[str, Any] = {"ready": True, "text": "", "open": 0}
        self._audio_item: Any = None
        self._summary_test: tuple[str, bool] | None = None
        self._download_text = ""
        self._bench_running = False
        self.windows: Any = None
        self.server: Any = None
        self.restart_args: list[str] | None = None
        self.stop_event = threading.Event()
        self.hotkey: Any = None
        self.tray: Any = None
        self.show_requests: Any = None
        # set in wire()
        self.ui: Any = None
        self.controller: Any = None
        self.queue: Any = None
        self.actions: Any = None
        self.profiles: Any = None
        self.index: Any = None
        self.samples: Any = None
        self.me_provider: Any = None
        self.autostop: Any = None

    # ------------------------------------------------------------------ wiring
    def wire(self) -> None:
        from callrec.audio.wasapi import WasapiBackend
        from callrec.consent import active_summary_provider
        from callrec.controller import Controller
        from callrec.factory import build_calendar_service, build_pipeline, profile_store
        from callrec.jobs import ProcessingQueue
        from callrec.library import RecordingIndex
        from callrec.ui.actions import AppActions
        from callrec.ui.app import MeProvider
        from callrec.ui.startflow import consent_mode
        from callrec.ui.webdialogs import SampleClips, WebDialogPort
        from callrec.ui.webui import WebUi

        cfg = self.cfg
        self.profiles = profile_store()
        calendar = build_calendar_service(cfg)
        self.me_provider = MeProvider(cfg)
        ui = WebUi(self.loop, self.events, calendar)
        ui.windows = self.windows
        ui.show_overlay = cfg.ui.show_overlay
        self.ui = ui

        def pipeline_for(preset: str | None = None) -> Any:
            return build_pipeline(load_config(preset=preset) if preset else cfg, self.profiles)

        self.queue = ProcessingQueue(pipeline_for, status=ui.set_status)
        self.controller = Controller(cfg, ui, lambda: WasapiBackend(cfg.detection.teams_processes), calendar,
                                     self.queue, self.me_provider)
        ui.summary_provider = lambda: active_summary_provider(cfg.summary)
        ui.consent_mode = lambda: consent_mode(cfg)
        ui.me_provider = self.me_provider
        self.actions = AppActions(cfg, ui, self.controller, self.queue, self.profiles, self.me_provider, self.log_file)
        self.samples = (SampleClips(self.server.publish, self.server.unpublish) if self.server is not None
                        else SampleClips(lambda p: ""))
        self.actions.dialogs = WebDialogPort(ui.broker, self.navigate, ui.notify, self.samples)
        ui.review_handler = self.actions.review
        self.actions.show_window_handler = lambda: self.windows.show_main(activate=True)
        self.index = RecordingIndex(cfg.recordings_path)
        self.actions.on_changed.append(self._on_changed)
        self.actions.on_config_changed.append(lambda: self.events.emit("config_changed", {}))
        self.queue.add_listener(lambda job: self.events.emit("job_done", {"rec": job.recording.folder.name,
                                                                          "state": job.state}))

    def _on_changed(self, rec: Any) -> None:
        if rec is not None:
            self.index.invalidate(rec.folder)
        else:
            self.index.invalidate(None)
        self.events.emit("recording_changed", {"id": rec.folder.name if rec is not None else ""})

    def consent_mode(self) -> str:
        from callrec.ui.startflow import consent_mode

        return consent_mode(self.cfg)

    def me_name(self) -> str:
        try:
            return str(self.me_provider().display) if self.me_provider is not None else ""
        except Exception:
            return ""

    def navigate(self, route: str, params: dict[str, Any]) -> None:
        self.pending_route = route + ("/" + str(params.get("page")) if params.get("page") else "")
        self.windows.show_main(activate=True)
        self.events.emit("navigate", {"route": route, **params})

    def file_dialog(self, kind: str, file_types: tuple[str, ...]) -> Any:
        import webview

        win = self.windows.main
        if kind == "folder":
            return win.create_file_dialog(webview.FileDialog.FOLDER)
        return win.create_file_dialog(webview.FileDialog.OPEN, file_types=file_types)

    # ------------------------------------------------------------------ setup checklist
    def run_checks(self, force_audio: bool = False) -> dict[str, Any]:
        from callrec import setupcheck
        from callrec.ui.dto import checks

        recording = self.controller.is_recording
        resolve: Any = "real"
        previous = self._audio_item
        if recording or (previous is not None and not force_audio):
            resolve = None
        items = setupcheck.run_all(self.cfg, self._summary_test, resolve=resolve, autostart=self.actions.autostart)
        if resolve is None:
            items[0] = previous or setupcheck.CheckItem(
                "audio", "Audiogeräte (Mikrofon und Gegenseite)", setupcheck.UNKNOWN,
                "Wird während einer Aufnahme nicht geprüft – danach „Erneut prüfen“.")
        elif items and items[0].key == "audio" and items[0].state != setupcheck.UNKNOWN:
            self._audio_item = items[0]
        res = checks(items, self._download_text)
        self.setup_summary = {"ready": res["ready"], "text": res["summary"], "open": res["open"]}
        self.events.emit("checks", res)
        return res

    def setup_action(self, key: str) -> dict[str, Any]:
        from callrec import setupcheck

        a = self.actions
        if key == setupcheck.A_TEST_SUMMARY:
            def work() -> None:
                self.events.emit("checks_busy", {"text": "Verbindung zum Sprachmodell wird getestet … (Claude: "
                                                         "höchstens ca. 20 s)"})
                self._summary_test = setupcheck.probe_summary_provider(self.cfg)
                self.run_checks()
            threading.Thread(target=work, name="summary-test", daemon=True).start()
        elif key == setupcheck.A_DOWNLOAD_WHISPER:
            self._download_text = "Download startet …"

            def report(text: str) -> None:
                self._download_text = text
                self.events.emit("download", {"text": text})

            def done(ok: bool, text: str) -> None:
                self._download_text = "" if ok else text
                self.ui.notify("Whisper-Modell geladen" if ok else "Modell-Download fehlgeschlagen", text)
                self.run_checks()

            a.download_whisper(report, done)
        elif key == setupcheck.A_LOGIN:
            self.loop.post(lambda: a.login(done=lambda text: self.run_checks()))
        elif key == setupcheck.A_README:
            self.loop.post(a.open_readme)
        elif key == setupcheck.A_RECHECK:
            threading.Thread(target=lambda: self.run_checks(True), name="checks", daemon=True).start()
        elif key == setupcheck.A_UPDATES:
            self.loop.post(lambda: a.check_updates())
        elif key in (setupcheck.A_AUTOSTART_ON, setupcheck.A_AUTOSTART_OFF):
            self.loop.call(lambda: a.set_autostart(key == setupcheck.A_AUTOSTART_ON))
            threading.Thread(target=self.run_checks, name="checks", daemon=True).start()
        elif key.startswith("settings:"):
            from callrec.ui.settingsmodel import PAGE_BY_TAB

            return {"navigate": "settings", "page": PAGE_BY_TAB.get(key.split(":", 1)[1], "general")}
        else:
            return {"error": "Unbekannte Aktion"}
        return {"ok": True}

    # ------------------------------------------------------------------ settings
    def settings_data(self) -> dict[str, Any]:
        from callrec import apikey
        from callrec.config import list_presets
        from callrec.llm_anthropic import DEFAULT_MODEL, MODEL_CHOICES
        from callrec.ui.settingsmodel import schema

        base = load_config(preset="")
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
        st = self.actions.autostart.status() if self.actions.autostart is not None else None
        key, source = apikey.load_key()
        return {
            "pages": schema(base), "mics": mics, "outs": outs, "presets": list_presets(),
            "autostart": {"state": st.state if st else "unavailable", "detail": st.detail if st else
                          "Nur unter Windows verfügbar.", "on": bool(st and st.state in ("on", "stale"))},
            "apikey": {"present": bool(key), "masked": apikey.mask(key) if key else "", "source": source},
            "anthropic_models": [[m, d + (" – Standard" if m == DEFAULT_MODEL else "")] for m, d in MODEL_CHOICES.items()],
            "remote_allowed": bool(base.summary.allow_remote_endpoint_data_leaves_this_computer),
            "paths": {"presets": str(paths.presets_dir()), "prompts": str(paths.prompts_dir())},
            "speed": self.speed_info(base),
        }

    def speed_info(self, cfg: Config | None = None) -> dict[str, Any]:
        from callrec.asr.benchmark import speed_overview

        try:
            return {**speed_overview(cfg or load_config(preset="")), "running": self._bench_running}
        except Exception as exc:  # noqa: BLE001 - a settings page must open anyway
            log.warning("Geschwindigkeitsübersicht nicht verfügbar: %s", exc)
            return {"plan": "", "lines": [], "power": "", "measured": False, "running": self._bench_running}

    def start_benchmark(self) -> dict[str, Any]:
        """"Geschwindigkeit messen": runs in a thread, progress as ``bench`` events."""
        from callrec.asr.benchmark import busy_reason, measure_now

        if self._bench_running:
            return {"error": "Die Messung läuft bereits."}
        reason = busy_reason() or ("Gerade wird aufgenommen – bitte danach messen." if self.controller.is_recording else "")
        if reason:
            return {"error": reason}
        self._bench_running = True
        emit = self.events.emit
        cfg = load_config(preset="")

        def work() -> None:
            try:
                measure_now(cfg, lambda f, m: emit("bench", {"status": m or "misst …", "fraction": f if f >= 0 else None}))
                emit("bench", {"status": "Messung abgeschlossen.", "fraction": 1.0, "done": True,
                               "speed": {**self.speed_info(cfg), "running": False}})
            except Exception as exc:  # noqa: BLE001 - shown on the page
                log.warning("Geschwindigkeitsmessung fehlgeschlagen", exc_info=True)
                emit("bench", {"status": str(exc), "fraction": None, "done": True, "error": True})
            finally:
                self._bench_running = False

        threading.Thread(target=work, name="benchmark", daemon=True).start()
        return {"ok": True}

    def save_settings(self, patch: dict[str, Any], confirmed: list[str], autostart: bool | None) -> dict[str, Any]:
        from callrec.ui import settingsmodel

        base = load_config(preset="")
        res = settingsmodel.save(base, patch, confirmed, save_config)
        if not res.get("ok"):
            return res
        if autostart is not None and self.actions.autostart is not None:
            from callrec.autostart import settings_change

            st = self.actions.autostart.status()
            change = settings_change(st.state, st.state in ("on", "stale"), autostart)
            if change:
                try:
                    self.actions.autostart.enable() if change == "enable" else self.actions.autostart.disable()
                except OSError as exc:
                    res = {"ok": True, "warning": f"Autostart konnte nicht geändert werden: {exc}"}
        self.loop.call(self.actions.apply_saved_config)
        self._summary_test = None
        return res

    def whisper_models(self, action: str, name: str, force_gpu: bool) -> dict[str, Any]:
        from callrec.asr.download import fmt_bytes
        from callrec.asr.gpu import describe_effective, gpu_status
        from callrec.asr.modelcache import installed_models, remove_model

        root = paths.models_dir() / "whisper"
        models = installed_models(root)
        if action == "remove":
            m = next((m for m in models if m.name == name), None)
            if m is None:
                return {"error": "Dieses Modell gibt es nicht."}
            remove_model(m, root)
            models = installed_models(root)
        gpu = gpu_status(force=force_gpu)
        return {"models": [{"name": m.name, "text": m.describe(), "size": fmt_bytes(m.size)} for m in models],
                "total": fmt_bytes(sum(m.size for m in models)), "root": str(root),
                "effective": describe_effective(self.cfg.transcription, gpu)}

    def prompts(self, action: str, texts: dict[str, Any], force: bool, rec: Any) -> dict[str, Any]:
        from callrec.ui.settings import PROMPT_TABS
        from callrec.userfiles import (
            PROMPT_FILES,
            PROMPT_PLACEHOLDERS,
            default_prompt,
            ensure_user_files,
            validate_prompt,
        )

        ensure_user_files()
        d = paths.prompts_dir()
        if action == "get":
            out = []
            for key, label in PROMPT_TABS:
                p = d / PROMPT_FILES[key][0]
                out.append({"key": key, "label": label, "text": p.read_text(encoding="utf-8") if p.exists()
                            else default_prompt(key), "placeholders": PROMPT_PLACEHOLDERS.get(key) or {},
                            "default": default_prompt(key)})
            return {"prompts": out, "dir": str(d)}
        if action == "validate":
            return {"problems": {k: validate_prompt(k, str(texts.get(k) or "")) for k, _l in PROMPT_TABS}}
        if action in ("save", "test"):
            problems = [f"{label}: {w}" for key, label in PROMPT_TABS
                        for w in validate_prompt(key, str(texts.get(key) or ""))]
            if problems and not force:
                return {"problems": problems}
            for key, _label in PROMPT_TABS:
                if key in texts:
                    (d / PROMPT_FILES[key][0]).write_text(str(texts[key]).rstrip() + "\n", encoding="utf-8")
            if action == "test" and rec is not None:
                self.loop.post(lambda: self.actions.resummarize(rec))
                return {"ok": True, "text": "Gespeichert – die Zusammenfassung der gewählten Aufnahme wird als neue "
                                            "Version erstellt."}
            return {"ok": True, "text": "Gespeichert. Gilt für künftige Zusammenfassungen."}
        return {"error": "Unbekannte Aktion"}

    def profiles_action(self, action: str, pid: str, index: Any) -> dict[str, Any]:
        from callrec.review import rebuild_profile
        from callrec.speakers.profiles import only_legacy_unconfirmed
        from callrec.ui.dto import profiles

        store = self.profiles
        text = ""
        if action == "remove_origin" and isinstance(index, int):
            store.remove_contribution(pid, index)
        elif action == "reset":
            left = store.reset_to_confirmed(pid)
            if left is None:
                text = "Das Profil enthielt nichts Bestätigtes und wurde gelöscht."
            elif only_legacy_unconfirmed(left):
                text = ("Das Profil stammt aus einer früheren Version (Herkunft der Einträge unbekannt) und bleibt "
                        "unverändert. „Neu aufbauen“ übernimmt nur die eigenen Zuordnungen aus den Aufnahmen; "
                        "„Löschen“ entfernt es ganz.")
        elif action == "rebuild":
            text = rebuild_profile(store, pid, self.cfg.recordings_path())
        elif action == "delete":
            store.delete(pid)
        elif action == "delete_all":
            store.delete_all()
        elif action in ("mode_never", "mode_always", "mode_normal"):
            store.set_mode(pid, {"mode_never": "never", "mode_always": "always", "mode_normal": ""}[action])
        elif action == "reidentify":
            n = self.loop.call(lambda: self.actions.reidentify_all(ask=False))
            text = f"{n} Aufnahme(n) werden mit den aktuellen Regeln neu erkannt (eigene Zuordnungen bleiben)."
        elif action == "repair":
            from callrec.speakers import repair

            rp = repair.plan(store, self.cfg.recordings_path())
            text = "\n".join(repair.apply(store, rp) if not rp.empty else rp.lines())
        elif action != "list":
            return {"error": "Unbekannte Aktion"}
        return {"profiles": profiles(store, self.cfg.recordings_path()), "text": text}

    # ------------------------------------------------------------------ window state, restart
    def save_window_state(self, state: dict[str, Any]) -> None:
        p = paths.data_dir() / "webwindow.json"
        keep = {k: state[k] for k in ("route", "filter", "list_width", "tab") if k in state and
                isinstance(state[k], (str, int))}
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
        except OSError:
            log.debug("Fensterzustand nicht gespeichert", exc_info=True)

    def restart_later(self, ui: str = "") -> None:
        """Quit, then start again (a short-lived helper waits until this process released the
        single-instance lock)."""
        self.restart_args = ["tray"] + (["--ui", ui] if ui else [])
        self.loop.post(self.actions.quit)


def _spawn_restart(args: list[str]) -> None:
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        cand = Path(exe).with_name("pythonw.exe")
        exe = str(cand) if cand.exists() else exe
    code = ("import subprocess, sys, time; time.sleep(4); "
            f"subprocess.Popen([sys.executable, '-m', 'callrec', *{args!r}])")
    flags = 0x00000008 if sys.platform == "win32" else 0  # DETACHED_PROCESS
    try:
        subprocess.Popen([exe, "-c", code], creationflags=flags, close_fds=True)  # noqa: S603
    except OSError:
        log.exception("Neustart nicht möglich")


def start_marker() -> Path:
    """Exists from the start of the web UI until its page has loaded once. Found at the next start, it
    means the last start ended before that (crash of WebView2/.NET in the GUI thread, killed, hung):
    the classic window is used instead of crashing at every start."""
    return paths.data_dir() / "webui-start.marker"


def clear_start_marker() -> None:
    """An explicit choice of the web UI (``--ui web``, settings saved with it) tries it again."""
    try:
        start_marker().unlink(missing_ok=True)
    except OSError:
        log.debug("Startmarke nicht entfernbar", exc_info=True)


def run_web_app(cfg: Config, log_file: Path | None, start_minimized: bool = False,
                notify_fallback: Callable[[str], None] = lambda reason: None) -> int:
    """Run the web UI; ``FALLBACK`` when it cannot start (nothing was started then)."""
    marker = start_marker()
    if marker.exists():
        reason = ("Sie ist beim letzten Start nicht fertig geladen worden (abgestürzt oder beendet). Erneut "
                  "versuchen: Einstellungen › Allgemein › Oberfläche speichern bzw. callrec tray --ui web.")
        log.warning("Neue Oberfläche nicht verwendet: %s", reason)
        notify_fallback(reason)
        return FALLBACK
    reason = preflight()
    if reason:
        log.warning("Neue Oberfläche nicht verfügbar: %s", reason)
        notify_fallback(reason)
        return FALLBACK
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    except OSError:
        log.debug("Startmarke nicht geschrieben", exc_info=True)
    import webview

    from callrec.ui.bridge import Bridge, CompanionApi
    from callrec.ui.webserver import UiServer
    from callrec.ui.webui import WebWindows

    core = WebCore(cfg, log_file)
    server = UiServer()
    server.start()
    core.server = server
    if sys.platform == "win32" and not harden_pywebview(server):
        server.stop()
        clear_start_marker()
        notify_fallback("Die Absicherung der Navigation in WebView2 ließ sich nicht einrichten.")
        return FALLBACK
    bg = window_background(cfg)
    try:
        st = json.loads((paths.data_dir() / "webwindow.json").read_text(encoding="utf-8"))
        core.pending_route = str(st.get("route") or "")
    except (OSError, ValueError):
        pass
    from callrec.updater import version_text

    main = webview.create_window(version_text(), url=server.url("index.html") + f"?theme={cfg.ui.theme}", js_api=Bridge(core), width=1280,
                                 height=820, min_size=(900, 600), hidden=True, background_color=bg,
                                 text_select=True)
    indicator = webview.create_window("CallRecorder – Aufnahme", url=server.url("indicator.html") + f"?theme={cfg.ui.theme}",
                                      js_api=CompanionApi(core, "indicator"), width=380, height=64, frameless=True,
                                      easy_drag=True, on_top=True, focus=False, hidden=True, resizable=False,
                                      background_color=bg, shadow=True)
    toast = webview.create_window("CallRecorder – Call erkannt", url=server.url("toast.html") + f"?theme={cfg.ui.theme}",
                                  js_api=CompanionApi(core, "toast"), width=400, height=252, frameless=True,
                                  easy_drag=False, on_top=True, focus=False, hidden=True, resizable=False,
                                  background_color=bg, shadow=True)
    assert main is not None
    windows = WebWindows(main, indicator, toast)
    core.windows = windows
    for name, w in (("main", main), ("indicator", indicator), ("toast", toast)):
        core.events.attach(name, w)
        _guard_navigation(w, server)

    def on_closing() -> bool:
        if windows.quitting:
            return True
        windows.hide_main()  # closing the main window = back to the tray
        return False

    main.events.closing += on_closing

    def companion_closing() -> bool:  # Alt+F4 on the indicator/prompt: refused (the window is reused)
        return bool(windows.quitting)

    for w in (indicator, toast):
        if w is not None:
            w.events.closing += companion_closing
    started = threading.Event()

    def boot() -> None:
        try:
            _boot(core, start_minimized)
            started.set()
        except Exception:
            log.exception("Start der neuen Oberfläche fehlgeschlagen")
            core.restart_args = ["tray", "--ui", "classic", "--once"]
            _stop_workers(core)  # pystray's thread is no daemon: it would keep this process (and its lock) alive
            windows.destroy_all()

    try:
        from callrec.ui.appid import icon_path

        # icon: pywebview 6.2.1 sets it on every WinForms form (else the icon of python(w).exe)
        webview.start(boot, gui="edgechromium" if sys.platform == "win32" else None, private_mode=False,
                      storage_path=str(paths.data_dir() / "webview"), debug=False, icon=str(icon_path()))
    except Exception as exc:
        log.exception("pywebview konnte nicht starten")
        server.stop()
        if not started.is_set():
            clear_start_marker()  # a clean, handled failure: nothing to remember for the next start
            notify_fallback(f"Die neue Oberfläche konnte nicht starten ({exc}).")
            return FALLBACK
    finally:
        core.stop_event.set()
        _stop_workers(core)
    _shutdown(core)
    server.stop()
    if core.restart_args:
        _spawn_restart(core.restart_args)
    return 0


def navigation_allowed(server: Any, uri: str) -> bool:
    return bool(server.is_own_url(uri)) or uri == "about:blank"


def harden_pywebview(server: Any) -> bool:
    """Windows/WebView2: patch pywebview 6.2.1's EdgeChrome handlers before any window exists.

    * pywebview injects ``window.pywebview`` (the js_api = the bridge) after EVERY completed
      navigation and accepts web messages from whatever page is loaded (edgechromium.py
      on_navigation_completed / on_script_notify). A foreign page in the window - a file dropped onto
      it navigates there, as would ``location = ...`` - would get the whole bridge. NavigationStarting
      (subscribed by pywebview on the GUI thread in ``EdgeChrome.__init__``, so the class attribute
      is what runs) now cancels every navigation away from the loopback pages.
    * NewWindowRequested (``window.open``, middle/Ctrl-click) went to ``webbrowser.open`` for any URL
      (file:, ms-settings:, ... - ``os.startfile`` on Windows). Now only https leaves, to the browser.
    Returns False when the module is not there (not Windows / other renderer)."""
    try:
        from webview.platforms import edgechromium as ec  # type: ignore[attr-defined]
    except Exception:
        return False
    original_start = ec.EdgeChrome.on_navigation_start

    def on_navigation_start(self: Any, sender: Any, args: Any) -> None:
        try:
            uri = str(args.Uri)
        except Exception:
            uri = ""
        if not navigation_allowed(server, uri):
            args.Cancel = True
            log.warning("Navigation zu fremder Adresse abgebrochen: %s", uri[:200])
            return
        original_start(self, sender, args)

    def on_new_window_request(self: Any, sender: Any, args: Any) -> None:
        args.set_Handled(True)  # never a second browser window inside the app
        uri = str(args.get_Uri())
        if uri.startswith("https://") and not server.is_own_url(uri):
            import webbrowser

            webbrowser.open(uri)
        else:
            log.warning("Neues Fenster verhindert: %s", uri[:200])

    ec.EdgeChrome.on_navigation_start = on_navigation_start  # type: ignore[method-assign]
    ec.EdgeChrome.on_new_window_request = on_new_window_request  # type: ignore[method-assign]
    return True


def _guard_navigation(win: Any, server: Any) -> None:
    """Only the bundled pages: a page that ends up elsewhere is sent back (defence in depth behind
    :func:`harden_pywebview` - the page opens links only via ``open_external``, and the CSP forbids
    remote content)."""

    def loaded() -> None:
        try:
            url = win.get_current_url() or ""
        except Exception:
            return
        if url and not server.is_own_url(url) and url != "about:blank":
            log.warning("Navigation zu fremder Adresse verhindert: %s", url[:200])
            win.load_url(server.url("index.html"))

    win.events.loaded += loaded



def _boot(core: WebCore, start_minimized: bool) -> None:
    """Runs in pywebview's start thread once the GUI exists: wire and start everything."""
    from callrec import autostop as autostop_mod
    from callrec.hotkey import GlobalHotkey, HotkeyError, parse_hotkey
    from callrec.postcall import startup_work
    from callrec.ui.app import detection_loop, schedule_daily_update_check
    from callrec.ui.tray import TrayIcon

    cfg = core.cfg
    core.events.start()
    core.loop.start()
    core.loop.call(core.wire)
    ui, actions, controller, queue = core.ui, core.actions, core.controller, core.queue
    windows = core.windows

    probe = autostop_mod.windows_probe(lambda: cfg.detection.teams_processes) if sys.platform == "win32" else None
    autostop = autostop_mod.AutoStopRunner(
        lambda: cfg.autostop, controller, core.loop.post, ui.notify, probe=probe,
        thread_context=autostop_mod.windows_thread_context if sys.platform == "win32" else autostop_mod._no_context,
    )

    def attention() -> None:
        # the indicator shows the countdown; without it, bring up the main window without focus
        if ui.show_overlay:
            windows.set_indicator(True, countdown=True)
        else:
            windows.show_main(activate=False)

    autostop.attention = attention
    controller.stop_info = autostop.info
    actions.autostop = autostop
    core.autostop = autostop
    autostop.start()

    def quit_app() -> None:
        autostop.shutdown()
        if controller.is_recording:
            controller.stop("app_quit")
        queue.shutdown()
        from callrec.cli import cancel_model_downloads

        cancel_model_downloads()
        core.stop_event.set()
        if core.hotkey:
            core.hotkey.stop()
        ui.broker.cancel_all()
        if core.tray is not None:
            core.tray.stop()
        core.events.stop()
        windows.destroy_all()  # ends webview.start() on the main thread

    actions.quit_handler = quit_app
    from callrec.ui.app import ShowRequests

    core.show_requests = ShowRequests(lambda: core.loop.post(lambda: windows.show_main(activate=True)))
    core.show_requests.start()  # a second start of CallRecorder shows this window
    from callrec.ui.sessionend import STOP_TIMEOUT_S, SessionEnd, install_winforms, winforms_pump

    session = SessionEnd(lambda: core.loop.call(actions.end_session, timeout=STOP_TIMEOUT_S),
                         lambda: core.loop.post(quit_app), pump=winforms_pump())
    all_windows = [w for w in (windows.main, getattr(windows, "indicator", None), getattr(windows, "toast", None))
                   if w is not None]
    installed = {"n": install_winforms(all_windows, session) if sys.platform == "win32" else len(all_windows)}

    def install_later() -> None:  # a form that did not exist yet at the start
        if installed["n"] < len(all_windows) and not windows.quitting:
            installed["n"] += install_winforms(all_windows, session)
            core.loop.after(2000, install_later)

    core.loop.after(2000, install_later)
    tray_actions: dict[str, Any] = {
        "show_window": lambda: windows.show_main(activate=True),
        "toggle": actions.toggle,
        "handoff_last": lambda: actions.handoff(None),
        "cancel_processing": lambda: actions.cancel_processing(None),
        "settings": actions.settings,
        "quit": quit_app,
    }
    core.tray = TrayIcon(tray_actions, core.loop.post)
    ui.tray = core.tray
    core.tray.start()
    try:
        core.hotkey = GlobalHotkey(parse_hotkey(cfg.hotkey.toggle), lambda: core.loop.post(controller.toggle))
        if not core.hotkey.start():
            ui.notify("Tastenkürzel", core.hotkey.error)
    except HotkeyError as exc:
        ui.notify("Tastenkürzel ungültig", str(exc))
    from callrec.notices import startup_notices

    for title, text in startup_notices(cfg):  # config warnings and, once, the notices after an update
        ui.notify(title, text)
    try:
        msg = actions.autostart.repair_if_stale()
        if msg:
            ui.notify("Autostart", msg)
    except Exception:
        log.exception("Autostart-Eintrag nicht prüfbar")
    controller.after_stop = actions.after_recording
    for rec, kind in startup_work(cfg.recordings_path()):
        if kind == "resume":
            core.loop.post(functools.partial(actions.chain.resume, rec))
        elif kind == "ask":
            ui.notify("Aufnahme noch nicht verarbeitet", rec.folder.name)
            core.loop.post(functools.partial(actions.ask_after_recording, rec))
        else:
            core.loop.post(functools.partial(controller.process, rec))
    if cfg.detection.enabled and sys.platform == "win32":
        threading.Thread(target=detection_loop, args=(cfg, controller, ui, core.stop_event), name="detect",
                         daemon=True).start()
    core.loop.after(3000, actions.show_update_result)
    actions.schedule_retention()  # automatic deletion of old recordings: at the start, then daily
    actions.schedule_profile_repair()  # once after the update to 0.4.1: rebuild/link the voice profiles
    schedule_daily_update_check(cfg, core.loop, actions)

    def tick() -> None:
        """Health check and the live state for the page and the indicator."""
        controller.tick()
        state = None
        if windows.main_visible or windows.indicator_shown:
            from callrec.ui.bridge import Bridge

            state = Bridge(core).get_state()
        if state is not None:
            if windows.main_visible:
                core.events.emit("state", state)
            if windows.indicator_shown:
                core.events.emit("state", state, target="indicator")
                view = (state.get("autostop") or {}).get("view")
                windows.set_indicator(True, countdown=view is not None)
        core.loop.after(250 if controller.is_recording else 1000, tick)

    core.loop.after(500, tick)
    threading.Thread(target=lambda: core.run_checks(), name="checks", daemon=True).start()
    if cfg.ui.start_minimized or start_minimized:
        ui.notify("CallRecorder bereit", f"Fenster: Klick auf das Symbol. Start/Stopp: {cfg.hotkey.toggle}")
    else:
        windows.show_main(activate=True)

    shown_once = threading.Event()
    windows.on_show = shown_once.set
    if windows.main_visible:
        shown_once.set()

    def watchdog() -> None:
        shown_once.wait()  # a hidden window (start in the tray) may load lazily: judge once it is shown
        if not core.page_ready.wait(PAGE_TIMEOUT_S) and not windows.quitting:
            log.error("Die neue Oberfläche hat nicht geladen - Wechsel zum klassischen Fenster")
            if core.tray is not None:
                core.tray.notify("Neue Oberfläche nicht verfügbar", "Die neue Oberfläche hat nicht geladen. CallRecorder startet "
                                 "neu mit dem klassischen Fenster (Umschalten: Einstellungen › Allgemein).")
            core.restart_args = ["tray", "--ui", "classic", "--once"]
            core.loop.post(actions.quit)

    threading.Thread(target=watchdog, name="page-watchdog", daemon=True).start()


def _stop_workers(core: WebCore) -> None:
    """Tray (a non-daemon thread) and hotkey; safe to call more than once."""
    for what in (core.hotkey, core.tray, core.show_requests):
        if what is not None:
            try:
                what.stop()
            except Exception:
                log.debug("Beenden von %r fehlgeschlagen", what, exc_info=True)
    if core.autostop is not None:
        try:
            core.autostop.shutdown()
        except Exception:
            log.debug("Auto-Stopp nicht beendet", exc_info=True)


def _shutdown(core: WebCore) -> None:
    """After the GUI loop ended: let a cancelled job record its state, stop the app thread."""
    if core.queue is not None:
        core.queue.stop()
        core.queue.join(timeout=10)
    core.loop.stop()
    core.loop.join(timeout=5)
    if core.samples is not None:
        core.samples.close()  # speaker clips in the temp folder
    time.sleep(0.05)
