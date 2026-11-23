"""The js_api of the web UI: the only door from the page into Python.

Thin by design: every method validates its arguments, then calls AppActions / the controller /
the DTO functions. pywebview exposes all public attributes of this object (also nested ones), so
it has public *methods* only; everything else is stored under a single underscore name.
Recording ids from the page are folder names and are checked against the recordings folder.

Threads: pywebview runs each call on a thread of its own. Calls that need the app thread go
through ``loop.post`` (fire and forget - dialogs that follow come as events) or ``loop.call``
(short reads/writes). Nothing here blocks the GUI main thread.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any

from callrec.recording import Recording

log = logging.getLogger(__name__)
jslog = logging.getLogger("callrec.ui.page")

_ID = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]{1,200}$")
REC_ACTIONS = ("process_now", "review", "resummarize", "handoff", "play", "folder", "retranscribe", "assign_event",
               "participants", "recheck", "rerender", "speaker_limit", "reprocess")


# js_api object -> {"core": WebCore, "window": name}; not reachable by attribute access from the page
_STATE: weakref.WeakKeyDictionary[Any, dict[str, Any]] = weakref.WeakKeyDictionary()


class BridgeError(ValueError):
    pass


def _err(exc: BaseException) -> dict[str, Any]:
    from callrec import hints

    h = hints.explain(str(exc))
    return {"error": str(exc) or exc.__class__.__name__, "hint": {"next": h.next_step, "action": h.action,
                                                                    "label": h.action_label}}


def safe(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Errors become ``{"error": ...}`` (German text) instead of a JS exception with a traceback."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except BridgeError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            log.exception("Fehler in %s", fn.__name__)
            return _err(exc)

    return wrapper


def _core(api: Any) -> Any:
    return _STATE[api]["core"]


class Bridge:
    # No instance attributes at all: pywebview hides "_" names only when it builds window.pywebview.api,
    # but resolves the name a page sends with getattr() for every part (webview/util.py
    # js_bridge_call -> get_nested_attribute), so "_core.cfg.summary.__setattr__" would be callable from
    # page script. The core therefore lives outside the object (module registry, see _core()).
    __slots__ = ("__weakref__",)

    def __init__(self, core: Any) -> None:
        _STATE[self] = {"core": core}

    # ------------------------------------------------------------------ helpers (private)
    def _rec(self, rec_id: Any) -> Recording:
        """A recording folder named by the page - only directly inside the recordings folder."""
        if not isinstance(rec_id, str) or not _ID.match(rec_id) or rec_id in (".", "..") or rec_id.strip() != rec_id:
            raise BridgeError("Ungültige Aufnahme.")
        root = _core(self).cfg.recordings_path().resolve()
        folder = (root / rec_id).resolve()
        if folder.parent != root or not folder.is_dir():
            raise BridgeError("Diese Aufnahme gibt es nicht (mehr).")
        return Recording(folder)

    def _info(self, rec: Recording) -> Any:
        from callrec.library import read_info

        _core(self).index.invalidate(rec.folder)
        return read_info(rec.folder)

    def _busy_ids(self) -> set[str]:
        snap = _core(self).queue.snapshot()
        jobs = ([snap.current] if snap.current is not None else []) + list(snap.pending)
        return {j.recording.folder.name for j in jobs}

    def _recording_id(self) -> str:
        cur = _core(self).controller.current
        return cur.folder.name if cur is not None else ""

    def _post(self, fn: Callable[[], object]) -> dict[str, Any]:
        _core(self).loop.post(fn)
        return {"ok": True}

    # ------------------------------------------------------------------ page lifecycle
    @safe
    def ready(self, which: str = "main") -> dict[str, Any]:
        """The page loaded (again): state to start from, open dialogs to show again."""
        core = _core(self)
        core.events.set_ready(which if which in ("main", "indicator", "toast") else "main")
        if which == "main":
            core.page_ready.set()
            from callrec.ui.webapp import clear_start_marker

            clear_start_marker()  # the web UI started fine
        from callrec.updater import version_text

        return {"version": version_text(), "theme": core.cfg.ui.theme, "hotkey": core.cfg.hotkey.toggle,
                "consent_mode": core.consent_mode(), "dialogs": core.ui.broker.open_dialogs(),
                "state": self.get_state(), "route": core.pending_route or "", "toast": core.ui.toast_data}

    @safe
    def log_js_error(self, message: str, source: str = "", line: int = 0, stack: str = "") -> bool:
        jslog.error("Fehler in der Oberfläche: %s (%s:%s)\n%s", str(message)[:2000], str(source)[:300], line,
                    str(stack)[:4000])
        return True

    @safe
    def open_external(self, url: str) -> bool:
        """Links leave the app only to the system browser, and only https."""
        if not isinstance(url, str) or not url.startswith("https://") or len(url) > 2000:
            raise BridgeError("Nur https-Links werden geöffnet.")
        import webbrowser

        webbrowser.open(url)
        return True

    # ------------------------------------------------------------------ state
    @safe
    def get_state(self) -> dict[str, Any]:
        core = _core(self)
        c = core.controller
        a = core.actions.autostop
        view = a.view() if (a is not None and c.is_recording) else None
        from callrec.ui.dto import queue_dto

        return {
            "recording": {"on": c.is_recording, "title": c.current_title(), "elapsed": c.elapsed(),
                          "id": self._recording_id(), "levels": c.levels() if c.is_recording else {}},
            "autostop": {"status": a.status_text() if a is not None else "",
                         "message": a.message() if a is not None and view is None else "",
                         "view": ({"kind": view.kind, "headline": view.headline, "detail": view.detail,
                                   "seconds_left": view.seconds_left, "keep_label": view.keep_label,
                                   "stop_label": view.stop_label} if view is not None else None)},
            "queue": queue_dto(core.queue.snapshot()),
            "status": core.ui.status_text,
            "setup": core.setup_summary,
            "me": core.me_name(),
        }

    @safe
    def get_home(self) -> dict[str, Any]:
        from callrec.ui.dto import home

        infos = _core(self).index.scan()
        return home(infos, self._busy_ids(), self._recording_id(), _core(self).queue.snapshot())

    @safe
    def get_calendar(self, force: bool = False) -> dict[str, Any]:
        """Today's events, best first (may take a moment: Microsoft Graph or the cache)."""
        from callrec.ui.dto import calendar_events

        cal = _core(self).controller.calendar
        events, message, source = cal.candidates()
        with _core(self).lock:
            _core(self).event_cache = list(events)
        return {**calendar_events(events, source, message, getattr(cal, "is_current", None)),
                "connected": cal.source is not None}

    # ------------------------------------------------------------------ recording
    @safe
    def start_recording(self, choice: Any = None) -> dict[str, Any]:
        """``choice``: index into the last calendar list, "manual" (no event) or None (let the
        calendar choose). The consent question follows as a dialog - there is no other start path."""
        from callrec.calendar.service import Suggestion

        core = _core(self)
        sugg: Suggestion | None = None
        if choice == "manual":
            sugg = Suggestion(None, [], "Ohne Termin – Titel und Teilnehmende eingeben.", "manual")
        elif isinstance(choice, int) and not isinstance(choice, bool):
            with core.lock:
                events = list(core.event_cache)
            if 0 <= choice < len(events):
                ev = events[choice]
                sugg = Suggestion(ev, ev.participants(), source="graph")

        def go() -> None:
            if core.controller.is_recording or core.controller.just_stopped():
                return
            core.actions.start_recording(sugg)

        return self._post(go)

    @safe
    def stop_recording(self) -> dict[str, Any]:
        return self._post(_core(self).actions.stop_recording)

    @safe
    def autostop(self, what: str) -> dict[str, Any]:
        a = _core(self).actions.autostop
        if a is None:
            return {"ok": False}
        if what == "keep":
            return self._post(a.keep_recording)
        if what == "stop":
            def stop() -> None:
                a.stop_now()
                _core(self).actions._changed(None)
            return self._post(stop)
        if what == "dismiss":
            return self._post(a.dismiss_message)
        raise BridgeError("Unbekannte Aktion")

    # ------------------------------------------------------------------ dialogs
    @safe
    def answer(self, dialog_id: str, result: Any = None) -> bool:
        return _core(self).ui.broker.answer(str(dialog_id), result if isinstance(result, (dict, type(None))) else None)

    @safe
    def import_ics(self, dialog_id: str) -> dict[str, Any]:
        """Native file dialog, then the event from the .ics file replaces the suggestion of the open
        start dialog (kept in Python; the page only gets texts to show)."""
        from callrec.people import format_participants
        from callrec.ui.webui import suggestion_text

        core = _core(self)
        ctx = core.ui.broker.context.get(str(dialog_id))
        if ctx is None or core.ui.broker.kind_of(str(dialog_id)) != "start":
            raise BridgeError("Kein offener Startdialog.")
        paths = core.file_dialog("open", ("Kalenderdatei (*.ics)",))
        if not paths:
            return {"cancelled": True}
        try:
            s = core.controller.calendar.suggest_from_ics(Path(paths[0]))
        except Exception as exc:
            raise BridgeError(f"Die Datei konnte nicht gelesen werden: {exc}") from exc
        ctx["suggestion"] = s
        return {"title": s.event.subject if s.event else "", "participants_text": format_participants(s.participants),
                "info": suggestion_text(s)}

    @safe
    def pending_dialogs(self) -> list[dict[str, Any]]:
        return list(_core(self).ui.broker.open_dialogs())

    @safe
    def play_sample(self, rec_id: str, speaker_id: str, step: int = 0) -> dict[str, Any]:
        """``step`` 1 = "Weiteres Beispiel" (the next passage of this speaker)."""
        self._rec(rec_id)
        return dict(_core(self).samples.url(rec_id, str(speaker_id), 1 if step else 0))

    # ------------------------------------------------------------------ recordings
    @safe
    def list_recordings(self, flt: str = "all", query: str = "") -> dict[str, Any]:
        from callrec.ui.dto import recordings_list

        infos = _core(self).index.scan()
        return recordings_list(infos, str(flt or "all"), str(query or "")[:200], self._busy_ids(),
                               self._recording_id(), _core(self).cfg.retention)

    @safe
    def get_recording(self, rec_id: str) -> dict[str, Any]:
        from callrec.ui.dto import recording_detail

        rec = self._rec(rec_id)
        info = self._info(rec)
        core = _core(self)
        busy = core.controller.is_busy(rec)
        return recording_detail(info, busy, self._recording_id() == rec.folder.name,
                                core.controller.calendar.source is not None, core.cfg.retention)

    @safe
    def get_text(self, rec_id: str, kind: str, file: str = "") -> dict[str, Any]:
        from callrec.ui.dto import read_text

        rec = self._rec(rec_id)
        if not isinstance(file, str) or "/" in file or "\\" in file:
            raise BridgeError("Ungültige Version.")
        return read_text(rec.folder, str(kind), file)

    @safe
    def recording_action(self, rec_id: str, key: str) -> dict[str, Any]:
        """One of the per-recording actions; dialogs that follow come as events."""
        if key not in REC_ACTIONS:
            raise BridgeError("Unbekannte Aktion.")
        rec = self._rec(rec_id)
        a = _core(self).actions
        fns: dict[str, Callable[[], object]] = {
            "process_now": lambda: a.process_now(rec), "review": lambda: a.review(rec),
            "resummarize": lambda: a.resummarize(rec), "handoff": lambda: a.handoff(rec),
            "play": lambda: a.play(rec), "folder": lambda: a.open_folder(rec),
            "retranscribe": lambda: a.ask_retranscribe(rec), "assign_event": lambda: a.assign_event(rec),
            "participants": lambda: a.edit_participants(rec), "recheck": lambda: a.recheck_speakers(rec),
            "rerender": lambda: a.rerender(rec), "speaker_limit": lambda: a.toggle_speaker_limit(rec),
            "reprocess": lambda: a.reprocess(rec),
        }
        return self._post(fns[key])

    @safe
    def rename(self, rec_id: str, title: str) -> dict[str, Any]:
        rec = self._rec(rec_id)
        if not isinstance(title, str) or not title.strip():
            raise BridgeError("Der Titel darf nicht leer sein.")
        new = _core(self).loop.call(lambda: _core(self).actions.rename(rec, title[:200]))
        return {"ok": True, "title": new}

    @safe
    def delete_info(self, rec_id: Any, scope: str = "recording") -> dict[str, Any]:
        """The confirmation for deleting ``scope`` of one recording or a list of them (multi-select):
        the same wording as the classic window (viewmodel.delete_question)."""
        from callrec.library import recycle_bin_available
        from callrec.ui.viewmodel import delete_question

        if scope not in ("recording", "audio_transcript", "audio"):
            raise BridgeError("Unbekannter Umfang.")
        ids = [str(x) for x in rec_id][:500] if isinstance(rec_id, list) else [str(rec_id)]
        recs = [self._rec(i) for i in ids]
        infos = [self._info(r) for r in recs]
        busy = [i.title for r, i in zip(recs, infos, strict=True) if _core(self).controller.is_busy(r)]
        # the whole recording goes to the recycle bin where there is one; the partial deletions are
        # permanent (the remaining recording would otherwise point at files in the recycle bin)
        permanent = scope != "recording" or not recycle_bin_available()
        title, message, button = delete_question(infos, scope, permanent)
        return {"ids": ids, "scope": scope, "title": title, "message": message, "confirm": button,
                "permanent": permanent, "recycle_bin": recycle_bin_available(),
                "handed_over": any(bool(i.handoff) for i in infos), "busy": busy}

    @safe
    def delete(self, rec_id: Any, permanent: bool = False, scope: str = "recording") -> dict[str, Any]:
        """``{"ok": True, "done": n, "errors": [...]}``; when the recycle bin fails
        ``{"confirm": {"kind": "permanent"}}`` (nothing deleted then)."""
        from callrec.library import LibraryError

        if scope not in ("recording", "audio_transcript", "audio"):
            raise BridgeError("Unbekannter Umfang.")
        ids = [str(x) for x in rec_id][:500] if isinstance(rec_id, list) else [str(rec_id)]
        recs = [self._rec(i) for i in ids]
        a = _core(self).actions
        done, errors = 0, []
        for rec in recs:
            try:
                if scope == "recording":
                    _core(self).loop.call(lambda rec=rec: a.delete(rec, permanent=permanent is True))
                else:
                    _core(self).loop.call(lambda rec=rec: a.remove(rec, scope, permanent=permanent is True))
                done += 1
            except LibraryError as exc:
                if scope == "recording" and permanent is not True and "Papierkorb" in str(exc) and not done:
                    return {"confirm": {"kind": "permanent", "message": str(exc)}}
                errors.append(f"{rec.folder.name}: {exc}")
        if errors and not done:
            raise BridgeError(" ".join(errors))
        return {"ok": True, "done": done, "errors": errors}

    @safe
    def pin(self, rec_id: Any, on: bool = True) -> dict[str, Any]:
        """ "Behalten" (never deleted automatically) for one or several recordings."""
        ids = [str(x) for x in rec_id][:500] if isinstance(rec_id, list) else [str(rec_id)]
        recs = [self._rec(i) for i in ids]
        for rec in recs:
            _core(self).loop.call(lambda rec=rec: _core(self).actions.set_pinned(rec, on is True))
        return {"ok": True}

    @safe
    def retention_overview(self) -> dict[str, Any]:
        """What the automatic deletion removes next (home view, library, settings)."""
        from datetime import datetime

        from callrec.retention import BUSY_TEXT, SCOPE_TEXT, mark_surfaced, read_log

        core = _core(self)
        plan = core.actions.retention_plan()
        now = datetime.now().astimezone()
        # shown: an unprocessed one may be removed after 24 h - but only when somebody can see it (the
        # page also loads in the hidden window of a start in the tray)
        windows = getattr(core, "windows", None)
        if windows is None or getattr(windows, "main_visible", False):
            mark_surfaced(plan, now)
        rt = core.cfg.retention
        items = [{"id": i.id, "title": i.title, "when": i.when_text(now), "warning": i.warning,
                  "due": i.due, "waiting": i.waiting} for i in plan.removable + plan.waiting + plan.upcoming]
        return {"enabled": plan.enabled, "notice": plan.notice(now), "items": items,
                "next24": [i.id for i in plan.next_24h(now)], "scope": SCOPE_TEXT.get(rt.scope, rt.scope),
                # what "Jetzt löschen" takes (not what is recorded, processed or open in a dialog right now)
                "next24_now": [i.id for i in plan.next_24h(now) if i.waiting != BUSY_TEXT],
                "days": rt.max_age_days, "permanent": rt.delete_mode == "permanent",
                "blocked": plan.blocked, "log": read_log(50)[::-1]}

    @safe
    def retention_now(self, ids: Any) -> dict[str, Any]:
        """ "Jetzt löschen" in the pre-notice: those recordings, with the configured scope and mode."""
        from datetime import datetime

        core = _core(self)
        rt = core.cfg.retention
        # only what is still announced now (not pinned, not deleted meanwhile in the other window/run)
        from callrec.retention import BUSY_TEXT

        # nor one that is recorded, processed or open in a dialog now (shown as "wird gerade … verarbeitet")
        current = {i.id for i in core.actions.retention_plan().next_24h(datetime.now().astimezone())
                   if i.waiting != BUSY_TEXT}
        wanted = [str(x) for x in ids if str(x) in current] if isinstance(ids, list) else []
        if not wanted:
            return {"ok": True, "done": 0, "errors": []}
        return self.delete(wanted, rt.delete_mode == "permanent", rt.scope)

    @safe
    def cancel_processing(self, job_id: Any = None) -> dict[str, Any]:
        jid = job_id if isinstance(job_id, int) and not isinstance(job_id, bool) else None
        text = _core(self).loop.call(lambda: _core(self).actions.cancel_processing(jid))
        return {"ok": True, "text": text}

    @safe
    def cancel_pending(self) -> dict[str, Any]:
        q = _core(self).queue
        for j in q.snapshot().pending:
            q.cancel(j.id)
        return {"ok": True}

    @safe
    def open_folder(self) -> dict[str, Any]:
        return self._post(lambda: _core(self).actions.open_folder(None))

    # ------------------------------------------------------------------ setup checklist
    @safe
    def run_checks(self, force_audio: bool = False) -> dict[str, Any]:
        return _core(self).run_checks(force_audio is True)

    @safe
    def setup_action(self, key: str) -> dict[str, Any]:
        return _core(self).setup_action(str(key))

    # ------------------------------------------------------------------ settings
    @safe
    def get_settings(self) -> dict[str, Any]:
        return _core(self).settings_data()

    @safe
    def save_settings(self, patch: dict[str, Any], confirmed: Any = None, autostart: Any = None) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise BridgeError("Ungültige Einstellungen.")
        conf = [str(x) for x in confirmed] if isinstance(confirmed, list) else []
        return _core(self).save_settings(patch, conf, autostart if isinstance(autostart, bool) else None)

    @safe
    def pick_folder(self) -> dict[str, Any]:
        paths = _core(self).file_dialog("folder", ())
        return {"path": str(paths[0])} if paths else {"cancelled": True}

    @safe
    def save_preset(self, name: str, patch: dict[str, Any]) -> dict[str, Any]:
        from callrec.config import load_config, save_preset
        from callrec.ui.settingsmodel import apply_patch

        cfg = apply_patch(load_config(preset=""), patch if isinstance(patch, dict) else {})
        path = save_preset(cfg, str(name or ""))
        return {"ok": True, "path": str(path)}

    @safe
    def api_key(self, action: str, key: str = "") -> dict[str, Any]:
        """Save or delete the Anthropic key (DPAPI). The key itself never goes back to the page."""
        from callrec import apikey

        if action == "save":
            apikey.store_key(str(key or ""))
        elif action == "delete":
            apikey.delete_key()
        elif action != "status":
            raise BridgeError("Unbekannte Aktion")
        stored, source = apikey.load_key()
        return {"ok": True, "present": bool(stored), "masked": apikey.mask(stored) if stored else "",
                "source": source}

    @safe
    def test_anthropic(self, patch: dict[str, Any], typed_key: str = "") -> dict[str, Any]:
        from callrec import apikey
        from callrec.config import load_config
        from callrec.llm_anthropic import TEST_TIMEOUT_S, AnthropicProvider
        from callrec.ui.settingsmodel import apply_patch

        cfg = apply_patch(load_config(preset=""), patch if isinstance(patch, dict) else {})
        key = str(typed_key or "").strip() or apikey.load_key()[0]
        provider = AnthropicProvider(key, cfg.summary.anthropic_model, effort=cfg.summary.anthropic_effort,
                                     max_attempts=1, timeout_s=TEST_TIMEOUT_S)
        return {"ok": True, "text": str(provider.test_connection())}

    @safe
    def llm_models(self, patch: dict[str, Any]) -> dict[str, Any]:
        from callrec.config import load_config
        from callrec.llm import make_provider
        from callrec.ui.settingsmodel import apply_patch

        cfg = apply_patch(load_config(preset=""), patch if isinstance(patch, dict) else {})
        p = make_provider(cfg.summary)
        return {"models": list(p.list_models()), "version": p.version() or "", "can_pull": bool(p.can_pull)}

    @safe
    def pull_llm_model(self, patch: dict[str, Any], model: str) -> dict[str, Any]:
        """Download in a worker; progress as ``pull`` events."""
        from callrec.config import load_config
        from callrec.llm import make_provider
        from callrec.ui.settingsmodel import apply_patch

        cfg = apply_patch(load_config(preset=""), patch if isinstance(patch, dict) else {})
        p = make_provider(cfg.summary)
        if not p.can_pull:
            raise BridgeError("Dieser Anbieter kann keine Modelle laden – bitte in dessen Oberfläche laden.")
        name = str(model or "").strip()
        if not name:
            raise BridgeError("Kein Modell angegeben.")
        emit = _core(self).events.emit

        def work() -> None:
            try:
                p.pull(name, lambda status, frac: emit("pull", {"status": status, "fraction": frac}))
                emit("pull", {"status": f"Modell {name} ist aktuell.", "fraction": 1.0, "done": True})
            except Exception as exc:
                emit("pull", {"status": str(exc), "fraction": None, "done": True, "error": True})

        threading.Thread(target=work, name="pull-model", daemon=True).start()
        return {"ok": True}

    @safe
    def benchmark(self, action: str = "start") -> dict[str, Any]:
        """"Geschwindigkeit messen" (start) or the current overview (info)."""
        core = _core(self)
        if action == "info":
            return dict(core.speed_info())
        if action != "start":
            raise BridgeError("Unbekannte Aktion.")
        return dict(core.start_benchmark())

    @safe
    def whisper_models(self, action: str = "list", name: str = "", force_gpu: bool = False) -> dict[str, Any]:
        return _core(self).whisper_models(str(action), str(name), force_gpu is True)

    @safe
    def prompts(self, action: str = "get", texts: Any = None, force: bool = False, rec_id: str = "") -> dict[str, Any]:
        rec = self._rec(rec_id) if rec_id else None
        return _core(self).prompts(str(action), texts if isinstance(texts, dict) else {}, force is True, rec)

    @safe
    def text_file(self, kind: str, text: Any = None) -> dict[str, Any]:
        from callrec import paths
        from callrec.userfiles import ensure_user_files

        files = {"glossary": paths.glossary_file(), "replacements": paths.replacements_file()}
        if kind not in files:
            raise BridgeError("Unbekannte Datei")
        ensure_user_files()
        p = files[kind]
        if isinstance(text, str):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text[:500000], encoding="utf-8")
            return {"ok": True}
        return {"text": p.read_text(encoding="utf-8") if p.exists() else "", "path": str(p)}

    @safe
    def profiles(self, action: str = "list", pid: str = "", index: Any = None) -> dict[str, Any]:
        return _core(self).profiles_action(str(action), str(pid), index)

    # ------------------------------------------------------------------ updates, about, app
    @safe
    def check_updates(self) -> dict[str, Any]:
        return self._post(lambda: _core(self).actions.check_updates())

    @safe
    def restore_previous_version(self) -> dict[str, Any]:
        return self._post(_core(self).actions.restore_previous_version)

    @safe
    def about(self) -> dict[str, Any]:
        from callrec import paths
        from callrec.updater import read_install_info, version_text

        info = read_install_info()
        return {"version": version_text(), "python": sys.version.split()[0], "log": str(_core(self).log_file or ""),
                "config": str(paths.config_file()), "recordings": str(_core(self).cfg.recordings_path()),
                "previous": str(info.get("previous_version") or ""), "frontend": "web"}

    @safe
    def open_path(self, what: str) -> dict[str, Any]:
        a = _core(self).actions
        fns: dict[str, Callable[[], object]] = {"log": a.open_log, "readme": a.open_readme,
                                                "recordings": lambda: a.open_folder(None)}
        if what not in fns:
            raise BridgeError("Unbekanntes Ziel")
        return self._post(fns[what])

    @safe
    def set_theme(self, mode: str) -> dict[str, Any]:
        return _core(self).save_settings({"ui.theme": str(mode)}, [], None)

    @safe
    def switch_frontend(self, frontend: str, restart: bool = False) -> dict[str, Any]:
        """Back to the classic window (or to the preview): saved in config.toml; with ``restart`` the
        app restarts right away."""
        res = _core(self).save_settings({"ui.frontend": "tk" if frontend == "tk" else "web"}, [], None)
        if res.get("ok") and restart is True:
            _core(self).restart_later()
        return res

    @safe
    def hide_window(self) -> bool:
        _core(self).windows.hide_main()
        return True

    @safe
    def quit_app(self) -> bool:
        _core(self).loop.post(_core(self).actions.quit)
        return True

    @safe
    def save_window_state(self, state: dict[str, Any]) -> bool:
        _core(self).save_window_state(state if isinstance(state, dict) else {})
        return True


