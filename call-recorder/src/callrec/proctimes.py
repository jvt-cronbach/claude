"""Processing times: per recording (meta.json ``processing.timings``), a short history on this PC
(``processing_history.json``) and the estimate of the remaining time derived from it.

The estimate uses the measured time per second of call ("Echtzeitfaktor") of each stage on this
PC - the median of the last recordings, preferably with the same transcription profile - and
nothing else; before the first recording there is no estimate.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from callrec import paths

log = logging.getLogger(__name__)

HISTORY_MAX = 40
ESTIMATE_FROM = 10  # last N recordings per stage
STAGE_NAMES = {"finalize": "Audio", "transcribe": "Transkription", "diarize": "Sprechertrennung",
               "identify": "Sprechererkennung", "render": "Transkript", "summarize": "Zusammenfassung"}


def history_file() -> Path:
    return paths.data_dir() / "processing_history.json"


def fmt_duration(seconds: float) -> str:
    """"6 min 40 s", "45 s", "1 h 5 min"."""
    s = int(round(max(0.0, seconds)))
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s} s" if s else f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if m else f"{h} h"


def fmt_minutes(seconds: float) -> str:
    """Call length: "38 min", "1 h 5 min", "45 s"."""
    s = int(round(max(0.0, seconds)))
    if s < 60:
        return f"{s} s"
    m = int(round(s / 60))
    return f"{m} min" if m < 60 else (f"{m // 60} h {m % 60} min" if m % 60 else f"{m // 60} h")


def make_entry(steps_s: dict[str, float], total_s: float, audio_s: float, detail: dict[str, Any],
               meta: dict[str, Any], profile: str) -> dict[str, Any]:
    from callrec.power import power_state
    from callrec.speakers.threads import physical_cores

    pw = power_state()
    entry: dict[str, Any] = {
        "at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "audio_s": round(float(audio_s or 0.0), 1), "total_s": round(total_s, 1),
        "steps": {k: round(v, 1) for k, v in steps_s.items()}, "profile": profile,
        "cores": physical_cores(), "power": pw.to_dict(),
    }
    tr = detail.get("transcription") or {}
    if tr:
        entry["transcription"] = {k: tr.get(k) for k in ("load_s", "detect_s", "decode_s", "vad_s", "transcribe_s",
                                                         "wall_s", "rtf", "threads", "pieces") if k in tr}
    di = detail.get("diarization") or {}
    if di:
        entry["diarization"] = {k: di.get(k) for k in ("diarize_s", "embed_s", "trackcheck_s", "num_threads",
                                                       "parallel", "parallel_wall_s", "total_s") if k in di}
    if "mix_s" in detail:
        entry["mix_s"] = detail["mix_s"]
    summ = ((meta.get("processing") or {}).get("summary") or {})
    if "summarize" in steps_s and summ.get("waited_s"):
        entry["summary_waited_s"] = summ.get("waited_s")
    return entry


def load_history(path: Path | None = None) -> list[dict[str, Any]]:
    try:
        data = json.loads((path or history_file()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def append_history(entry: dict[str, Any], path: Path | None = None) -> None:
    path = path or history_file()
    items = (load_history(path) + [entry])[-HISTORY_MAX:]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def stage_rates(history: Iterable[dict[str, Any]], profile: str = "") -> dict[str, float]:
    """Seconds of processing per second of call, per stage (median of the last recordings)."""
    items = [e for e in history if float(e.get("audio_s") or 0) >= 30]
    rates: dict[str, float] = {}
    stages = {k for e in items for k in (e.get("steps") or {})}
    for st in stages:
        pool = [e for e in items if st in (e.get("steps") or {})]
        if st == "transcribe" and profile:
            same = [e for e in pool if e.get("profile") == profile]
            pool = same or pool
        vals = [float(e["steps"][st]) / float(e["audio_s"]) for e in pool[-ESTIMATE_FROM:]]
        if vals:
            rates[st] = statistics.median(vals)
    return rates


def estimate_remaining(steps: Iterable[str], current: str, fraction: float, audio_s: float,
                       rates: dict[str, float], current_elapsed_s: float = 0.0) -> float | None:
    """Seconds still to go for ``steps`` (in pipeline order), ``current`` running at ``fraction``.

    None when a stage that is still to come has never been measured on this PC."""
    from callrec.pipeline import STEPS

    if audio_s <= 0:
        return None
    order = [s for s in STEPS if s in set(steps)]
    if current not in order:
        return None
    rest = 0.0
    for st in order[order.index(current):]:
        rate = rates.get(st)
        if rate is None:
            if st == "summarize" or st in ("identify", "render", "finalize"):
                continue  # quick or not predictable (network)
            return None
        need = rate * audio_s
        if st == current:
            if 0.0 < fraction < 1.0 and current_elapsed_s > 5:
                need = max(need - current_elapsed_s, current_elapsed_s * (1 - fraction) / fraction)
            else:
                need = max(0.0, need - current_elapsed_s)
        rest += need
    return rest


def remaining_text(seconds: float | None) -> str:
    if seconds is None:
        return ""
    if seconds < 60:
        return "noch weniger als 1 min"
    return f"noch etwa {int(round(seconds / 60))} min"


def summary_text(entry: dict[str, Any]) -> str:
    """"Verarbeitung: 6 min 40 s für 38 min Gespräch (Transkription 4 min 10 s, ...)"."""
    steps = entry.get("steps") or {}
    if not steps:
        return ""
    di = entry.get("diarization") or {}
    parts = []
    for k, v in steps.items():
        if k == "diarize" and di.get("parallel") and di.get("parallel_wall_s") is not None:
            parts.append(f"Sprechertrennung {fmt_duration(float(di['parallel_wall_s']))} gleichzeitig")
        elif v >= 1.0:
            parts.append(f"{STAGE_NAMES.get(k, k)} {fmt_duration(v)}")
    audio = float(entry.get("audio_s") or 0.0)
    head = f"Verarbeitung: {fmt_duration(float(entry.get('total_s') or sum(steps.values())))}"
    if audio > 0:
        head += f" für {fmt_minutes(audio)} Gespräch"
    tr = entry.get("transcription") or {}
    th = tr.get("threads") or {}
    extra = []
    if th.get("total_threads"):
        lanes = int(th.get("lanes") or 1)
        extra.append(f"{lanes} Spuren × {th.get('threads_per_lane')} Threads" if lanes > 1
                     else f"{th.get('total_threads')} Threads")
    pw = entry.get("power") or {}
    if pw.get("saver"):
        extra.append("Energiesparmodus")
    elif pw.get("on_battery"):
        extra.append("Akkubetrieb")
    text = head + (f" ({', '.join(parts)})" if parts else "")
    return text + (f"; {', '.join(extra)}" if extra else "")


def recording_text(meta: dict[str, Any]) -> str:
    """For the recording details: the last full processing, else the last run."""
    proc = meta.get("processing") or {}
    entry = proc.get("timings_full") or proc.get("timings") or {}
    return summary_text(entry) if isinstance(entry, dict) else ""
