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

import copy
import json
import logging
import os
import threading
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
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


def pending_scope(meta: dict[str, Any]) -> str:
    """A deletion that started but did not finish (a file was locked): its scope, else ""."""
    r = meta.get("retention")
    return str(r.get("removing") or "") if isinstance(r, dict) and not r.get("removed") else ""


def removed_text(meta: dict[str, Any]) -> str:
    """ "Nur Zusammenfassung (Audio und Transkript gelöscht am 20.11.2026)" / "Audio gelöscht am …"."""
    raw = meta.get("retention")
    r: dict[str, Any] = raw if isinstance(raw, dict) else {}
    if pending_scope(meta):
        return ("Löschen unvollständig (eine Datei war gesperrt, z. B. durch einen Player) – wird erneut versucht; "
                "die Aufnahme kann nicht mehr verarbeitet werden")
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
def targets(rec: Recording, scope: str) -> list[Path]:
    """The files/folders ``scope`` removes from a recording (for ``recording``: the folder)."""
    f = rec.folder
    if scope == SCOPE_RECORDING:
        return [f]
    out = [f / "audio"]
    out += sorted(p for p in f.iterdir() if p.is_file() and p.suffix.lower() in (".wav", ".flac", ".opus", ".ogg"))
    if scope == SCOPE_AUDIO_TRANSCRIPT:
        out += sorted(f.glob("transcript*.md"))
        out += sorted(f.glob("transcript*.tmp"))  # left over by an interrupted atomic write
        out.append(f / "data")
    return [p for p in out if p.exists() or p.is_symlink()]


def reduced_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """meta.json without anything that contains or describes the conversation itself."""
    out = {k: v for k, v in meta.items() if k in KEEP_META}
    proc = meta.get("processing") or {}
    summ = proc.get("summary") if isinstance(proc, dict) else None
    if isinstance(summ, dict) and "skipped" in summ:
        # the reason a summary attempt failed can quote the request (e.g. a server's error body with
        # parts of the transcript): only a neutral note stays
        summ = {**summ, "skipped": "Zusammenfassung wurde nicht erstellt."}
    out["processing"] = {"status": "done"} | ({"summary": summ} if isinstance(summ, dict) else {})
    versions = meta.get("versions") or {}
    if isinstance(versions, dict) and versions.get("summary"):
        out["versions"] = {"summary": versions["summary"]}
    return out


def remove(rec: Recording, root: Path, scope: str, mode: str = "permanent", by: str = "user", reason: str = "",
           recycler: Callable[[Path], None] | None = None) -> str:
    """Remove ``scope`` of one recording (the caller checked that it is not busy). Returns a short
    German description of what happened."""
    from callrec.library import (
        _check_deletable,
        _send_to_recycle_bin,
        delete_recording,
        recycle_bin_available,
        unlink_tree,
    )
    from callrec.recording import _meta_lock

    if scope not in SCOPE_TEXT:
        raise RetentionError(f"Unbekannter Umfang: {scope}")
    folder = _check_deletable(rec, root)
    title = _title(rec)
    meta = rec.load_meta()
    if scope != SCOPE_RECORDING and (removed_scope(meta) == SCOPE_AUDIO_TRANSCRIPT
                                     or (scope == SCOPE_AUDIO and removed_scope(meta) == SCOPE_AUDIO)):
        return "bereits entfernt"
    items = targets(rec, scope) if scope != SCOPE_RECORDING else []
    if mode != "permanent" and recycler is None:
        if not recycle_bin_available():
            raise RetentionError("Kein Papierkorb verfügbar - nur endgültiges Löschen möglich.")
        recycler = _send_to_recycle_bin
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    old_entry = meta.get("retention")
    # first record that a deletion is under way: should a file be locked (player, virus scanner), the
    # recording ends up clearly marked as incompletely deleted (never processed again, deleted again
    # by the next run) - not half-deleted with a meta.json that looks untouched
    # (not for the whole folder into the recycle bin: one move, and a folder restored from there must
    # not carry the mark)
    marked = not (scope == SCOPE_RECORDING and mode != "permanent")
    # everything that could go, down to single files (a folder can be emptied only partly: one track
    # deleted, the other one locked by a player) - to tell "nothing removed" from "partly removed"
    before = _inventory(sorted(folder.iterdir()) if scope == SCOPE_RECORDING else items)
    if marked:
        _set_retention(rec, _meta_lock, {"removing": scope, "started": now, "by": by, "mode": mode})
    removed: list[str] = []
    try:
        if scope == SCOPE_RECORDING:
            how = delete_recording(rec, root, permanent=mode == "permanent", recycler=recycler)
            write_log({"folder": folder.name, "scope": scope, "mode": how, "by": by, "reason": reason})
            return "in den Papierkorb verschoben" if how == "recycled" else "endgültig gelöscht"
        for p in items:
            if mode == "permanent":
                unlink_tree(p)
            else:
                assert recycler is not None
                recycler(p)
                if p.exists() or p.is_symlink():
                    raise RetentionError(f"{p.name} ist noch vorhanden - nicht in den Papierkorb verschoben.")
            removed.append(p.name)
    except BaseException as exc:
        gone = bool(removed) or any(not os.path.lexists(p) for p in before)
        if marked and rec.meta_path.exists():
            if gone:
                _set_retention(rec, _meta_lock, {"removing": scope, "started": now, "by": by, "mode": mode,
                                                 "failed": str(exc)[:200], "items": removed})
            else:  # nothing was removed: the recording is unchanged
                _set_retention(rec, _meta_lock, old_entry)
        write_log({"folder": folder.name, "scope": scope, "mode": mode, "by": by, "reason": reason,
                   "error": str(exc)[:200]})
        if isinstance(exc, Exception) and not isinstance(exc, LibraryError):
            raise RetentionError(f"Löschen unvollständig: {exc}") from exc
        raise
    entry = {"removed": scope, "at": now, "by": by, "mode": mode, "items": removed}
    if reason:
        entry["reason"] = reason
    with _meta_lock:  # the newest meta.json (a rename meanwhile), reduced in one step
        meta = rec.load_meta()
        new = reduced_meta(meta) if scope == SCOPE_AUDIO_TRANSCRIPT else dict(meta)
        new["retention"] = entry
        if scope == SCOPE_AUDIO and isinstance(new.get("audio"), dict):
            new["audio"] = {**new["audio"], "deleted": True}
        rec.save_meta(new)
    write_log({"folder": folder.name, "scope": scope, "mode": mode, "by": by, "reason": reason})
    log.info("%s: %s entfernt (%s, %s)", folder.name, scope, mode, by)
    return f"{SCOPE_TEXT[scope].split(' (')[0]} von „{title}“ gelöscht"