class CompanionApi:
    """js_api of the indicator and the toast window: only these few calls."""

    __slots__ = ("__weakref__",)  # see Bridge

    def __init__(self, core: Any, window: str) -> None:
        _STATE[self] = {"core": core, "window": window}

    @safe
    def ready(self) -> dict[str, Any]:
        _core(self).events.set_ready(_STATE[self]["window"])
        return {"state": Bridge(_core(self)).get_state(), "toast": _core(self).ui.toast_data,
                "theme": _core(self).cfg.ui.theme}

    @safe
    def state(self) -> dict[str, Any]:
        return dict(Bridge(_core(self)).get_state())

    @safe
    def stop(self) -> bool:
        _core(self).loop.post(_core(self).actions.stop_recording)
        return True

    @safe
    def autostop(self, what: str) -> dict[str, Any]:
        return dict(Bridge(_core(self)).autostop(what))

    @safe
    def toast_answer(self, choice: str, toast_id: str = "") -> bool:
        return bool(_core(self).ui.toast_answer(str(choice), str(toast_id or "")))

    @safe
    def show_main(self) -> bool:
        _core(self).windows.show_main(activate=True)
        return True

    @safe
    def log_js_error(self, message: str, source: str = "", line: int = 0, stack: str = "") -> bool:
        jslog.error("Fehler im Begleitfenster %s: %s (%s:%s)\n%s", _STATE[self]["window"], str(message)[:2000],
                    str(source)[:300], line, str(stack)[:4000])
        return True
