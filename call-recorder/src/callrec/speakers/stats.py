"""Per person: how often recognised automatically vs. named by you, what the profile holds.

This is the honest answer to "has it finished learning?": there is no end - a profile keeps a
reference per learned recording (different devices, rooms, codecs); what matters is whether the
person was recognised automatically in the last calls they took part in.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from callrec.speakers.profiles import AUTO_HOW, USER_SOURCES, VoiceProfile

HOW_TEXT = {"single": "automatisch (einzige weitere Person)", "profile": "automatisch (Stimmprofil)",
            "elimination": "automatisch (Ausschluss)", "always": "automatisch („immer erkennen“)",
            "confirmed": "automatisch, von Ihnen bestätigt", "manual": "von Ihnen zugeordnet",
            "corrected": "automatisch falsch – von Ihnen korrigiert"}
RECENT = 5


def _is_auto(h: dict[str, Any]) -> bool:
    return h.get("how") in AUTO_HOW or h.get("how") == "confirmed"


def profile_stats(p: VoiceProfile) -> dict[str, Any]:
    hist = [h for h in p.history if isinstance(h, dict)]
    by_how = Counter(str(h.get("how", "")) for h in hist)
    months: dict[str, Counter[str]] = {}
    for h in hist:
        m = str(h.get("at", ""))[:7] or "?"
        months.setdefault(m, Counter())["auto" if _is_auto(h) else "manual"] += 1
    # per recording the final outcome (a correction replaces the automatic entry of that speaker)
    per_rec: dict[tuple[str, str], dict[str, Any]] = {}
    for h in hist:
        per_rec[(str(h.get("recording", "")), str(h.get("speaker", "")))] = h
    recent = sorted(per_rec.values(), key=lambda h: str(h.get("at", "")))[-RECENT:]
    recent_auto = sum(1 for h in recent if _is_auto(h))
    refs = Counter(c.source for c in p.contributions)
    scores = [float(h["score"]) for h in recent if isinstance(h.get("score"), (int, float))]
    return {
        "id": p.id, "name": p.name, "mode": p.mode,
        "references": len(p.contributions), "user_references": sum(refs[s] for s in USER_SOURCES),
        "references_by_source": dict(refs), "speech_s": round(sum(c.seconds for c in p.contributions), 1),
        "aliases": [a.get("name") or a.get("email") for a in p.aliases],
        "outcomes": dict(by_how), "months": {m: dict(c) for m, c in sorted(months.items())},
        "recent": [{"at": h.get("at", ""), "how": h.get("how", ""), "score": h.get("score"),
                    "folder": h.get("folder", "")} for h in recent],
        "recent_auto": recent_auto, "recent_n": len(recent), "recent_scores": scores,
        "state": learning_state(len(recent), recent_auto, len(p.contributions)),
    }


def learning_state(n: int, auto: int, refs: int) -> str:
    if n == 0:
        return ("noch keine Erkennung aufgezeichnet (ab Version 0.4.1)" if refs else "noch nichts gelernt")
    if auto == n:
        return f"wird erkannt: zuletzt {auto} von {n} Gesprächen automatisch"
    if auto == 0:
        return f"wird noch nicht erkannt: zuletzt 0 von {n} Gesprächen automatisch (Begründung im Dialog „Sprecher zuordnen“)"
    return f"lernt noch: zuletzt {auto} von {n} Gesprächen automatisch"


def stats_lines(st: dict[str, Any]) -> list[str]:
    out = [f"{st['name']}: {st['state']}"]
    src = st["references_by_source"]
    out.append(f"  Stimmproben: {st['references']} ({st['user_references']} von Ihnen bestätigt"
               + (f", {src.get('sole', 0)} aus Gesprächen zu zweit" if src.get("sole") else "")
               + (f", {src.get('auto', 0)} automatisch" if src.get("auto") else "")
               + (f", {src.get('legacy', 0)} aus früherer Version" if src.get("legacy") else "")
               + f"), zusammen {st['speech_s']:.0f} s Sprache")
    if st["mode"]:
        out.append("  Einstellung: " + ("nie automatisch zuordnen" if st["mode"] == "never" else
                                       "immer erkennen, auch ohne Einladung"))
    if st["aliases"]:
        out.append("  auch bekannt als: " + ", ".join(str(a) for a in st["aliases"]))
    if st["outcomes"]:
        out.append("  bisher: " + ", ".join(f"{HOW_TEXT.get(k, k)} {v}×" for k, v in sorted(st["outcomes"].items())))
    for m, c in st["months"].items():
        out.append(f"  {m}: {c.get('auto', 0)}× automatisch, {c.get('manual', 0)}× von Ihnen")
    if st["recent_scores"]:
        out.append("  letzte Ähnlichkeiten: " + ", ".join(f"{s:.2f}" for s in st["recent_scores"]))
    return out