def _inventory(items: list[Path]) -> list[str]:
    """``items`` and everything below them (links are listed, never followed)."""
    from callrec.library import is_link

    out: list[str] = []
    for p in items:
        out.append(str(p))
        if p.is_dir() and not is_link(p):
            for dirpath, dirs, files in os.walk(p):
                links = [d for d in dirs if is_link(Path(dirpath) / d)]
                dirs[:] = [d for d in dirs if d not in links]
                out.extend(os.path.join(dirpath, n) for n in [*links, *dirs, *files])
    return out


def _set_retention(rec: Recording, lock: Any, entry: Any) -> None:
    with lock:
        meta = rec.load_meta()
        if entry is None:
            meta.pop("retention", None)
        else:
            meta["retention"] = entry
        rec.save_meta(meta)


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
    scope: str = ""  # an incompletely deleted recording: the scope of that deletion (finished first)
    mode: str = ""
    unfinished: bool = False  # never (successfully) processed when planned

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
    candidates: int = 0  # recordings that still have something this scope would remove (not pinned)
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


def _busy_by_meta(meta: dict[str, Any]) -> bool:
    """Recorded, processed or in a post-call chain according to meta.json - also when another process
    (``callrec process`` on the command line, a second app) does it."""
    rs = str((meta.get("recording") or {}).get("status") or "")
    proc = meta.get("processing") or {}
    chain = proc.get("chain") if isinstance(proc.get("chain"), dict) else {}
    return rs == "recording" or str(proc.get("status") or "") == "running" or \
        str((chain or {}).get("stage") or "") in ("process", "review", "summary", "handoff")


