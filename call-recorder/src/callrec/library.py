"""The list of recordings as the main window (and ``callrec list``) shows it.

Reading a recording's state touches a handful of small files (meta.json, speakers.json,
summary.md); :class:`RecordingIndex` caches the result per folder and re-reads a folder only
when one of those files changed, so refreshing a list of hundreds of recordings costs a few
``stat`` calls per folder. Nothing in here imports Tk - the window runs it on a worker thread.

Also the two operations on a whole recording that are not pipeline steps: renaming (the
title only - the folder name is the stable ``recording_id`` of the SharePoint hand-over) and
deleting (to the Windows recycle bin where possible).
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from callrec.recording import Recording

log = logging.getLogger(__name__)

# stage states shown per column
OK, MISSING, RUNNING, FAILED, OPEN, NA = "ok", "missing", "running", "failed", "open", "na"

FILTERS = {
    "all": "Alle Aufnahmen",
    "no_summary": "Ohne Zusammenfassung",
    "speakers_open": "Sprecher noch zuordnen",
    "not_handed_over": "Noch nicht an SharePoint übergeben",
    "changed": "Seit der Übergabe geändert",
    "problems": "Mit Fehlern",
}


@dataclass
class RecordingInfo:
    folder: Path
    title: str
    started_at: datetime | None = None
    duration_s: float = 0.0
    language: str = ""  # "de", "en", "de+en" or "" (not transcribed yet)
    participants: list[str] = field(default_factory=list)
    recording_status: str = ""  # recording | stopped | recovered | failed
    processing_status: str = ""  # "" | running | done | cancelled | error: ...
    transcript_versions: int = 0
    has_transcript: bool = False
    speakers_unassigned: int | None = None  # None = speakers not determined yet
    speakers_spoken: int = 0
    has_summary: bool = False
    summary_versions: int = 0
    summary_problem: str = ""  # why the last summary attempt produced nothing
    handoff: dict[str, Any] | None = None  # last hand-over entry from meta.json
    summary_sha256: str = ""  # of the document that would be handed over now
    error: str = ""  # meta.json unreadable etc.
    chain_stage: str = ""  # guided processing after the recording (processing.chain.stage)
    handoff_skipped: str = ""  # why the chain did not hand over
    summary_outdated: bool = False  # the summary names speakers that were re-assigned since
    summary_missing: list[str] = field(default_factory=list)  # expected sections the summary lacks
    old_identification: bool = False  # speakers matched automatically under the rule of earlier versions
    capture_warnings: list[str] = field(default_factory=list)  # mic track checks (processing.identification)
    pinned: bool = False  # "Behalten": never deleted automatically
    removed: str = ""  # retention scope already removed ("audio" | "audio_transcript")
    removed_text: str = ""  # "Nur Zusammenfassung (Audio und Transkript gelöscht am …)"

    @property
    def has_audio(self) -> bool:
        return self.removed not in ("audio", "audio_transcript")

    @property
    def reduced(self) -> bool:
        """Only the summary is left (audio and transcript removed)."""
        return self.removed == "audio_transcript"

    @property
    def id(self) -> str:
        return self.folder.name

    @property
    def is_recording(self) -> bool:
        return self.recording_status == "recording" and not self.processing_status

    # -- stage states --------------------------------------------------
    @property
    def busy(self) -> bool:
        return self.processing_status == "running"

    @property
    def chain_active(self) -> bool:
        return self.chain_stage in ("process", "review", "summary", "handoff")

    @property
    def needs_processing(self) -> bool:
        """Stopped, never (successfully) processed and nothing running: offer "Jetzt verarbeiten"."""
        # "recording" with a processing decision: interrupted by a crash, then "Später" (not finalised yet)
        stopped = self.recording_status in ("stopped", "recovered") or (
            self.recording_status == "recording" and self.processing_status != "")
        return (stopped and not self.has_transcript and not self.busy
                and not self.chain_active and (self.processing_status in ("", "deferred", "cancelled")
                                               or self.processing_status.startswith("error")))

    def transcript_state(self) -> str:
        if self.has_transcript:
            return OK
        if self.busy:
            return RUNNING
        if self.processing_status.startswith("error"):
            return FAILED
        return MISSING

    def speakers_state(self) -> str:
        if self.speakers_unassigned is None:
            return RUNNING if self.busy else MISSING
        return OK if self.speakers_unassigned == 0 else OPEN

    def summary_state(self) -> str:
        if self.has_summary:
            return OK
        if self.busy:
            return RUNNING
        return FAILED if self.summary_problem else MISSING

    def handoff_state(self) -> str:
        if not self.handoff:
            return MISSING if self.has_summary else NA
        return OK if self.handoff_current else OPEN

    @property
    def handoff_current(self) -> bool:
        """The summary that would be handed over now equals the last handed-over one."""
        return bool(self.handoff) and bool(self.summary_sha256) and \
            (self.handoff or {}).get("sha256") == self.summary_sha256

    def has_problem(self) -> bool:
        return bool(self.error) or self.processing_status.startswith("error") or bool(self.summary_problem) \
            or self.recording_status == "failed"

    def search_text(self) -> str:
        return " ".join([self.title, self.folder.name, *self.participants]).casefold()


def _mtime(p: Path) -> float:
    try:
        return float(p.stat().st_mtime_ns)
    except OSError:
        return 0.0


def _signature(folder: Path) -> tuple[float, ...]:
    return tuple(_mtime(folder / n) for n in ("meta.json", "summary.md", "transcript.md", "data/speakers.json"))


def read_info(folder: Path) -> RecordingInfo:
    """Build the list entry of one recording (never raises)."""
    rec = Recording(folder)
    try:
        meta = rec.load_meta()
    except (OSError, ValueError) as exc:
        return RecordingInfo(folder, folder.name, error=f"meta.json nicht lesbar: {exc}")
    r = meta.get("recording") or {}
    proc = meta.get("processing") or {}
    versions = meta.get("versions") or {}
    started = None
    try:
        started = datetime.fromisoformat(r["started_at"]) if r.get("started_at") else None
    except (TypeError, ValueError):
        started = None
    tmeta = proc.get("transcription") or {}
    langs = [str((tmeta.get(t) or {}).get("language") or "") for t in ("mic", "remote")]
    language = "+".join(x for x in dict.fromkeys(langs) if x)
    info = RecordingInfo(
        folder=folder,
        title=str(meta.get("title") or folder.name),
        started_at=started,
        duration_s=float(r.get("duration_s") or 0.0),
        language=language,
        participants=[str(p.get("name") or p.get("email") or "") for p in meta.get("participants") or []
                      if isinstance(p, dict)],
        recording_status=str(r.get("status") or ""),
        processing_status=str(proc.get("status") or ""),
        transcript_versions=len(versions.get("transcript") or []),
        has_transcript=rec.transcript_path.exists(),
        summary_versions=len(versions.get("summary") or []),
        has_summary=rec.summary_path.exists(),
    )
    raw_chain = proc.get("chain")
    chain: dict[str, Any] = raw_chain if isinstance(raw_chain, dict) else {}
    info.chain_stage = str(chain.get("stage") or "")
    info.handoff_skipped = str(chain.get("handoff_skipped") or "")
    smeta = proc.get("summary") or {}
    if not info.has_summary and smeta.get("skipped"):
        info.summary_problem = str(smeta["skipped"])
    info.summary_outdated = info.has_summary and bool(smeta.get("speakers_outdated"))
    if info.has_summary and isinstance(smeta.get("missing_sections"), list):
        info.summary_missing = [str(x) for x in smeta["missing_sections"]]
    from callrec.retention import is_pinned, pending_scope, removed_scope, removed_text

    info.pinned = is_pinned(meta)
    pending = pending_scope(meta)
    # an incompletely deleted recording counts as "only the summary left": nothing may process it again
    info.removed = removed_scope(meta) or ({"audio": "audio"}.get(pending, "audio_transcript") if pending else "")
    info.removed_text = removed_text(meta)
    sp = proc.get("speakers")
    if info.reduced:
        pass  # no speaker data left: nothing to assign
    elif isinstance(sp, dict) and "unassigned" in sp:
        info.speakers_unassigned = int(sp.get("unassigned") or 0)
        info.speakers_spoken = int(sp.get("spoken") or 0)
    elif rec.speakers_path.exists():  # older recordings: approximate from speakers.json
        try:
            speakers = rec.load_speakers()
            remote = [s for s in speakers.values() if s.id not in ("ME", "S?")]
            info.speakers_unassigned = sum(1 for s in remote if not s.name and not s.minor)
            info.speakers_spoken = len(remote)
        except (OSError, ValueError, KeyError):
            pass
    ident = proc.get("identification")
    if isinstance(ident, dict):
        info.capture_warnings = [str(w) for w in ((ident.get("trackcheck") or {}).get("warnings") or [])]
    elif rec.speakers_path.exists():
        try:
            info.old_identification = any(s.source in ("auto", "manual") and s.name
                                          for s in rec.load_speakers().values())
        except (OSError, ValueError, KeyError):
            pass
    handoffs = meta.get("handoffs") or []
    info.handoff = dict(handoffs[-1]) if handoffs else None
    if info.has_summary and info.handoff:
        from callrec.handoff import HandoffError, build_summary_document

        try:
            info.summary_sha256 = build_summary_document(rec, False).sha256
            if info.handoff.get("sha256") != info.summary_sha256:
                # handed over with participant e-mails? compare that variant too
                alt = build_summary_document(rec, True).sha256
                if info.handoff.get("sha256") == alt:
                    info.summary_sha256 = alt
        except (HandoffError, OSError, ValueError) as exc:
            log.debug("Übergabe-Dokument nicht baubar (%s): %s", folder.name, exc)
    return info


class RecordingIndex:
    """Cached :func:`read_info` over all recording folders below ``root``."""

    def __init__(self, root: Callable[[], Path] | Path) -> None:
        self._root = root if callable(root) else (lambda: Path(root))
        self._cache: dict[Path, tuple[tuple[float, ...], RecordingInfo]] = {}
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root()

    def scan(self, batch: int = 0, on_batch: Callable[[list[RecordingInfo]], None] | None = None) -> list[RecordingInfo]:
        """All recordings, newest first. With ``on_batch`` the list so far is reported every
        ``batch`` folders, so a long list fills progressively."""
        root = self.root
        try:
            folders = [p for p in root.iterdir() if p.is_dir() and (p / "meta.json").exists()]
        except OSError:
            folders = []
        folders.sort(key=lambda p: p.name, reverse=True)
        out: list[RecordingInfo] = []
        with self._lock:
            for i, folder in enumerate(folders, 1):
                sig = _signature(folder)
                hit = self._cache.get(folder)
                if hit is None or hit[0] != sig:
                    hit = (sig, read_info(folder))
                    self._cache[folder] = hit
                out.append(hit[1])
                if on_batch is not None and batch and i % batch == 0 and i < len(folders):
                    on_batch(list(out))
            for gone in set(self._cache) - set(folders):
                del self._cache[gone]
        return out

    def invalidate(self, folder: Path | None = None) -> None:
        with self._lock:
            if folder is None:
                self._cache.clear()
            else:
                self._cache.pop(Path(folder), None)


def filter_infos(infos: Iterable[RecordingInfo], query: str = "", flt: str = "all") -> list[RecordingInfo]:
    words = [w for w in query.casefold().split() if w]
    out = []
    for i in infos:
        if words and not all(w in i.search_text() for w in words):
            continue
        if flt == "no_summary" and i.has_summary:
            continue
        if flt == "speakers_open" and i.speakers_state() != OPEN:
            continue
        if flt == "not_handed_over" and (not i.has_summary or i.handoff):
            continue
        if flt == "changed" and not (i.handoff and i.has_summary and not i.handoff_current):
            continue
        if flt == "problems" and not i.has_problem():
            continue
        out.append(i)
    return out


# ---------------------------------------------------------------------------
# display helpers (plain text; used by the window and the CLI)
# ---------------------------------------------------------------------------
def fmt_duration(seconds: float) -> str:
    s = int(round(seconds or 0))
    if s <= 0:
        return "–"
    if s < 60:
        return f"{s} s"
    h, m = divmod(int(round(s / 60)), 60)
    return f"{h}:{m:02d} h" if h else f"{m} min"


def fmt_language(code: str) -> str:
    names = {"de": "Deutsch", "en": "Englisch"}
    if not code:
        return "–"
    return " + ".join(names.get(c, c) for c in code.split("+"))


STAGE_TEXT = {
    "transcript": {OK: "✓", MISSING: "–", RUNNING: "…", FAILED: "Fehler"},
    "speakers": {OK: "✓", MISSING: "–", RUNNING: "…", OPEN: "offen"},
    "summary": {OK: "✓", MISSING: "–", RUNNING: "…", FAILED: "Fehler"},
    "handoff": {OK: "✓", MISSING: "–", OPEN: "geändert", NA: ""},
}


def stage_cells(info: RecordingInfo) -> tuple[str, str, str, str]:
    t = STAGE_TEXT["transcript"][info.transcript_state()]
    sp_state = info.speakers_state()
    sp = f"{info.speakers_unassigned} offen" if sp_state == OPEN else STAGE_TEXT["speakers"][sp_state]
    s = STAGE_TEXT["summary"][info.summary_state()]
    h_state = info.handoff_state()
    h = STAGE_TEXT["handoff"][h_state]
    if h_state == OK and info.handoff:
        at = _parse_dt(info.handoff.get("at"))
        h = f"✓ {at:%d.%m.}" if at else "✓"
    return t, sp, s, h


def _parse_dt(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def handoff_description(info: RecordingInfo) -> str:
    """One or two sentences for the details pane: when/where handed over, changed since?"""
    if not info.has_summary:
        return "SharePoint: Noch keine Zusammenfassung – es gibt nichts zu übergeben."
    if not info.handoff:
        return "SharePoint: Noch nicht übergeben."
    h = info.handoff
    at = _parse_dt(h.get("at"))
    when = f"{at:%d.%m.%Y um %H:%M} Uhr" if at else "zu unbekannter Zeit"
    where = h.get("location") or h.get("target") or "?"
    version = f" (Version {h['summary_version']})" if h.get("summary_version") else ""
    text = f"SharePoint: Zuletzt übergeben am {when}{version} nach {where}."
    if info.handoff_current:
        return text + " Die Zusammenfassung ist seitdem unverändert."
    return text + " Die Zusammenfassung hat sich seitdem geändert – bitte erneut übergeben."


def status_description(info: RecordingInfo) -> str:
    """Plain-German state of the processing for the details pane."""
    parts = []
    if info.error:
        return info.error
    if info.removed_text.startswith("Löschen unvollständig"):
        return info.removed_text + "."
    if info.removed_text:
        return info.removed_text + (" – die Zusammenfassung bleibt lesbar und kann übergeben werden."
                                    if info.reduced else " – Transkript und Zusammenfassung bleiben.")
    if info.recording_status == "recording" and not info.processing_status:
        return "Aufnahme läuft bzw. wurde unterbrochen (wird beim nächsten Start repariert)."
    if info.recording_status == "failed":
        return "Die Aufnahme konnte nicht gestartet werden (Audiogeräte?)."
    ps = info.processing_status
    stage_text = {"process": "Transkript und Sprecher", "review": "wartet auf die Sprecherzuordnung",
                  "summary": "Zusammenfassung", "handoff": "Übergabe an SharePoint"}
    if info.chain_active:
        parts.append(f"Verarbeitung nach der Aufnahme läuft: {stage_text.get(info.chain_stage, info.chain_stage)} "
                     "(wird nach einem Neustart fortgesetzt).")
    elif ps == "deferred":
        parts.append("Noch nicht verarbeitet („Später“ gewählt) – „Jetzt verarbeiten“ startet Transkript, "
                     "Sprecher und Zusammenfassung.")
    if info.handoff_skipped and not info.handoff:
        parts.append(f"Übergabe an SharePoint übersprungen: {info.handoff_skipped}")
    if info.chain_active or ps == "deferred":
        pass
    elif ps == "running":
        parts.append("Verarbeitung läuft (oder wurde unterbrochen und wird beim nächsten Start fortgesetzt).")
    elif ps == "cancelled":
        parts.append("Die letzte Verarbeitung wurde abgebrochen.")
    elif ps.startswith("error"):
        parts.append("Die letzte Verarbeitung ist fehlgeschlagen: " + ps.split(":", 1)[-1].strip())
    if info.summary_problem:
        parts.append("Zusammenfassung fehlt: " + info.summary_problem)
    if info.speakers_state() == OPEN:
        n = info.speakers_unassigned
        parts.append(f"{n} Stimme(n) noch keiner Person zugeordnet („Sprecher zuordnen“).")
    if info.summary_outdated:
        parts.append("Die Zusammenfassung beruht auf alten Sprechernamen – „Zusammenfassung neu erstellen“"
                     + (" und danach erneut an SharePoint übergeben." if info.handoff else "."))
    if info.summary_missing:
        parts.append("Der Zusammenfassung fehlen Abschnitte (" + ", ".join(info.summary_missing) + ") – Prompt prüfen.")
    if info.old_identification:
        parts.append("Die Sprecher wurden von einer früheren Version zugeordnet, die auch nicht eingeladene Personen "
                     "erkannt hat – „Weitere … → Sprecher neu erkennen“.")
    for w in info.capture_warnings:
        parts.append(f"Mikrofon prüfen: {w}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# rename / delete
# ---------------------------------------------------------------------------
class LibraryError(RuntimeError):
    pass


def rename_recording(rec: Recording, new_title: str) -> str:
    """Change the displayed title (meta.json). The folder keeps its name: it is the stable
    ``recording_id`` an assistant uses to recognise the same call in SharePoint."""
    title = " ".join(new_title.split())
    if not title:
        raise LibraryError("Der Titel darf nicht leer sein.")
    if len(title) > 200:
        raise LibraryError("Der Titel ist zu lang (höchstens 200 Zeichen).")
    old = rec.title()
    rec.update_meta(["title"], title)
    history = list(rec.load_meta().get("title_history") or [])
    history.append({"from": old, "to": title, "at": datetime.now().astimezone().isoformat(timespec="seconds")})
    rec.update_meta(["title_history"], history)
    return title


def assign_event(rec: Recording, event: Any, take_title: bool = True) -> list[str]:
    """Attach a calendar event to an existing recording ("Termin zuordnen").

    Its participants become the recording's participants (the prioritised candidates for voice
    matching and the names offered in the speaker assignment); people entered by hand before are
    kept. The folder name stays (stable ``recording_id``). Returns German messages."""
    from callrec.models import Person

    old: list[Person] = rec.participants()
    merged: list[Person] = list(event.participants())
    keys = {p.key() for p in merged}
    for p in old:
        if p.key() not in keys:
            merged.append(p)
            keys.add(p.key())
    rec.update_meta(["event"], event.to_dict())
    rec.update_meta(["participants"], [p.to_dict() for p in merged])
    rec.update_meta(["event_assigned"], {"at": datetime.now().astimezone().isoformat(timespec="seconds"),
                                         "event_id": event.id, "by": "user"})
    msgs = [f"Termin „{event.subject}“ zugeordnet: {len(merged)} Teilnehmende stehen für die Sprecherzuordnung bereit."]
    if take_title and event.subject.strip() and event.subject.strip() != rec.title():
        msgs.append(f"Titel geändert in „{rename_recording(rec, event.subject)}“.")
    return msgs


def _check_deletable(rec: Recording, root: Path) -> Path:
    folder = rec.folder.resolve()
    try:
        folder.relative_to(Path(root).resolve())
    except ValueError:
        raise LibraryError(f"{folder} liegt nicht im Aufnahmeordner - wird nicht gelöscht.") from None
    if folder == Path(root).resolve() or not (folder / "meta.json").exists():
        raise LibraryError(f"{folder} ist kein Aufnahmeordner - wird nicht gelöscht.")
    return folder


def recycle_bin_available() -> bool:
    return sys.platform == "win32"


def _send_to_recycle_bin(folder: Path) -> None:  # pragma: no cover - Windows only
    """SHFileOperationW with FOF_ALLOWUNDO. FOF_WANTNUKEWARNING makes Windows ask instead of
    silently deleting for good where there is no recycle bin (e.g. network drives)."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        if ctypes.sizeof(ctypes.c_void_p) == 4:  # shellapi.h packs its structs to 1 byte on 32-bit Windows
            _pack_ = 1
        _fields_ = [
            ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI, FOF_WANTNUKEWARNING = (
        3, 0x4, 0x10, 0x40, 0x400, 0x4000)
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = str(folder) + "\0"  # double-NUL terminated list
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING
    shfileop = ctypes.windll.shell32.SHFileOperationW  # type: ignore[attr-defined]
    shfileop.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    shfileop.restype = ctypes.c_int
    rc = shfileop(ctypes.byref(op))
    if rc != 0:
        raise LibraryError(f"Löschen fehlgeschlagen (Fehlercode {rc}). Ist eine Datei noch geöffnet (z. B. im Player)?")
    if op.fAnyOperationsAborted:
        raise LibraryError("Löschen abgebrochen - die Aufnahme ist unverändert.")


def delete_recording(rec: Recording, root: Path, permanent: bool = False,
                     recycler: Callable[[Path], None] | None = None) -> str:
    """Move the recording folder to the recycle bin (Windows) or, only with ``permanent=True``,
    delete it for good. Returns "recycled" or "deleted"."""
    folder = _check_deletable(rec, root)
    if not permanent:
        if recycler is None:
            if not recycle_bin_available():
                raise LibraryError("Kein Papierkorb verfügbar - nur endgültiges Löschen möglich.")
            recycler = _send_to_recycle_bin
        recycler(folder)
        if folder.exists():
            raise LibraryError("Der Ordner ist noch vorhanden - Löschen wurde nicht ausgeführt.")
        log.info("Aufnahme in den Papierkorb verschoben: %s", folder)
        return "recycled"

    # meta.json last: if a file is locked (player, virus scanner), the folder stays a recording that
    # is listed (marked as incompletely deleted by the caller) and can be deleted again - never an
    # invisible half-deleted folder without meta.json
    clear_readonly(folder)
    for entry in sorted(folder.iterdir()):
        if entry.name != "meta.json":
            unlink_tree(entry)
    unlink_tree(folder / "meta.json")
    folder.rmdir()
    log.info("Aufnahme endgültig gelöscht: %s", folder)
    return "deleted"


def is_link(p: Path) -> bool:
    """A symbolic link or (Windows) a directory junction - never followed when deleting."""
    try:
        return p.is_symlink() or bool(getattr(os.path, "isjunction", lambda _p: False)(p))
    except OSError:
        return False


def clear_readonly(top: Path) -> None:
    """Make the files below ``top`` deletable (read-only files, e.g. copied from a share) - without
    following links or junctions out of the folder."""
    if is_link(top) or not top.is_dir():
        return
    for dirpath, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if not is_link(Path(dirpath) / d)]
        for name in files:
            fp = os.path.join(dirpath, name)
            if os.path.islink(fp):
                continue
            try:
                os.chmod(fp, 0o600)
            except OSError:
                pass


def unlink_tree(p: Path) -> None:
    """Delete a file or folder for good. A link/junction is removed itself, never its target."""
    if is_link(p):
        try:
            os.unlink(p)
        except OSError:
            os.rmdir(p)  # Windows: a directory symlink or junction
        return
    if p.is_dir():
        clear_readonly(p)
        shutil.rmtree(p)
    elif p.exists():
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        p.unlink()


def info_lines(info: RecordingInfo) -> str:
    """One line for ``callrec list``."""
    t, sp, s, h = stage_cells(info)
    date = f"{info.started_at:%d.%m.%Y %H:%M}" if info.started_at else "–"
    return (f"{info.folder.name:<52} {date:<16} {fmt_duration(info.duration_s):>8}  "
            f"Transkript {t:<6} Sprecher {sp:<7} Zusammenfassung {s:<6} SharePoint {h or '–'}")
