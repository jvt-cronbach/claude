"""JSON data for the web UI, built from the existing models (no toolkit, no business rules).

Everything the page shows comes from here; texts from recordings, calendar and transcripts are
plain strings here and are set with ``textContent`` on the page (never as HTML).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from callrec.library import (
    FILTERS,
    OPEN,
    RecordingInfo,
    filter_infos,
    fmt_duration,
    fmt_language,
    handoff_description,
    status_description,
)
from callrec.recording import Recording
from callrec.ui.viewmodel import action_states, markdown_blocks, next_action, status_tone, version_choices

log = logging.getLogger(__name__)

FILTER_ORDER = ("all", "speakers_open", "not_handed_over", "no_summary", "changed", "problems")
FILTER_SHORT = {"all": "Alle", "speakers_open": "Sprecher offen", "not_handed_over": "Nicht übergeben",
                "no_summary": "Ohne Zusammenfassung", "changed": "Seit Übergabe geändert", "problems": "Mit Fehlern"}


def iso(dt: datetime | None) -> str:
    return dt.astimezone().isoformat(timespec="seconds") if dt is not None else ""


def person(p: Any) -> dict[str, str]:
    return {"name": str(getattr(p, "name", "") or ""), "email": str(getattr(p, "email", "") or ""),
            "display": str(getattr(p, "display", "") or "")}


# ---------------------------------------------------------------------------
# recordings
# ---------------------------------------------------------------------------
def recording_row(info: RecordingInfo, busy: bool = False, recording_now: bool = False) -> dict[str, Any]:
    status, label, key = next_action(info, busy=busy, recording_now=recording_now)
    return {
        "id": info.id, "title": info.title, "started_at": iso(info.started_at), "duration_s": info.duration_s,
        "duration": fmt_duration(info.duration_s), "language": fmt_language(info.language),
        "participants": list(info.participants),
        "status": {"text": status, "tone": status_tone(info, key, busy, recording_now)},
        "action": {"key": key, "label": label} if key else None,
        "busy": busy or info.busy or info.chain_active, "recording": recording_now,
        "stages": {"transcript": info.transcript_state(), "speakers": info.speakers_state(),
                   "summary": info.summary_state(), "handoff": info.handoff_state()},
        "unassigned": info.speakers_unassigned,
    }


def recordings_list(infos: list[RecordingInfo], flt: str, query: str, busy_ids: set[str],
                    recording_id: str) -> dict[str, Any]:
    flt = flt if flt in FILTERS else "all"
    shown = filter_infos(infos, query or "", flt)
    counts = {k: len(filter_infos(infos, "", k)) for k in FILTER_ORDER}
    return {
        "items": [recording_row(i, i.id in busy_ids, i.id == recording_id) for i in shown],
        "total": len(infos), "filter": flt, "query": query or "",
        "filters": [{"key": k, "label": FILTER_SHORT[k], "count": counts[k]} for k in FILTER_ORDER],
    }


def _versions(info: RecordingInfo, meta: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    versions = meta.get("versions") or {}
    out: dict[str, list[dict[str, str]]] = {}
    for kind in ("summary", "transcript"):
        out[kind] = [{"label": c.label, "file": c.path.name}
                     for c in version_choices(info.folder, kind, list(versions.get(kind) or []))]
    return out


def recording_detail(info: RecordingInfo, busy: bool, recording_now: bool, calendar_connected: bool) -> dict[str, Any]:
    from callrec.ui.startflow import consent_summary

    rec = Recording(info.folder)
    try:
        meta = rec.load_meta()
    except (OSError, ValueError):
        meta = {}
    row = recording_row(info, busy, recording_now)
    h = info.handoff or {}
    bound = ((meta.get("processing") or {}).get("diarization") or {}).get("bound") or {}
    lifted = bool((meta.get("speaker_limit") or {}).get("lifted"))
    return {
        **row,
        "folder": info.folder.name,
        "status_text": status_description(info),
        "handoff": {"text": handoff_description(info), "done": bool(info.handoff), "current": info.handoff_current,
                    "at": str(h.get("at") or ""), "location": str(h.get("location") or h.get("target") or ""),
                    "version": h.get("summary_version")},
        "consent": consent_summary(meta),
        "actions": action_states(info, recording_now, busy, calendar_connected),
        "speaker_limit": {"merged": bool(bound.get("merged")), "lifted": lifted},
        "versions": _versions(info, meta),
        "summary_problem": info.summary_problem,
        "has_summary": info.has_summary, "has_transcript": info.has_transcript,
        "speakers_open": info.speakers_state() == OPEN,
    }


def read_text(folder: Path, kind: str, file: str) -> dict[str, Any]:
    """One version of summary/transcript as blocks. ``file`` must be one of the offered versions."""
    if kind not in ("summary", "transcript"):
        raise ValueError("Unbekannte Textart")
    rec = Recording(folder)
    try:
        meta = rec.load_meta()
    except (OSError, ValueError):
        meta = {}
    choices = version_choices(folder, kind, list((meta.get("versions") or {}).get(kind) or []))
    allowed = {c.path.name: c.path for c in choices}
    if not choices:
        return {"blocks": [], "empty": True}
    path = allowed.get(file) if file else choices[0].path
    if path is None:
        raise ValueError("Diese Version gibt es nicht")
    text = path.read_text(encoding="utf-8", errors="replace")
    blocks = markdown_blocks(text, transcript=kind == "transcript")
    return {"blocks": blocks, "file": path.name, "empty": False, "self": _self_name(meta, rec)}


def _self_name(meta: dict[str, Any], rec: Recording | None = None) -> str:
    """Name of the own (mic) track as the transcript shows it."""
    if rec is not None:
        try:
            from callrec.models import SELF_SPEAKER_ID

            me = rec.load_speakers().get(SELF_SPEAKER_ID)
            if me is not None and me.display:
                return str(me.display)
        except (OSError, ValueError, KeyError):
            pass
    s = meta.get("self") or {}
    return str(s.get("name") or "") if isinstance(s, dict) else ""


# ---------------------------------------------------------------------------
# dialogs
# ---------------------------------------------------------------------------
def after_recording_data(rec: Recording, plan: Any, handoff_problem: str, summary_problem: str) -> dict[str, Any]:
    try:
        title = rec.title()
    except (OSError, ValueError):
        title = rec.folder.name
    return {"title": title, "duration": fmt_duration(rec.duration()), "started_at": iso(rec.started_at()),
            "plan": {"transcript": plan.transcript, "speakers": plan.speakers, "summary": plan.summary,
                     "handoff": plan.handoff},
            "handoff_problem": handoff_problem, "summary_problem": summary_problem}


def speaker_review(rec: Recording, profiles: Any, speaker_cfg: Any, in_chain: bool = False, can_handoff: bool = False,
                   can_allow_more: bool = False) -> dict[str, Any]:
    from callrec.audio.finalize import find_audio
    from callrec.review import SELF_CHOICE, row_choices, self_overview, speaker_overview
    from callrec.transcript import fmt_ts

    meta = rec.load_meta()
    overview = speaker_overview(rec)
    me = self_overview(rec)
    audio = find_audio(rec.folder)
    all_profiles = getattr(speaker_cfg, "match_scope", "participants") == "all_profiles"
    warnings = list((((meta.get("processing") or {}).get("identification") or {}).get("trackcheck") or {})
                    .get("warnings") or [])
    bound = ((meta.get("processing") or {}).get("diarization") or {}).get("bound") or {}
    rows = []
    for ov in overview:
        choices = []
        for c in row_choices(rec, profiles, ov, all_profiles):
            if c == SELF_CHOICE:
                choices.append({"value": c, "label": "Das bin ich (eigene Stimme auf der Spur der Gegenseite)",
                                "kind": "self"})
            elif c.endswith(" (kein Teilnehmer dieses Termins)"):
                choices.append({"value": c, "label": c, "kind": "suggest"})
            else:
                choices.append({"value": c, "label": c, "kind": "participant"})
        initial = SELF_CHOICE if ov.source == "self_echo" else (ov.name or ov.preselect_name)
        rows.append({
            "id": ov.id, "label": ov.label, "name": ov.name, "initial": initial, "source": ov.source,
            "score": ov.score, "talk": fmt_ts(ov.talk_time_s, "mm:ss"), "talk_s": ov.talk_time_s,
            "utterances": ov.utterances, "sample": ov.sample_text, "note": ov.note if not ov.name else "",
            "suggested": ov.suggested_name, "self_suspect": ov.self_suspect,
            "keep": ov.source != "auto" and not ov.minor,  # an automatic match is only learned when confirmed here
            "can_play": audio is not None and bool(ov.samples),
            "minor": ov.minor, "speech": f"{ov.speech_s:.1f}".replace(".", ","),
            "sample_info": _sample_info(ov), "sample_count": len(ov.samples),
            "choices": choices, "_samples": ov.samples, "_track": "remote",
        })
    return {
        "id": rec.folder.name, "title": rec.title(), "in_chain": in_chain,
        "me": {"id": me.id, "name": me.name, "talk": fmt_ts(me.talk_time_s, "mm:ss"), "utterances": me.utterances,
               "sample": me.sample_text, "can_play": audio is not None and bool(me.samples),
               "sample_info": _sample_info(me), "_samples": me.samples, "_track": "mic"},
        "rows": rows, "warnings": warnings, "fragments": sum(1 for r in rows if r["minor"]),
        "bound": {"merged": bool(bound.get("merged")), "participants": bound.get("participants"),
                  "raw": bound.get("raw_speakers")} if can_allow_more and not in_chain else None,
        "can_handoff": can_handoff and not in_chain, "error": "",
    }


def _sample_info(ov: Any) -> str:
    if not ov.samples:
        return ""
    first = ov.samples[0]
    return f"{first.count} Beispiel(e) · {first.duration:.0f} s"


def event_choice(ev: Any) -> dict[str, Any]:
    from callrec.calendar.selection import demotion

    parts = ev.participants()
    return {"subject": ev.subject, "start": iso(ev.start), "end": iso(ev.end), "all_day": bool(ev.is_all_day),
            "online": bool(ev.is_online), "organizer": ev.organizer.display if ev.organizer else "",
            "people": [p.display for p in parts], "note": demotion(ev) or ""}


def update_data(res: Any, busy: str, install_info: dict[str, Any]) -> dict[str, Any]:
    from callrec.updater import version_text

    out: dict[str, Any] = {"installed": version_text(), "busy": busy,
                           "can_restore": bool(install_info.get("previous_commit")),
                           "previous": str(install_info.get("previous_version") or "")}
    if isinstance(res, Exception):
        out.update({"error": str(res), "available": False})
        return out
    rel = res.release
    out.update({"message": res.message, "available": bool(res.available and rel is not None),
                "requirements_changed": bool(res.requirements_changed),
                "release": {"version": rel.version, "commit": rel.commit[:10], "date": rel.date,
                            "notes": list(rel.notes)} if rel is not None else None})
    return out


# ---------------------------------------------------------------------------
# home
# ---------------------------------------------------------------------------
def calendar_events(events: list[Any], source: str, message: str, is_current: Any, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    items = []
    for i, ev in enumerate(events):
        d = event_choice(ev)
        d["index"] = i
        d["current"] = bool(is_current(ev)) if callable(is_current) else False
        d["past"] = ev.end < now
        items.append(d)
    return {"events": items, "source": source, "message": message}


def home(infos: list[RecordingInfo], busy_ids: set[str], recording_id: str, queue_snapshot: Any) -> dict[str, Any]:
    attention = []
    for info in infos:
        if info.id == recording_id:
            continue
        row = recording_row(info, info.id in busy_ids, False)
        act = row["action"]
        if info.has_problem() and not act:
            act = {"key": "open", "label": "Ansehen"}
        if (not act or (row["busy"] and act.get("key") != "review")) and not info.has_problem():
            continue
        reason = status_description(info) or row["status"]["text"]
        attention.append({**row, "reason": reason, "action": act})
        if len(attention) >= 8:
            break
    return {"attention": attention, "recent": [recording_row(i, i.id in busy_ids, i.id == recording_id)
                                               for i in infos[:5]],
            "work": queue_dto(queue_snapshot), "count": len(infos)}


def queue_dto(snap: Any) -> dict[str, Any]:
    from callrec.jobs import STEP_LABELS

    if snap is None:
        return {"current": None, "pending": []}
    cur = snap.current
    current = None
    if cur is not None:
        current = {"id": cur.id, "rec": cur.recording.folder.name, "title": cur.title, "step": cur.step,
                   "step_label": STEP_LABELS.get(cur.step, cur.step or "Startet"), "fraction": cur.fraction,
                   "message": cur.message, "line": cur.status_line(), "steps": sorted(cur.steps),
                   "cancel_requested": cur.cancel_requested}
    return {"current": current, "pending": [{"id": j.id, "rec": j.recording.folder.name, "title": j.title}
                                            for j in snap.pending]}


# ---------------------------------------------------------------------------
# setup checklist, profiles
# ---------------------------------------------------------------------------
def checks(items: list[Any], download_text: str = "") -> dict[str, Any]:
    from callrec.setupcheck import summary_line

    ready, sentence = summary_line(items)
    out = []
    for it in items:
        detail = it.detail + (f"\n{download_text}" if it.key == "whisper" and download_text else "")
        out.append({"key": it.key, "title": it.title, "state": it.state, "detail": detail, "required": it.required,
                    "actions": [{"label": t, "key": k} for t, k in it.actions]})
    return {"ready": ready, "summary": sentence, "items": out,
            "open": sum(1 for it in items if it.state in ("warn", "error"))}


def profiles(store: Any) -> list[dict[str, Any]]:
    from callrec.review import SOURCE_TEXT

    out = []
    for p in store.all_profiles():
        out.append({"id": p.id, "name": p.name, "email": p.email, "samples": p.samples, "model": p.model_id,
                    "updated": p.updated_at, "origins": [
                        {"index": i, "recording": (f"{c.title} ({c.folder})" if c.folder and c.title else
                                                   c.title or c.folder or ("–" if not c.recording else c.recording[:8])),
                         "speaker": c.speaker if c.recording else "–", "source": SOURCE_TEXT.get(c.source, c.source),
                         "seconds": round(c.seconds or 0.0), "at": c.at}
                        for i, c in enumerate(p.contributions)]})
    return out
