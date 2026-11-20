"""Toolkit-independent helpers of the main window (no tkinter import - testable anywhere).

- :func:`markdown_spans`: the small Markdown subset of transcript.md / summary.md as
  (text, tags) runs for a read-only Tk ``Text`` widget,
- window geometry: parsing, clamping onto the visible screen, persisting,
- version choices for the transcript/summary selector.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from callrec import paths
from callrec.library import OPEN, RecordingInfo

log = logging.getLogger(__name__)

Span = tuple[str, tuple[str, ...]]

_INLINE = re.compile(r"(\*\*[^*\n]+?\*\*|`[^`\n]+`|(?<![\w*])\*[^*\s][^*\n]*?\*(?![\w*])|(?<![\w])_[^_\s][^_\n]*?_(?![\w]))")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def inline_spans(text: str, base: tuple[str, ...] = ()) -> list[Span]:
    out: list[Span] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], base))
        tok = m.group(0)
        if tok.startswith("**"):
            out.append((tok[2:-2], (*base, "bold")))
        elif tok.startswith("`"):
            out.append((tok[1:-1], (*base, "code")))
        else:
            out.append((tok[1:-1], (*base, "italic")))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], base))
    return out


def _table_rows(lines: list[str]) -> list[list[str]]:
    rows = []
    for line in lines:
        if _TABLE_SEP.match(line):
            continue
        cells = line.strip().strip("|").split("|")
        rows.append([re.sub(r"\*\*(.+?)\*\*", r"\1", c.strip()) for c in cells])
    return rows


def _table_spans(lines: list[str]) -> list[Span]:
    rows = _table_rows(lines)
    if not rows:
        return []
    ncol = max(len(r) for r in rows)
    rows = [r + [""] * (ncol - len(r)) for r in rows]
    widths = [min(40, max(len(r[c]) for r in rows)) for c in range(ncol)]
    out: list[Span] = []
    for i, r in enumerate(rows):
        cells = []
        for c, cell in enumerate(r):
            cell = cell if len(cell) <= widths[c] else cell[: widths[c] - 1] + "…"
            cells.append(cell.ljust(widths[c]))
        out.append(("  ".join(cells).rstrip() + "\n", ("table", "bold") if i == 0 else ("table",)))
    return out


def _is_plain(line: str) -> bool:
    s = line.strip()
    return bool(s) and not (s.startswith(("#", "|", ">", "-", "*", "+", "_ _", "---")) or re.match(r"^\d+[.)]\s", s))


def markdown_spans(md: str) -> list[Span]:
    """Readable rendering of headings, lists, tables, rules, quotes and **bold**/*italic*/`code`."""
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[Span] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("|") and stripped.count("|") >= 2:
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            out.extend(_table_spans(block))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = min(len(m.group(1)), 3)
            out.extend(inline_spans(m.group(2).strip(), (f"h{level}",)))
            out.append(("\n", (f"h{level}",)))
        elif re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line):
            out.append(("─" * 40 + "\n", ("rule",)))
        elif re.match(r"^\s*[-*+]\s+", line):
            indent = len(line) - len(line.lstrip())
            body = re.sub(r"^\s*[-*+]\s+", "", line)
            tag = "bullet2" if indent >= 2 else "bullet"
            out.append(("• ", (tag,)))
            out.extend(inline_spans(body, (tag,)))
            out.append(("\n", (tag,)))
        elif re.match(r"^\s*\d+[.)]\s+", line):
            out.extend(inline_spans(line.strip(), ("bullet",)))
            out.append(("\n", ("bullet",)))
        elif stripped.startswith(">"):
            out.extend(inline_spans(stripped.lstrip("> ").rstrip(), ("quote",)))
            out.append(("\n", ("quote",)))
        elif stripped:
            # a paragraph: soft line breaks inside it are joined (the Text widget wraps itself)
            para = [stripped]
            while i + 1 < len(lines) and _is_plain(lines[i + 1]):
                i += 1
                para.append(lines[i].strip())
            out.extend(inline_spans(" ".join(para)))
            out.append(("\n", ()))
        else:
            out.append(("\n", ()))
        i += 1
    # collapse runs of more than one empty line
    merged: list[Span] = []
    blank = 0
    for text, tags in out:
        if text == "\n" and not tags:
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        merged.append((text, tags))
    return merged


def plain_text(spans: list[Span]) -> str:
    return "".join(t for t, _ in spans)


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------
_GEOM = re.compile(r"^(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)$")


def parse_geometry(text: str) -> tuple[int, int, int, int] | None:
    m = _GEOM.match((text or "").strip())
    if not m:
        return None
    # Tk writes "+-5" for x = -5 on a monitor left of the primary one
    return int(m.group(1)), int(m.group(2)), int(m.group(3).lstrip("+")), int(m.group(4).lstrip("+"))


# Tk's geometry is the client area: title bar (~31 px), menu bar (~20 px) and the taskbar
# (~48 px at 100 %) come on top of it on Windows.
FRAME_RESERVE = 160


def default_geometry(screen_w: int, screen_h: int, scale: float = 1.0) -> tuple[int, int, int, int]:
    """1120x700 logical pixels, but never larger than the screen minus taskbar, title and menu bar."""
    w = min(int(1120 * scale), screen_w - 40)
    h = min(int(700 * scale), screen_h - int(FRAME_RESERVE * scale))
    return w, h, max(0, (screen_w - w) // 2), max(0, (screen_h - h - int(130 * scale)) // 2)


def clamp_geometry(geom: tuple[int, int, int, int], screen_w: int, screen_h: int,
                   min_w: int, min_h: int) -> tuple[int, int, int, int]:
    """Keep a remembered window on the (current, possibly smaller) screen: no larger than the
    screen, title bar reachable. Multi-monitor setups with negative coordinates are reduced to
    the primary screen - Tk only reports that one."""
    w, h, x, y = geom
    usable_h = screen_h - FRAME_RESERVE
    w = max(min(w, screen_w), min(min_w, screen_w))
    h = max(min(h, usable_h), min(min_h, usable_h))
    x = min(max(0, x), max(0, screen_w - w))
    y = min(max(0, y), max(0, usable_h - h))
    return w, h, x, y


def format_geometry(geom: tuple[int, int, int, int]) -> str:
    w, h, x, y = geom
    return f"{w}x{h}+{x}+{y}"


@dataclass
class WindowState:
    geometry: str = ""
    zoomed: bool = False
    sash: int = 0  # width of the recording list (pixels)
    tab: str = ""
    filter: str = "all"
    compact: bool = True  # compact default view (record, current call, last recordings, processing)

    @staticmethod
    def file() -> Path:
        return paths.data_dir() / "window.json"

    @classmethod
    def load(cls, path: Path | None = None) -> WindowState:
        try:
            d: dict[str, Any] = json.loads((path or cls.file()).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        return cls(geometry=str(d.get("geometry") or ""), zoomed=bool(d.get("zoomed")), sash=int(d.get("sash") or 0),
                   tab=str(d.get("tab") or ""), filter=str(d.get("filter") or "all"),
                   compact=bool(d.get("compact", True)))

    def save(self, path: Path | None = None) -> None:
        p = path or self.file()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self.__dict__, ensure_ascii=False), encoding="utf-8")
        except OSError:
            log.debug("Fensterzustand nicht gespeichert", exc_info=True)


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VersionChoice:
    label: str
    path: Path


def version_choices(folder: Path, kind: str, versions: list[dict[str, Any]]) -> list[VersionChoice]:
    """Newest first. Without recorded versions the plain ``kind.md`` (if present) is the only choice."""
    out: list[VersionChoice] = []
    for v in sorted(versions, key=lambda v: int(v.get("version") or 0), reverse=True):
        n = int(v.get("version") or 0)
        p = folder / str(v.get("file") or f"{kind}.v{n}.md")
        if not p.exists():
            continue
        when = ""
        try:
            when = f" – {datetime.fromisoformat(str(v.get('updated') or v.get('created'))):%d.%m.%Y %H:%M}"
        except (TypeError, ValueError):
            pass
        extra = ""
        st = v.get("settings") or {}
        if kind == "transcript":
            preset = ((st.get("transcription") or {}).get("preset")) or st.get("preset")
            extra = f", Preset {preset}" if preset else ""
        elif kind == "summary":
            llm = st.get("llm") or {}
            model = llm.get("served_model") or llm.get("model")
            extra = f", {model}" if model else ""
        newest = " (neueste)" if not out else ""
        out.append(VersionChoice(f"Version {n}{newest}{when}{extra}", p))
    if not out and (folder / f"{kind}.md").exists():
        out.append(VersionChoice("aktuelle Fassung", folder / f"{kind}.md"))
    return out


def event_label(ev: Any, now: Any = None) -> str:
    from callrec.calendar.selection import demotion

    start, end = ev.start.astimezone(), ev.end.astimezone()
    online = " (Teams)" if getattr(ev, "is_online", False) else ""
    times = "ganztägig" if getattr(ev, "is_all_day", False) else f"{start:%H:%M}–{end:%H:%M}"
    if not getattr(ev, "is_all_day", False) and start.date() != end.date():
        times = f"{start:%d.%m. %H:%M}–{end:%d.%m. %H:%M}"
    marks = []
    why = demotion(ev)
    if why and why != "ganztägig":
        marks.append(why)
    if now is not None and ev.end < now:
        marks.append("vorbei")
    n = len(ev.participants()) if hasattr(ev, "participants") else 0
    people = f", {n} Pers." if n else ""
    return f"{times}  {ev.subject}{online}{people}" + (f"  [{', '.join(marks)}]" if marks else "")


MANUAL_EVENT = "Ohne Termin – Titel und Teilnehmende selbst eingeben"


def fmt_clock(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


# ---------------------------------------------------------------------------
# compact view: the single most relevant next action per recording
# ---------------------------------------------------------------------------
def next_action(info: RecordingInfo, busy: bool = False, recording_now: bool = False) -> tuple[str, str, str]:
    """(short status, button label, action key) - label/key empty when nothing is to do."""
    if info.error:
        return "Fehler", "", ""
    if recording_now:
        return "Aufnahme läuft", "", ""
    if busy or info.busy or info.chain_active:
        if info.chain_stage == "review":
            return "wartet auf Zuordnung", "Sprecher zuordnen", "review"
        return "wird verarbeitet …", "", ""
    if info.reduced:
        if info.has_summary and not info.handoff:
            return "Nur Zusammenfassung, nicht übergeben", "Übergeben", "handoff"
        return "Nur Zusammenfassung", "", ""
    if info.needs_processing and not info.has_audio:
        return "Audio gelöscht", "", ""
    if info.needs_processing:
        return "nicht verarbeitet", "Jetzt verarbeiten", "process_now"
    if info.speakers_state() == OPEN:
        return f"{info.speakers_unassigned} Stimme(n) offen", "Sprecher zuordnen", "review"
    if info.has_transcript and not info.has_summary:
        return "ohne Zusammenfassung", "Zusammenfassung erstellen", "resummarize"
    if info.has_summary and not info.handoff:
        return "fertig, nicht übergeben", "Übergeben", "handoff"
    if info.has_summary and info.handoff and not info.handoff_current:
        return "seit Übergabe geändert", "Erneut übergeben", "handoff"
    if info.has_summary:
        return "fertig und übergeben", "", ""
    return "–", "", ""


# ---------------------------------------------------------------------------
# Markdown as blocks (web UI): structure only, never HTML - the page sets text with textContent
# ---------------------------------------------------------------------------
Inline = list[list[Any]]  # [[text, "b"|"i"|"c"|""], ...]
_UTT = re.compile(r"^\*\*(?:\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*)?([^*\n]{1,200}?):\*\*\s?(.*)$")
_TAG_FLAG = {"bold": "b", "italic": "i", "code": "c"}


def inline_runs(text: str) -> Inline:
    out: Inline = []
    for chunk, tags in inline_spans(text):
        flag = "".join(_TAG_FLAG[t] for t in tags if t in _TAG_FLAG)
        if chunk:
            out.append([chunk, flag])
    return out


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def markdown_blocks(md: str, transcript: bool = False) -> list[dict[str, Any]]:
    """The Markdown subset of summary.md / transcript.md as a list of blocks:

    ``{"t": "h", "level": 1-3, "inl": ...}``, ``{"t": "p", "inl": ...}``, ``{"t": "li", "level": 0|1,
    "num": "1." | "", "inl": ...}``, ``{"t": "quote", "inl": ...}``, ``{"t": "rule"}``,
    ``{"t": "table", "head": [...], "rows": [[...]]}`` (cells as inline runs) and, in a transcript,
    ``{"t": "utt", "ts": "00:01:02", "who": "Name", "inl": ...}`` for every contribution."""
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("|") and stripped.count("|") >= 2:
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            rows = [_cells(b) for b in block if not _TABLE_SEP.match(b)]
            has_head = len(block) > 1 and bool(_TABLE_SEP.match(block[1]))
            head = rows[0] if has_head and rows else []
            body = rows[1:] if has_head else rows
            out.append({"t": "table", "head": [inline_runs(c) for c in head],
                        "rows": [[inline_runs(c) for c in r] for r in body]})
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if transcript and (u := _UTT.match(stripped)):
            out.append({"t": "utt", "ts": u.group(1) or "", "who": u.group(2).strip(), "inl": inline_runs(u.group(3))})
        elif m:
            out.append({"t": "h", "level": min(len(m.group(1)), 3), "inl": inline_runs(m.group(2).strip())})
        elif re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line):
            out.append({"t": "rule"})
        elif re.match(r"^\s*[-*+]\s+", line):
            indent = len(line) - len(line.lstrip())
            out.append({"t": "li", "level": 1 if indent >= 2 else 0, "num": "",
                        "inl": inline_runs(re.sub(r"^\s*[-*+]\s+", "", line))})
        elif n := re.match(r"^\s*(\d+[.)])\s+(.*)$", line):
            out.append({"t": "li", "level": 0, "num": n.group(1), "inl": inline_runs(n.group(2))})
        elif stripped.startswith(">"):
            out.append({"t": "quote", "inl": inline_runs(stripped.lstrip("> ").rstrip())})
        elif stripped:
            para = [stripped]
            while i + 1 < len(lines) and _is_plain(lines[i + 1]):
                i += 1
                para.append(lines[i].strip())
            out.append({"t": "p", "inl": inline_runs(" ".join(para))})
        i += 1
    return out


# ---------------------------------------------------------------------------
# per-recording actions (both main windows)
# ---------------------------------------------------------------------------
def action_states(info: RecordingInfo, recording_now: bool, busy: bool, calendar_connected: bool) -> dict[str, bool]:
    """Which actions of a recording are possible right now (see :func:`disabled_reasons` for why not)."""
    audio = info.has_audio
    material = not info.reduced  # transcript and speaker data still there
    return {
        "process_now": info.needs_processing and not busy and not recording_now and audio,
        "handoff": info.has_summary,
        "review": info.speakers_unassigned is not None and not busy and material,
        "resummarize": info.has_transcript and not recording_now and material,
        "retranscribe": info.recording_status in ("stopped", "recovered") and not recording_now and audio,
        "reprocess": info.recording_status in ("stopped", "recovered", "recording") and not recording_now and audio,
        "rename": not busy,
        "assign_event": not busy and not recording_now and calendar_connected and material,
        "participants": not busy and material,
        "recheck": not busy and info.speakers_unassigned is not None and material,
        "rerender": not busy and info.has_transcript and material,
        "speaker_limit": not busy and info.speakers_unassigned is not None and material and audio,
        "folder": True,
        "play": not recording_now and audio and (info.folder / "audio").is_dir(),
        "delete": not busy,
        "delete_audio": not busy and not recording_now and audio and info.recording_status != "recording",
        "delete_material": not busy and not recording_now and material and info.recording_status != "recording",
        "pin": True,
    }


def disabled_reasons(info: RecordingInfo) -> dict[str, str]:
    """Short explanation for actions that need material the recording no longer has."""
    out: dict[str, str] = {}
    if info.reduced:
        why = "Audio und Transkript dieser Aufnahme wurden gelöscht."
        for k in ("review", "resummarize", "retranscribe", "reprocess", "recheck", "rerender", "participants",
                  "assign_event", "speaker_limit", "play", "process_now", "delete_audio", "delete_material"):
            out[k] = why
    elif not info.has_audio:
        why = "Das Audio dieser Aufnahme wurde gelöscht."
        for k in ("retranscribe", "reprocess", "play", "process_now", "speaker_limit", "delete_audio"):
            out[k] = why
    return out


def status_tone(info: RecordingInfo, key: str, busy: bool = False, recording_now: bool = False) -> str:
    """Colour family of the status chip of ``next_action`` (never colour alone - text and icon too)."""
    if info.error or info.has_problem():
        return "err" if info.error or not key else "warn"
    if recording_now:
        return "rec"
    if busy or info.busy or info.chain_active:
        return "warn" if key == "review" else "info"
    if key == "handoff":
        return "warn" if info.handoff else "accent"
    if key:
        return "warn"
    return "ok" if info.has_summary else ""


# ---------------------------------------------------------------------------
# deleting (both main windows): one wording for the confirmation
# ---------------------------------------------------------------------------
DELETE_LABELS = {
    "recording": "Aufnahme vollständig löschen",
    "audio_transcript": "Audio und Transkript löschen (Zusammenfassung bleibt)",
    "audio": "Nur Audio löschen",
}


def delete_question(infos: list[RecordingInfo], scope: str, permanent: bool) -> tuple[str, str, str]:
    """(title, message, confirm button) for deleting ``scope`` of one or more recordings."""
    n = len(infos)
    what = f"„{infos[0].title}“" if n == 1 else f"{n} Aufnahmen"
    gone = {
        "recording": "Audio, Transkript, Zusammenfassung und Sprecherdaten (Stimmmerkmale dieses Gesprächs)",
        "audio_transcript": "Audio, alle Transkript-Versionen und alle Sprecher- und Erkennungsdaten – die "
                            "Zusammenfassung bleibt lesbar und übergebbar",
        "audio": "nur die Audiodateien – Transkript und Zusammenfassung bleiben (neu transkribieren und Hörproben "
                 "sind danach nicht mehr möglich)",
    }[scope]
    how = ("ENDGÜLTIG gelöscht – das lässt sich nicht rückgängig machen" if permanent
           else "in den Windows-Papierkorb verschoben (dort wiederherstellbar, bis er geleert wird)")
    msg = f"{what}: {gone}.\n\nDie Dateien werden {how}."
    if n > 1:  # name them: a selection can contain recordings that are scrolled or filtered away
        names = "\n".join(f"• {i.title}" + (f" ({i.started_at:%d.%m.%Y %H:%M})" if i.started_at else "")
                          for i in infos[:15])
        msg = f"{what}:\n{names}" + (f"\n• … und {n - 15} weitere" if n > 15 else "") + "\n\n" + \
            f"Gelöscht wird: {gone}.\n\nDie Dateien werden {how}."
    if any(i.handoff for i in infos):
        msg += "\n\nBereits an SharePoint übergebene Zusammenfassungen bleiben dort erhalten (CallRecorder löscht sie nicht)."
    if any(i.pinned for i in infos):
        msg += "\n\nDarunter ist eine mit „Behalten“ markierte Aufnahme."
    title = DELETE_LABELS[scope] + ("?" if scope != "recording" else "?")
    button = ("Endgültig löschen" if permanent else "In den Papierkorb") if scope == "recording" else (
        "Endgültig löschen" if permanent else "Löschen")
    return title, msg, button


def retention_badge(info: RecordingInfo, due_text: str = "") -> str:
    """Short marker for the list: "Behalten" / "wird in 2 Tagen gelöscht" / "Nur Zusammenfassung"."""
    if info.pinned:
        return "Behalten"
    if info.removed_text:
        if info.removed_text.startswith("Löschen unvollständig"):
            return "Löschen unvollständig"
        return "Nur Zusammenfassung" if info.reduced else "Audio gelöscht"
    return due_text


def retention_due_text(info: RecordingInfo, rt: Any, now: Any = None) -> str:
    """ "wird in 2 Tagen gelöscht" for the list and the details ("" = not soon / not at all)."""
    from datetime import datetime, timedelta

    from callrec.retention import MAX_AGE_DAYS, PRE_NOTICE_DAYS

    if rt is None or not rt.enabled or info.pinned or info.started_at is None:
        return ""
    pending = info.removed_text.startswith("Löschen unvollständig")
    if info.reduced and not pending:
        return ""
    if rt.scope == "audio" and not info.has_audio and not pending:
        return ""
    now = now or datetime.now().astimezone()
    start = info.started_at if info.started_at.tzinfo else info.started_at.astimezone()
    left = (start + timedelta(days=min(int(rt.max_age_days), MAX_AGE_DAYS)) - now).total_seconds() / 86400
    if pending:  # finished with the scope of the deletion that did not finish, not with the settings
        return "Löschen unvollständig: wird beim nächsten Lauf fertig gelöscht" if left <= 0 else ""
    what = {"audio": "Audio", "recording": "Aufnahme"}.get(rt.scope, "Audio und Transkript")
    if left <= 0:
        if info.is_recording or info.busy or info.chain_active:
            return f"{what}: wird gelöscht, sobald Aufnahme bzw. Verarbeitung fertig ist"
        if (not info.has_transcript and not info.removed) or info.processing_status.startswith("error"):
            # held back until it was shown as "wird gelöscht" for 24 h (retention.SURFACE_S)
            return f"{what}: nicht fertig verarbeitet – wird frühestens 24 h nach der Vorankündigung gelöscht"
        return f"{what}: wird beim nächsten Lauf gelöscht"
    if left < 1:
        return f"{what}: wird in den nächsten 24 h gelöscht"
    if left <= PRE_NOTICE_DAYS:
        n = int(left + 0.999)
        return f"{what}: wird in {n} Tagen gelöscht"
    return ""
