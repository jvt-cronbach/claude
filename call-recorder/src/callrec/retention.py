"""Deleting recordings: the one delete path (manual and automatic) and the retention rules.

What can be removed (``scope``):

* ``recording`` - the whole folder (audio, transcripts, summary, speaker data, meta.json);
* ``audio_transcript`` - audio, every transcript version and everything from which the conversation
  could be reconstructed (ASR results, archived ones, language detection, diarization, speaker data,
  per-recording voice embeddings - the whole ``data`` folder); kept: ``summary.md`` with its versions
  and a reduced ``meta.json`` (title, date, duration, participants, consent/start record, summary
  provider/model, hand-overs, a ``retention`` entry saying what was removed and when);
* ``audio`` - only the audio files (transcript and summary stay).

``permanent`` unlinks the files (ordinary deletion - no secure overwrite); ``recycle_bin`` moves
them to the Windows recycle bin. A summary already handed over to SharePoint is never touched.

The automatic run (:func:`plan` / :func:`run`) removes ``retention.scope`` from recordings older
than ``retention.max_age_days`` (from their start), except recordings marked "Behalten", busy ones
(recording, processing, queued, an unfinished post-call chain), and - until they were shown as
"wird gelöscht" for at least ``SURFACE_S`` - recordings whose processing never completed or failed.
It refuses to run (and asks) when a recording is dated in the future or more than
``max_share_per_run`` of the library would go at once (wrong system clock), and on the first
activation when recordings are already past the limit. A small log (what, when, why - no content)
is kept in ``retention.log``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from callrec import paths
from callrec.library import LibraryError
from callrec.recording import Recording, list_recordings

log = logging.getLogger(__name__)

SCOPE_RECORDING = "recording"
SCOPE_AUDIO_TRANSCRIPT = "audio_transcript"
SCOPE_AUDIO = "audio"
SCOPE_TEXT = {
    SCOPE_AUDIO_TRANSCRIPT: "Audio und Transkript (die Zusammenfassung bleibt)",
    SCOPE_RECORDING: "die ganze Aufnahme (auch die Zusammenfassung)",
    SCOPE_AUDIO: "nur das Audio (Transkript und Zusammenfassung bleiben)",
}
SURFACE_S = 24 * 3600  # an unprocessed/failed recording is shown this long as "wird gelöscht" first
PRE_NOTICE_DAYS = 3
FUTURE_TOLERANCE = timedelta(days=1)
# meta.json keys a reduced recording keeps (everything else may describe the conversation)
KEEP_META = ("schema", "app_version", "app_commit", "uid", "title", "title_history", "recording", "event",
             "event_assigned", "participants", "participants_history", "self", "consent", "devices", "handoffs",
             "keep", "retention")


class RetentionError(LibraryError):
    pass


# ---------------------------------------------------------------------------
# state of one recording
# ---------------------------------------------------------------------------
def is_pinned(meta: dict[str, Any]) -> bool:
    return meta.get("keep") is True


def set_pinned(rec: Recording, on: bool) -> None:
    """ "Behalten": never deleted automatically."""
    rec.update_meta(["keep"], bool(on))


def removed_scope(meta: dict[str, Any]) -> str:
    r = meta.get("retention")
    return str(r.get("removed") or "") if isinstance(r, dict) else ""


def has_audio(meta: dict[str, Any]) -> bool:
    return removed_scope(meta) not in (SCOPE_AUDIO, SCOPE_AUDIO_TRANSCRIPT)


def has_transcript_material(meta: dict[str, Any]) -> bool:
    return removed_scope(meta) != SCOPE_AUDIO_TRANSCRIPT


def removed_text(meta: dict[str, Any]) -> str:
    """ "Nur Zusammenfassung (Audio und Transkript gelöscht am 20.11.2026)" / "Audio gelöscht am …"."""
    r = meta.get("retention") if isinstance(meta.get("retention"), dict) else {}
    scope = removed_scope(meta)
    if not scope:
        return ""
    when = ""
    try:
        when = f" am {datetime.fromisoformat(str(r.get('at'))):%d.%m.%Y}"
    except (TypeError, ValueError):
        pass
    if scope == SCOPE_AUDIO_TRANSCRIPT:
        return f"Nur Zusammenfassung (Audio und Transkript gelöscht{when})"
    return f"Audio gelöscht{when}"


# ---------------------------------------------------------------------------
# the one delete path
# ---------------------------------------------------------------------------
def _unlink_tree(p: Path) -> None:
    if p.is_dir():
        for dirpath, _dirs, files in os.walk(p):  # read-only files (e.g. copied from a share)
            for name in files:
                try:
                    os.chmod(os.path.join(dirpath, name), 0o600)
                except OSError:
                    pass
        shutil.rmtree(p)
    elif p.exists():
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        p.unlink()


def targets(rec: Recording, scope: str) -> list[Path]:
    """The files/folders ``scope`` removes from a recording (for ``recording``: the folder)."""
    f = rec.folder
    if scope == SCOPE_RECORDING:
        return [f]
    out = [f / "audio"]
    out += sorted(p for p in f.iterdir() if p.is_file() and p.suffix.lower() in (".wav", ".flac", ".opus", ".ogg"))
    if scope == SCOPE_AUDIO_TRANSCRIPT:
        out += sorted(f.glob("transcript*.md"))
        out.append(f / "data")
    return [p for p in out if p.exists()]


def reduced_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """meta.json without anything that contains or describes the conversation itself."""
    out = {k: v for k, v in meta.items() if k in KEEP_META}
    proc = meta.get("processing") or {}
    summ = proc.get("summary") if isinstance(proc, dict) else None
    out["processing"] = {"status": "done"} | ({"summary": summ} if isinstance(summ, dict) else {})
    versions = meta.get("versions") or {}
    if isinstance(versions, dict) and versions.get("summary"):
        out["versions"] = {"summary": versions["summary"]}
    return out


def remove(rec: Recording, root: Path, scope: str, mode: str = "permanent", by: str = "user", reason: str = "",
           recycler: Callable[[Path], None] | None = None) -> str:
    """Remove ``scope`` of one recording (the caller checked that it is not busy). Returns a short
    German description of what happened."""
    from callrec.library import _check_deletable, _send_to_recycle_bin, delete_recording, recycle_bin_available

    if scope not in SCOPE_TEXT:
        raise RetentionError(f"Unbekannter Umfang: {scope}")
    folder = _check_deletable(rec, root)
    title = _title(rec)
    if scope == SCOPE_RECORDING:
        how = delete_recording(rec, root, permanent=mode == "permanent", recycler=recycler)
        write_log({"folder": folder.name, "scope": scope, "mode": how, "by": by, "reason": reason})
        return "in den Papierkorb verschoben" if how == "recycled" else "endgültig gelöscht"
    meta = rec.load_meta()
    if removed_scope(meta) == SCOPE_AUDIO_TRANSCRIPT or (scope == SCOPE_AUDIO and removed_scope(meta) == SCOPE_AUDIO):
        return "bereits entfernt"
    items = targets(rec, scope)
    if mode != "permanent":
        if recycler is None:
            if not recycle_bin_available():
                raise RetentionError("Kein Papierkorb verfügbar - nur endgültiges Löschen möglich.")
            recycler = _send_to_recycle_bin
    removed = []
    for p in items:
        if mode == "permanent":
            _unlink_tree(p)
        else:
            assert recycler is not None
            recycler(p)
        removed.append(p.name)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    entry = {"removed": scope, "at": now, "by": by, "mode": mode, "items": removed}
    if reason:
        entry["reason"] = reason
    new = reduced_meta(meta) if scope == SCOPE_AUDIO_TRANSCRIPT else dict(meta)
    new["retention"] = entry
    if scope == SCOPE_AUDIO and isinstance(new.get("audio"), dict):
        new["audio"] = {**new["audio"], "deleted": True}
    rec.save_meta(new)
    write_log({"folder": folder.name, "scope": scope, "mode": mode, "by": by, "reason": reason})
    log.info("%s: %s entfernt (%s, %s)", folder.name, scope, mode, by)
    return f"{SCOPE_TEXT[scope].split(' (')[0]} von „{title}“ gelöscht"


def _title(rec: Recording) -> str:
    try:
        return rec.title()
    except (OSError, ValueError):
        return rec.folder.name


# ---------------------------------------------------------------------------
# log and state
# ---------------------------------------------------------------------------
def log_path() -> Path:
    return paths.data_dir() / "retention.log"


def write_log(entry: dict[str, Any]) -> None:
    """One JSON line: what (folder name), when, why - never content."""
    try:
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now().astimezone().isoformat(timespec="seconds"), **entry},
                                ensure_ascii=False) + "\n")
    except OSError:
        log.warning("Löschprotokoll nicht geschrieben", exc_info=True)


def read_log(limit: int = 200) -> list[dict[str, Any]]:
    try:
        lines = log_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _state_path() -> Path:
    return paths.data_dir() / "retention_state.json"


def load_state() -> dict[str, Any]:
    try:
        d = json.loads(_state_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        log.warning("Zustand des automatischen Löschens nicht gespeichert", exc_info=True)


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------
@dataclass
class Item:
    rec: Recording
    title: str
    started_at: datetime
    due_at: datetime
    due: bool  # past the age now
    waiting: str = ""  # why it is not removed yet although due ("" = removable)
    warning: str = ""  # e.g. "Zusammenfassung fehlt – vor dem Löschen erstellen?"

    @property
    def id(self) -> str:
        return self.rec.folder.name

    def when_text(self, now: datetime) -> str:
        if self.due:
            return "wird beim nächsten Lauf gelöscht" if not self.waiting else f"wird gelöscht ({self.waiting})"
        days = (self.due_at - now).total_seconds() / 86400
        if days < 1:
            return "wird in den nächsten 24 h gelöscht"
        n = int(days + 0.999)
        return f"wird in {n} Tag{'en' if n != 1 else ''} gelöscht"


@dataclass
class Plan:
    enabled: bool
    scope: str
    total: int = 0
    removable: list[Item] = field(default_factory=list)  # removed by a run now
    waiting: list[Item] = field(default_factory=list)  # due, but held back (shown first / busy)
    upcoming: list[Item] = field(default_factory=list)  # due within PRE_NOTICE_DAYS
    blocked: str = ""  # "" | "first" | "future" | "share": the run asks instead
    blocked_text: str = ""

    def next_24h(self, now: datetime) -> list[Item]:
        return [i for i in self.removable + self.waiting + self.upcoming if i.due_at - now <= timedelta(days=1)]

    def notice(self, now: datetime) -> str:
        n = len(self.next_24h(now))
        if not self.enabled or not n:
            return ""
        return f"{n} Aufnahme{'n' if n != 1 else ''} {'werden' if n != 1 else 'wird'} in den nächsten 24 h gelöscht"


def _unfinished(meta: dict[str, Any], rec: Recording) -> bool:
    proc = meta.get("processing") or {}
    status = str(proc.get("status") or "")
    return not rec.transcript_path.exists() and not removed_scope(meta) or status.startswith("error")


def _warning(meta: dict[str, Any], rec: Recording, scope: str) -> str:
    if scope == SCOPE_AUDIO:
        return ""
    proc = meta.get("processing") or {}
    summ = proc.get("summary") or {}
    if not rec.summary_path.exists():
        why = "fehlgeschlagen" if summ.get("status") == "failed" else "fehlt"
        return f"Zusammenfassung {why} – vor dem Löschen erstellen?"
    sp = proc.get("speakers") or {}
    if isinstance(sp, dict) and int(sp.get("unassigned") or 0) > 0:
        return "Sprecher noch nicht zugeordnet – vor dem Löschen zuordnen?"
    return ""


def plan(root: Path, cfg: Any, now: datetime | None = None, busy: Callable[[Recording], bool] = lambda r: False,
         state: dict[str, Any] | None = None) -> Plan:
    rt = cfg.retention
    now = now or datetime.now().astimezone()
    state = load_state() if state is None else state
    out = Plan(bool(rt.enabled), rt.scope)
    recs = list_recordings(root)
    out.total = len(recs)
    if not rt.enabled:
        return out
    surfaced = state.get("surfaced") or {}
    age = timedelta(days=rt.max_age_days)
    newest: datetime | None = None
    for rec in recs:
        try:
            meta = rec.load_meta()
            start = rec.started_at()
        except (OSError, ValueError):
            continue
        if start is None:
            continue
        start = start if start.tzinfo else start.astimezone()
        newest = start if newest is None or start > newest else newest
        done = removed_scope(meta)
        if is_pinned(meta) or done == SCOPE_AUDIO_TRANSCRIPT or (rt.scope == SCOPE_AUDIO and done == SCOPE_AUDIO):
            continue
        item = Item(rec, _title(rec), start, start + age, now >= start + age, warning=_warning(meta, rec, rt.scope))
        if not item.due:
            if item.due_at - now <= timedelta(days=PRE_NOTICE_DAYS):
                out.upcoming.append(item)
            continue
        rs = str((meta.get("recording") or {}).get("status") or "")
        chain = ((meta.get("processing") or {}).get("chain") or {}).get("stage") or ""
        if busy(rec) or rs == "recording" or chain in ("process", "review", "summary", "handoff"):
            item.waiting = "wird gerade aufgenommen oder verarbeitet"
        elif _unfinished(meta, rec):
            seen = surfaced.get(item.id)
            try:
                shown_long = seen is not None and (now - datetime.fromisoformat(seen)).total_seconds() >= SURFACE_S
            except ValueError:
                shown_long = False
            if not shown_long:
                item.waiting = "nicht fertig verarbeitet – frühestens nach 24 h Vorankündigung"
        (out.waiting if item.waiting else out.removable).append(item)
    if newest is not None and newest > now + FUTURE_TOLERANCE:
        out.blocked, out.blocked_text = "future", (
            "Eine Aufnahme ist auf ein Datum in der Zukunft datiert – die Systemuhr stimmt vermutlich nicht. "
            "Automatisches Löschen ausgesetzt.")
    elif not state.get("acknowledged") and out.removable:
        out.blocked, out.blocked_text = "first", (
            f"{len(out.removable)} vorhandene Aufnahme(n) sind älter als {rt.max_age_days} Tage.")
    elif out.removable and out.total and len(out.removable) / out.total > rt.max_share_per_run and len(out.removable) > 1:
        out.blocked, out.blocked_text = "share", (
            f"Der Lauf würde {len(out.removable)} von {out.total} Aufnahmen löschen – mehr als üblich "
            "(stimmt die Systemuhr?).")
    return out


def mark_surfaced(p: Plan, now: datetime | None = None, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """The plan was shown to the user: remember since when each due/upcoming item has been visible."""
    now = now or datetime.now().astimezone()
    st = load_state() if state is None else state
    surfaced = dict(st.get("surfaced") or {})
    for i in p.removable + p.waiting + p.upcoming:
        surfaced.setdefault(i.id, now.isoformat(timespec="seconds"))
    st["surfaced"] = surfaced
    if state is None:
        save_state(st)
    return st


@dataclass
class RunResult:
    removed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    blocked: str = ""
    blocked_text: str = ""


def run(root: Path, cfg: Any, remover: Callable[[Recording, str, str], object], now: datetime | None = None,
        busy: Callable[[Recording], bool] = lambda r: False, confirmed: bool = False) -> RunResult:
    """Remove what is due. ``remover(rec, scope, mode)`` is the app's delete path (Controller.remove,
    which refuses busy recordings). ``confirmed``: the user answered the question of a blocked run
    ("Wie eingestellt löschen")."""
    now = now or datetime.now().astimezone()
    state = load_state()
    if not state.get("acknowledged"):
        p0 = plan(root, cfg, now, busy, state)
        if not p0.removable:  # nothing old at the first activation: no question needed
            state["acknowledged"] = True
    state = mark_surfaced(plan(root, cfg, now, busy, state), now, state)
    p = plan(root, cfg, now, busy, state)
    res = RunResult()
    if p.blocked and not (confirmed and p.blocked != "future"):
        res.blocked, res.blocked_text = p.blocked, p.blocked_text
        save_state(state)
        return res
    if confirmed:
        state["acknowledged"] = True
    for item in p.removable:
        try:
            remover(item.rec, cfg.retention.scope, cfg.retention.delete_mode)
            res.removed.append(item.id)
            (state.get("surfaced") or {}).pop(item.id, None)
        except Exception as exc:  # one failure must not stop the others
            log.warning("Automatisches Löschen von %s fehlgeschlagen: %s", item.id, exc)
            res.failed.append((item.id, str(exc)))
            write_log({"folder": item.id, "scope": cfg.retention.scope, "by": "retention", "error": str(exc)[:200]})
    state["last_run"] = now.isoformat(timespec="seconds")
    save_state(state)
    return res


def pin_all(items: list[Item]) -> None:
    for i in items:
        set_pinned(i.rec, True)


def acknowledge() -> None:
    st = load_state()
    st["acknowledged"] = True
    save_state(st)


def notify_due_today(p: Plan, now: datetime | None = None) -> str:
    """The once-a-day notification text ("" = nothing due or already notified today)."""
    now = now or datetime.now().astimezone()
    text = p.notice(now)
    st = load_state()
    day = now.date().isoformat()
    if not text or st.get("notified_day") == day:
        return ""
    st["notified_day"] = day
    save_state(st)
    return text


def resolve_folder(root: Path, name: str) -> Recording:
    p = Path(name)
    folder = p if p.is_absolute() else Path(root) / p
    rec = Recording(folder)
    if not rec.meta_path.exists():
        raise RetentionError(f"Keine Aufnahme: {folder}")
    return rec


def all_recordings(root: Path) -> list[Recording]:
    return list_recordings(root)