LONG_GAP = timedelta(days=2)  # no run for longer than this: judge a large run against the remaining recordings
BUSY_TEXT = "wird gerade aufgenommen oder verarbeitet"
MAX_AGE_DAYS = 36500  # a larger value (config.toml) means "never" - and must not overflow the date arithmetic


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
    raw_surfaced = state.get("surfaced")
    surfaced: dict[str, Any] = raw_surfaced if isinstance(raw_surfaced, dict) else {}
    age = timedelta(days=min(int(rt.max_age_days), MAX_AGE_DAYS))
    newest: datetime | None = None
    from callrec.library import is_link

    try:
        inside = Path(root).resolve()
    except OSError:
        return out
    for rec in recs:
        try:
            # a link/junction to a folder elsewhere is never deleted (Controller.remove refuses it): not listed,
            # not asked about, not pinned through the question (which would write outside the recordings folder)
            if is_link(rec.folder) or rec.folder.resolve().parent != inside:
                continue
            meta = rec.load_meta()
            if "schema" not in meta:  # not written by CallRecorder (a recordings folder set to e.g. Documents)
                continue
            raw = (meta.get("recording") or {}).get("started_at")
            start = datetime.fromisoformat(raw) if isinstance(raw, str) and raw else None
            if start is None:  # no (readable) start: never treated as old
                continue
            start = start if start.tzinfo else start.astimezone()
            newest = start if newest is None or start > newest else newest
            done = removed_scope(meta)
            pending = pending_scope(meta)
            if is_pinned(meta) or (not pending and (done == SCOPE_AUDIO_TRANSCRIPT
                                                     or (rt.scope == SCOPE_AUDIO and done == SCOPE_AUDIO))):
                continue
            out.candidates += 1
            item = Item(rec, _title(rec), start, start + age, now >= start + age, warning=_warning(meta, rec, rt.scope))
            if pending:  # finish an incompletely deleted recording with that deletion's scope
                item.scope = pending
                r = meta.get("retention") or {}
                item.mode = "permanent" if r.get("mode") == "permanent" else rt.delete_mode
                item.warning = ""
            hold = busy(rec) or _busy_by_meta(meta)
            unfinished = item.unfinished = not pending and _unfinished(meta, rec)
        except (OSError, ValueError, TypeError, AttributeError):  # malformed meta.json: skipped, never deleted
            log.warning("Automatisches Löschen: %s übersprungen (meta.json unvollständig)", rec.folder.name)
            continue
        if not item.due:
            if item.due_at - now <= timedelta(days=PRE_NOTICE_DAYS):
                out.upcoming.append(item)
            continue
        if hold:
            item.waiting = BUSY_TEXT
        elif unfinished:
            seen = surfaced.get(item.id)
            try:
                shown_long = seen is not None and (now - datetime.fromisoformat(seen)).total_seconds() >= SURFACE_S
            except (TypeError, ValueError):
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
    elif len(out.removable) > 1 and _unusual(len(out.removable), out, rt.max_share_per_run, state, now):
        out.blocked, out.blocked_text = "share", (
            f"Der Lauf würde {len(out.removable)} von {out.total} Aufnahmen löschen – mehr als üblich "
            "(stimmt die Systemuhr?).")
    return out


def _unusual(n: int, p: Plan, share: float, state: dict[str, Any], now: datetime) -> bool:
    """More than ``share`` of the library at once - or, after the app did not run for a while (or the
    clock jumped ahead), of the recordings that still have something to remove: once most of the
    library is only summaries, the share of the whole library says nothing about a wrong clock."""
    if p.total and n / p.total > share:
        return True
    try:
        last = datetime.fromisoformat(str(state.get("last_run")))
        last = last if last.tzinfo else last.astimezone()
        gap = now - last
    except (TypeError, ValueError):
        gap = None
    long_gap = gap is None or gap > LONG_GAP or gap < -FUTURE_TOLERANCE
    return long_gap and bool(p.candidates) and n / p.candidates > share


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


_run_lock = threading.Lock()  # the start run, the daily one and "Wie eingestellt löschen" never overlap


def settings_key(cfg: Any, root: Path) -> tuple[Any, ...]:
    """What a deletion was decided (or confirmed) under: a run stops when this changes meanwhile."""
    rt = cfg.retention
    return (bool(rt.enabled), str(rt.scope), str(rt.delete_mode), rt.max_age_days, str(root))


def _current_key(cfg: Any, root: Path) -> tuple[Any, ...]:
    """:func:`settings_key` with the recordings folder the settings name now (``root`` without one)."""
    current = cfg.recordings_path() if callable(getattr(cfg, "recordings_path", None)) else root
    return settings_key(cfg, current)


def _recheck(item: Item, rt: Any, now: datetime, busy: Callable[[Recording], bool]) -> str:
    """Right before deleting: why ``item`` must not be removed any more ("" = still removable) - it may
    have been pinned, opened, queued or processed since the plan was made."""
    try:
        meta = item.rec.load_meta()
        raw = (meta.get("recording") or {}).get("started_at")
        start = datetime.fromisoformat(raw) if isinstance(raw, str) and raw else None
        if start is None:
            return "Beginn nicht lesbar"
        start = start if start.tzinfo else start.astimezone()
        if start != item.started_at or now < start + timedelta(days=min(int(rt.max_age_days), MAX_AGE_DAYS)):
            return "nicht mehr fällig"
        if is_pinned(meta):
            return "Behalten"
        if pending_scope(meta) != item.scope:
            return "Zustand geändert"
        done = removed_scope(meta)
        if not item.scope and (done == SCOPE_AUDIO_TRANSCRIPT or (rt.scope == SCOPE_AUDIO and done == SCOPE_AUDIO)):
            return "bereits entfernt"
        if busy(item.rec) or _busy_by_meta(meta):
            return BUSY_TEXT
        if not item.scope and not item.unfinished and _unfinished(meta, item.rec):
            return "Zustand geändert"
    except (OSError, ValueError, TypeError, AttributeError):
        return "meta.json nicht lesbar"
    return ""


