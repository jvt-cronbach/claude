"""In-page dialogs of the web UI: a request/answer broker plus the DialogPort AppActions talks to.

``DialogBroker.ask(kind, data)`` sends ``dialog {id, kind, data}`` to the page and waits for
``answer(id, result)`` from the bridge. Waiting happens on the app thread with pumping (other
posted calls keep running, like Tk's nested loop while a modal dialog was open) - never on the
GUI main thread. Every answer is validated here; the page only ever sends choices.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from callrec.models import Person
from callrec.recording import Recording
from callrec.ui.apploop import AppLoop
from callrec.ui.dialogport import AFTER_DISCARD, AFTER_FULL, AFTER_LATER

log = logging.getLogger(__name__)


class DialogBroker:
    def __init__(self, loop: AppLoop, emit: Callable[[str, dict[str, Any]], None],
                 show_main: Callable[[bool], None] = lambda activate: None) -> None:
        self.loop = loop
        self.emit = emit
        self.show_main = show_main
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[str, Future[Any], dict[str, Any]]] = {}
        self.context: dict[str, dict[str, Any]] = {}  # server-side state of an open dialog (never sent)

    def ask(self, kind: str, data: dict[str, Any], activate: bool = True, timeout: float | None = None,
            dialog_id: str = "") -> Any:
        did = dialog_id or uuid.uuid4().hex
        fut: Future[Any] = Future()
        with self._lock:
            self._pending[did] = (kind, fut, data)
        try:
            self.show_main(activate)
            self.emit("dialog", {"id": did, "kind": kind, "data": data})
            try:
                return self.loop.wait(fut, timeout)
            except (TimeoutError, Exception) as exc:  # cancelled at shutdown / timed out = no answer
                if not isinstance(exc, TimeoutError):
                    log.debug("Dialog %s ohne Antwort beendet", kind, exc_info=True)
                return None
        finally:
            with self._lock:
                self._pending.pop(did, None)
            self.context.pop(did, None)
            self.emit("dialog_close", {"id": did})

    def answer(self, dialog_id: str, result: Any) -> bool:
        with self._lock:
            entry = self._pending.get(str(dialog_id))
        if entry is None:
            return False
        fut = entry[1]
        if not fut.done():
            fut.set_result(result)
        return True

    def kind_of(self, dialog_id: str) -> str:
        with self._lock:
            entry = self._pending.get(str(dialog_id))
        return entry[0] if entry else ""

    def open_dialogs(self) -> list[dict[str, Any]]:
        """What the page must show again after a reload."""
        with self._lock:
            return [{"id": k, "kind": v[0], "data": v[2]} for k, v in self._pending.items()]

    def cancel_all(self) -> None:
        with self._lock:
            futs = [v[1] for v in self._pending.values()]
        for f in futs:
            if not f.done():
                f.set_result(None)


def _as_dict(x: Any) -> dict[str, Any]:
    return x if isinstance(x, dict) else {}


def _bool(x: Any) -> bool:
    return x is True


def message(broker: DialogBroker, title: str, text: str, tone: str = "info") -> None:
    broker.ask("info", {"title": title, "message": text, "tone": tone})


def confirm(broker: DialogBroker, title: str, text: str, confirm_label: str = "Ja", cancel_label: str = "Abbrechen",
            danger: bool = False, warning: bool = False) -> bool:
    res = broker.ask("confirm", {"title": title, "message": text, "confirm": confirm_label, "cancel": cancel_label,
                                 "danger": danger, "warning": warning})
    return _as_dict(res).get("ok") is True


class WebDialogPort:
    """:class:`callrec.ui.dialogport.DialogPort` for the web UI."""

    def __init__(self, broker: DialogBroker, navigate: Callable[[str, dict[str, Any]], None],
                 notify: Callable[[str, str], None], samples: Any = None) -> None:
        self.broker = broker
        self.navigate = navigate
        self.notify = notify
        self.samples = samples  # SampleClips (speaker samples for the review dialog)

    # -- after a recording ------------------------------------------------------
    def ask_after_recording(self, rec: Recording, plan: Any, handoff_problem: str = "",
                            summary_problem: str = "") -> tuple[str, Any]:
        from callrec.postcall import Plan
        from callrec.ui.dto import after_recording_data

        res = _as_dict(self.broker.ask("after_recording", after_recording_data(rec, plan, handoff_problem,
                                                                                summary_problem)))
        choice = res.get("choice")
        if choice == AFTER_FULL:
            p = _as_dict(res.get("plan"))
            return AFTER_FULL, Plan(_bool(p.get("transcript")), _bool(p.get("speakers")), _bool(p.get("summary")),
                                    _bool(p.get("handoff")))
        if choice == AFTER_DISCARD:  # the page asked "wirklich verwerfen?" before answering this
            return AFTER_DISCARD, None
        return AFTER_LATER, None  # "Später", closed, Esc

    # -- speakers -------------------------------------------------------------------
    def review_speakers(self, rec: Recording, profiles: Any, rerender: Callable[[], object],
                        resummarize: Callable[[], object], handoff: Callable[[], object] | None = None,
                        in_chain: bool = False, speaker_cfg: Any = None,
                        allow_more: Callable[[], object] | None = None) -> bool:
        from callrec.ui.dto import speaker_review

        data = speaker_review(rec, profiles, speaker_cfg, in_chain=in_chain, can_handoff=handoff is not None,
                              can_allow_more=allow_more is not None)
        if self.samples is not None:
            self.samples.register(rec, data)
        allowed_ids = {r["id"] for r in data["rows"]}
        try:
            return self._review_loop(rec, profiles, rerender, resummarize, handoff, in_chain, speaker_cfg,
                                     allow_more, data, allowed_ids)
        finally:
            if self.samples is not None:
                self.samples.release()

    def _review_loop(self, rec: Recording, profiles: Any, rerender: Callable[[], object],
                     resummarize: Callable[[], object], handoff: Callable[[], object] | None, in_chain: bool,
                     speaker_cfg: Any, allow_more: Callable[[], object] | None, data: dict[str, Any],
                     allowed_ids: set[str]) -> bool:
        from callrec.review import Assignment, apply_assignments

        while True:
            res = _as_dict(self.broker.ask("review", data))
            action = res.get("action")
            if action == "more" and allow_more is not None and not in_chain:
                allow_more()
                return False
            if action == "handoff" and handoff is not None and not in_chain:
                handoff()
                continue
            if action != "apply":
                return False
            assignments = []
            for a in res.get("assignments") or []:
                a = _as_dict(a)
                sid = str(a.get("id") or "")
                if sid not in allowed_ids:
                    continue
                assignments.append(Assignment(sid, str(a.get("name") or "")[:200], _bool(a.get("keep"))))
            try:
                msgs = apply_assignments(rec, assignments, profiles, rerender,
                                         min_learn_speech_s=getattr(speaker_cfg, "min_learn_speech_s", None))
            except Exception as exc:
                log.exception("Zuordnung fehlgeschlagen")
                data = {**data, "error": f"Zuordnung nicht gespeichert: {exc}"}
                continue
            if res.get("resummarize") is True and not in_chain:
                resummarize()
            if msgs:
                self.notify("Sprecher gespeichert", " ".join(msgs))
            return True

    def edit_participants(self, rec: Recording) -> list[Person] | None:
        from callrec.people import format_participants, parse_participants_text

        try:
            title = rec.title()
        except (OSError, ValueError):
            title = rec.folder.name
        res = self.broker.ask("participants", {"title": title, "text": format_participants(rec.participants())})
        if not isinstance(res, dict) or not isinstance(res.get("text"), str):
            return None
        return parse_participants_text(res["text"][:20000])

    def confirm_reset_foreign(self, rec: Recording, foreign: list[tuple[str, str, str]]) -> bool:
        lines = "\n".join(f"• {label}: {name}" for _sid, label, name in foreign)
        return confirm(self.broker, "Frühere Zuordnungen prüfen",
                       f"In „{rec.title()}“ sind Stimmen Personen zugeordnet, die keine Teilnehmenden dieses Termins "
                       f"sind:\n\n{lines}\n\nFrühere Versionen haben automatisch erkannte Namen beim Klick auf "
                       "„Übernehmen“ als bestätigt gespeichert – diese Zuordnungen sind daher womöglich nie geprüft "
                       "worden. Die Stimmprofile dieser Personen werden danach aus den übrigen Zuordnungen neu aufgebaut.",
                       "Zurücksetzen und neu erkennen", "Nicht zurücksetzen", warning=True)

    def confirm_recheck(self, rec: Recording, changes: list[tuple[str, str, str]]) -> bool:
        if not changes:
            message(self.broker, "Sprecherzuordnung neu prüfen", "Nach der Regel „nur Teilnehmende dieses Termins“ "
                    "ändert sich nichts. Manuelle Zuordnungen werden nie geändert.")
            return False
        lines = "\n".join(f"• {label}: {before} → {after}" for label, before, after in changes)
        return confirm(self.broker, "Sprecherzuordnung neu prüfen",
                       f"Automatische Zuordnungen in „{rec.title()}“ nach der Regel „nur Teilnehmende dieses "
                       f"Termins“:\n\n{lines}\n\nManuelle Zuordnungen bleiben unverändert. Das Transkript wird danach "
                       "neu erstellt; die bisherige Zuordnung wird unter data\\archive gesichert.", "Übernehmen")

    def confirm_cloud_summary(self, rec: Recording, me: Person) -> bool:
        from callrec.consent import LATER_STATEMENT_ANTHROPIC, later_confirmation, record_later_confirmation

        ok = confirm(self.broker, "Übertragung an Anthropic bestätigen",
                     f"Die Einwilligung für „{rec.title()}“ wurde ohne Hinweis auf Anthropic erteilt.\n\n"
                     f"{LATER_STATEMENT_ANTHROPIC}\n\nNur nach dieser Bestätigung wird das Transkript an Anthropic "
                     "gesendet.", "Bestätigen und senden", "Nicht senden", warning=True)
        if not ok:
            return False
        record_later_confirmation(rec, later_confirmation(me.display or "Benutzer", "dialog", True))
        return True

    # -- hand-over --------------------------------------------------------------------
    def ask_handoff(self, rec: Recording, cfg: Any, perform: Callable[[str], dict[str, Any]]) -> bool:
        from callrec.handoff import HANDOFF_WARNING, HandoffError, build_summary_document, last_handoff
        from callrec.ui.viewmodel import markdown_blocks

        try:
            doc = build_summary_document(rec, cfg.handoff.include_participant_emails)
        except HandoffError as exc:
            message(self.broker, "Keine Übergabe möglich", str(exc))
            return False
        prev = last_handoff(rec)
        target = cfg.handoff.folder if cfg.handoff.mode == "folder" else f"Graph-Upload: {cfg.handoff.graph_folder or '/'}"
        data = {"title": rec.title(), "warning": HANDOFF_WARNING, "target": target, "filename": doc.filename,
                "previous": ({"at": str(prev.get("at", "")), "same": prev.get("sha256") == doc.sha256,
                              "location": str(prev.get("location") or "")} if prev else None),
                "blocks": markdown_blocks(doc.markdown)}
        while True:
            res = _as_dict(self.broker.ask("handoff", data))
            if res.get("action") != "go":
                return False
            fut: Future[Any] = Future()

            def work(fut: Future[Any] = fut) -> None:
                try:
                    fut.set_result(perform(doc.sha256))
                except Exception as exc:  # shown to the user
                    fut.set_exception(exc)

            self.notify("Übergabe läuft", rec.title())
            threading.Thread(target=work, name="handoff", daemon=True).start()
            try:
                entry = self.broker.loop.wait(fut)
            except Exception as exc:
                data = {**data, "error": f"Übergabe fehlgeschlagen: {exc}"}
                continue
            self.notify("Übergeben", f"{rec.title()}: {entry.get('location', '')}")
            return True

    # -- calendar / re-transcription / updates -------------------------------------------
    def ask_event_for_recording(self, rec: Recording, events: list[Any]) -> tuple[Any, bool] | None:
        from callrec.ui.dto import event_choice

        start = rec.started_at()
        data = {"title": rec.title(), "when": f"{start:%d.%m.%Y, %H:%M} Uhr" if start else "",
                "events": [event_choice(ev) for ev in events]}
        res = _as_dict(self.broker.ask("event", data))
        idx = res.get("index")
        if not isinstance(idx, int) or not 0 <= idx < len(events):
            return None
        return events[idx], res.get("take_title") is not False

    def ask_retranscribe(self, rec: Recording, presets: list[str]) -> tuple[str, str | None] | None:
        res = _as_dict(self.broker.ask("retranscribe", {"title": rec.title(), "presets": list(presets)}))
        if res.get("ok") is not True:
            return None
        lang = res.get("language")
        preset = res.get("preset") or None
        if lang not in ("auto", "de", "en") or (preset is not None and preset not in presets):
            return None
        return str(lang), preset

    def ask_update(self, res: Any, busy: str, install_info: dict[str, Any]) -> str | None:
        from callrec.ui.dto import update_data

        ans = _as_dict(self.broker.ask("update", update_data(res, busy, install_info)))
        choice = ans.get("choice")
        return choice if choice in ("now", "after", "restore") else None

    # -- pages instead of modal windows ----------------------------------------------------
    def manage_profiles(self, profiles: Any, rebuild: Callable[[str], str] | None = None,
                        recordings_root: Any = None) -> None:
        self.navigate("settings", {"page": "profiles"})

    def edit_settings(self, base: Any, mics: list[str], outs: list[str], save: Callable[[Any], None],
                      initial_tab: str = "", autostart: Any = None) -> bool:
        from callrec.ui.settingsmodel import PAGE_BY_TAB

        self.navigate("settings", {"page": PAGE_BY_TAB.get(initial_tab, "general")})
        return False  # saved later from the page (bridge.save_settings -> AppActions.apply_saved_config)

    def edit_summary_prompts(self, test: Callable[[], object] | None = None) -> bool:
        self.navigate("prompts", {})
        return False

    def ask_yes_no(self, title: str, message_text: str, warning: bool = False) -> bool:
        return confirm(self.broker, title, message_text, "Ja", "Nein", warning=warning)

    def ask_retention(self, kind: str, text: str, items: list[tuple[str, str]], rt: Any) -> str:
        from callrec.retention import RETENTION_CHOICES, question_text

        title, message_text = question_text(kind, text, items, rt)
        res = _as_dict(self.broker.ask("choice", {"title": title, "message": message_text, "danger": "delete",
                                                  "choices": [{"key": k, "label": lbl} for k, lbl in RETENTION_CHOICES]}))
        choice = res.get("choice")
        return str(choice) if choice in {k for k, _l in RETENTION_CHOICES} else "later"


class SampleClips:
    """Speaker samples of the open review dialog as WAV clips (:class:`callrec.ui.clips.ClipStore`),
    served to the page by the loopback server only under an unguessable name; removed again (file and
    registration) when superseded, when the dialog closes and at the exit."""

    def __init__(self, publish: Callable[[Path], str], unpublish: Callable[[Path], None] = lambda p: None) -> None:
        from callrec.ui.clips import ClipStore

        self.publish = publish
        self.store = ClipStore(on_remove=unpublish)
        self._samples: dict[tuple[str, str], tuple[Path, str, list[Any], float]] = {}
        self._lock = threading.Lock()

    def register(self, rec: Recording, data: dict[str, Any]) -> None:
        with self._lock:
            for row in [data.get("me") or {}, *data.get("rows", [])]:
                samples = row.pop("_samples", None)
                track = str(row.pop("_track", "remote"))
                if samples:
                    self._samples[(rec.folder.name, str(row["id"]))] = (rec.folder, track, list(samples),
                                                                        float(row.get("talk_s") or 0.0))

    def url(self, rec_id: str, speaker_id: str, step: int = 0) -> dict[str, Any]:
        with self._lock:
            entry = self._samples.get((str(rec_id), str(speaker_id)))
        if entry is None:
            raise ValueError("Für diese Stimme gibt es keine Hörprobe.")
        folder, track, samples, _talk = entry
        clip = self.store.make(folder, track, samples, (str(rec_id), str(speaker_id)), 1 if step else 0)
        return {"url": self.publish(clip.path), "index": clip.index, "count": clip.count,
                "caption": clip.caption()}

    def release(self) -> None:
        """The review dialog closed: its clips go."""
        with self._lock:
            self._samples.clear()
        self.store.clear()

    def close(self) -> None:
        self.release()
        self.store.close()
