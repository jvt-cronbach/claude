"""A small, anonymised diagnostics file of the speaker recognition of one recording.

What it contains: numbers and rule outcomes only - per remote voice the clean speech, whether it
is a fragment or probably your own voice, the rule that named it (or why not), the scores of the
candidates, the participant count and kinds, the speaker limit, the track-check metrics and the
settings in effect. What it never contains: audio, transcript text, voice embeddings, the title,
the folder name. Names and addresses are replaced by "Person 1", "Person 2" ... unless
``include_names`` is set (the user ticks it).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from callrec.recording import Recording


def _speaker_rows(rec: Recording) -> tuple[list[dict[str, Any]], set[str]]:
    ident = (rec.load_meta().get("processing") or {}).get("identification") or {}
    decisions = ident.get("decisions") or {}
    names: set[str] = set()
    rows = []
    for sid, s in rec.load_speakers().items():
        if sid == "ME":
            continue
        d = decisions.get(sid) or {}
        cands = []
        for c in d.get("candidates") or []:
            names.add(str(c.get("name") or ""))
            if c.get("profile_name"):
                names.add(str(c["profile_name"]))
            cands.append({"who": c.get("name"), "score": c.get("score"), "kind": c.get("kind"),
                          "expected": c.get("expected"), "profile": c.get("profile"),
                          "probable_score": c.get("probable_score"), "never": c.get("never")})
        for n in (s.name, s.suggested_name, s.preselect_name):
            if n:
                names.add(n)
        rows.append({
            "speaker": sid, "speech_s": (ident.get("speech_s") or {}).get(sid), "fragment": s.minor,
            "own_voice_suspected": s.self_suspect, "source": s.source, "rule": s.rule, "score": s.score,
            "named": s.name or "", "preselected": s.preselect_name or "", "preselect_score": s.preselect_score,
            "suggested": s.suggested_name or "", "suggested_score": s.suggested_score,
            "decision": d.get("decision", ""), "reason": d.get("reason") or s.note or "",
            "learned": d.get("learn", ""), "gap": d.get("gap"), "candidates": cands,
        })
    return rows, names


def build(rec: Recording, cfg: Any = None, include_names: bool = False) -> dict[str, Any]:
    from callrec import __version__

    meta = rec.load_meta()
    proc = meta.get("processing") or {}
    ident = proc.get("identification") or {}
    diar = proc.get("diarization") or {}
    bound = dict(diar.get("bound") or {})
    rows, names = _speaker_rows(rec)
    for p in meta.get("participants") or []:
        for k in ("name", "email"):
            if p.get(k):
                names.add(str(p[k]))
    tc = ident.get("trackcheck") or {}
    mic = ((meta.get("devices") or {}).get("mic") or {})
    data: dict[str, Any] = {
        "format": "callrec-speaker-diagnostics-1",
        "app_version": __version__,
        "recording": hashlib.sha256(rec.uid.encode()).hexdigest()[:12],
        "duration_s": (meta.get("recording") or {}).get("duration_s"),
        "rule": ident.get("rule", ""),
        "calendar_event": bool(meta.get("event")),
        "participants": ident.get("participants"),
        "expected_participants": ident.get("expected"),
        "participant_kinds": ident.get("participant_kinds"),
        "speaker_limit": {k: bound.get(k) for k in ("bound", "raw_speakers", "speakers", "lifted", "merged")},
        "fragments": len(ident.get("fragments") or []),
        "trackcheck": {"metrics": tc.get("metrics"), "own_voice_suspects": len(tc.get("self_suspects") or {}),
                       "warnings": len(tc.get("warnings") or []), "mic_unreliable": tc.get("mic_unreliable")},
        "mic": {"hands_free": any(k in str(mic.get("name", "")).casefold() for k in ("hands-free", "freisprech")),
                "sample_rate": mic.get("sample_rate")},
        "embedding_model": diar.get("embedding_model"),
        "speakers": rows,
        "settings": dict(vars(cfg.speakers)) if cfg is not None and hasattr(cfg, "speakers") else {},
        "names_included": include_names,
    }
    if not include_names:
        data = anonymise(data, names)
    return data


def anonymise(data: dict[str, Any], names: set[str]) -> dict[str, Any]:
    """Replace every known name/address (longest first) by "Person n" - also inside the reason texts."""
    ordered = sorted((n for n in names if n and len(n) >= 2), key=len, reverse=True)
    alias: dict[str, str] = {}
    for n in ordered:
        alias.setdefault(n.casefold(), f"Person {len(alias) + 1}")
    text = json.dumps(data, ensure_ascii=False)
    for n in ordered:
        text = re.sub(re.escape(json.dumps(n, ensure_ascii=False)[1:-1]), alias[n.casefold()], text, flags=re.IGNORECASE)
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "Adresse", text)  # any address left over
    return dict(json.loads(text))


def export(rec: Recording, out: Path, cfg: Any = None, include_names: bool = False) -> Path:
    out.write_text(json.dumps(build(rec, cfg, include_names), ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def default_path(rec: Recording, base: Path | None = None) -> Path:
    from callrec import paths

    folder = base or paths.data_dir() / "diagnostics"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"sprechererkennung-{hashlib.sha256(rec.uid.encode()).hexdigest()[:8]}.json"