def run(root: Path, cfg: Any, remover: Callable[[Recording, str, str], object], now: datetime | None = None,
        busy: Callable[[Recording], bool] = lambda r: False, confirmed: bool = False,
        only: Collection[str] | None = None, expect: tuple[Any, ...] | None = None) -> RunResult:
    """Remove what is due. ``remover(rec, scope, mode)`` is the app's delete path (Controller.remove,
    which refuses busy recordings). ``confirmed``: the user answered the question of a blocked run
    ("Wie eingestellt löschen"); with ``only`` (the ids that question listed) nothing else is removed
    by this run - a recording that became due while the question was open is not part of the answer -
    and with ``expect`` (:func:`settings_key` when the question was asked) nothing at all when the
    settings changed while it was open: the answer was given to what the question said.

    The run decides by the settings at its start and stops when they change meanwhile (settings saved,
    another recordings folder); each recording is checked again right before it is removed.

    The run itself does not count as "shown": an unprocessed recording is held back until a UI showed
    it for :data:`SURFACE_S` (:func:`mark_surfaced`). The first deletion of all is always asked."""
    now = now or datetime.now().astimezone()
    with _run_lock:
        rt = copy.copy(cfg.retention)  # the settings this run decides by
        key = settings_key(cfg, root)
        started = _current_key(cfg, root)
        res = RunResult()
        if expect is not None and tuple(expect) != key:
            res.blocked, res.blocked_text = "changed", (
                "Die Einstellungen zum Löschen wurden geändert, während die Frage offen war – nichts gelöscht.")
            return res
        snap = SimpleNamespace(retention=rt)
        state = load_state()
        p = plan(root, snap, now, busy, state)
        if not state.get("acknowledged") and rt.enabled and not p.candidates:
            state["acknowledged"] = True  # nothing that could ever be deleted yet: no question needed
            p = plan(root, snap, now, busy, state)
        if p.blocked and not (confirmed and p.blocked != "future"):
            res.blocked, res.blocked_text = p.blocked, p.blocked_text
            save_state(state)
            return res
        if confirmed:
            state["acknowledged"] = True
        wanted = set(only) if only is not None else None
        for item in p.removable:
            if wanted is not None and item.id not in wanted:
                continue
            if _current_key(cfg, root) != started:
                log.info("Automatisches Löschen angehalten: Einstellungen wurden während des Laufs geändert")
                break
            why = _recheck(item, rt, now, busy)
            if why:
                log.info("Automatisches Löschen: %s übersprungen (%s)", item.id, why)
                continue
            try:
                remover(item.rec, item.scope or rt.scope, item.mode or rt.delete_mode)
                res.removed.append(item.id)
                surfaced = state.get("surfaced")
                if isinstance(surfaced, dict):
                    surfaced.pop(item.id, None)
            except Exception as exc:  # one failure must not stop the others
                log.warning("Automatisches Löschen von %s fehlgeschlagen: %s", item.id, exc)
                res.failed.append((item.id, str(exc)))
                write_log({"folder": item.id, "scope": item.scope or rt.scope, "by": "retention",
                           "error": str(exc)[:200]})
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


RETENTION_CHOICES = (
    ("delete", "Wie eingestellt löschen"),
    ("keep", "Diese behalten (anpinnen)"),
    ("off", "Automatisches Löschen ausschalten"),
    ("later", "Später entscheiden"),
)


def question_text(kind: str, text: str, items: list[tuple[str, str]], rt: Any) -> tuple[str, str]:
    """(title, message) of the question of a run that did not delete (first activation / unusual run)."""
    names = "\n".join(f"• {t}" for t, _w in items[:12]) + (f"\n• … und {len(items) - 12} weitere" if len(items) > 12 else "")
    how = ("ENDGÜLTIG gelöscht (nicht rückgängig zu machen)" if rt.delete_mode == "permanent"
           else "in den Windows-Papierkorb verschoben")
    title = "Automatisches Löschen" if kind == "first" else "Ungewöhnlich viele Aufnahmen zum Löschen"
    intro = (f"Automatisches Löschen ist eingeschaltet: Aufnahmen älter als {rt.max_age_days} Tage werden gelöscht. "
             if kind == "first" else "")
    msg = (f"{intro}{text}\n\n{names}\n\nBei „Wie eingestellt löschen“ wird von diesen Aufnahmen "
           f"{SCOPE_TEXT.get(rt.scope, rt.scope)} {how}. Bereits an SharePoint übergebene Zusammenfassungen bleiben "
           "dort erhalten. Ohne Entscheidung wird nichts gelöscht.")
    return title, msg
