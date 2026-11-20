"""Post-check of a Whisper result: flag suspicious repetitions instead of silently deleting them.

A wrong language or a decoding loop shows up as one short phrase repeated many times in a row
("mit Ihnen, mit Ihnen, mit Ihnen, ..."). Such places are reported (transcript header, meta.json,
log) so the user can re-transcribe - the text itself is left untouched.
"""

from __future__ import annotations

import re
from typing import Any

from callrec.models import AsrSegment

_WORD_RE = re.compile(r"[\w'’-]+", re.UNICODE)
MAX_PHRASE_WORDS = 8
SINGLE_WORD_MIN_REPEATS = 8  # "ja ja ja ja" is normal speech; eight identical words in a row are not


def _tokens(segments: list[AsrSegment]) -> list[tuple[str, float, str]]:
    """(normalised word, start time, original word)"""
    out: list[tuple[str, float, str]] = []
    for seg in segments:
        if seg.words:
            for w in seg.words:
                for m in _WORD_RE.finditer(w.text):
                    out.append((m.group(0).lower(), float(w.start), m.group(0)))
        else:
            for m in _WORD_RE.finditer(seg.text):
                out.append((m.group(0).lower(), float(seg.start), m.group(0)))
    return out


def find_repetitions(segments: list[AsrSegment], min_repeats: int = 4,
                     max_phrase_words: int = MAX_PHRASE_WORDS) -> list[dict[str, Any]]:
    """Phrases (1-8 words) repeated at least ``min_repeats`` times back to back."""
    if min_repeats <= 0:
        return []
    toks = _tokens(segments)
    words = [t[0] for t in toks]
    found: list[dict[str, Any]] = []
    i = 0
    while i < len(words):
        hit = None
        for n in range(1, max_phrase_words + 1):
            if i + 2 * n > len(words):
                break
            phrase = words[i:i + n]
            count = 1
            while words[i + count * n:i + (count + 1) * n] == phrase:
                count += 1
            needed = max(min_repeats, SINGLE_WORD_MIN_REPEATS) if n == 1 else min_repeats
            if count >= needed:
                hit = (n, count)
                break
        if hit is None:
            i += 1
            continue
        n, count = hit
        found.append({
            "start": round(toks[i][1], 2),
            "end": round(toks[min(len(toks) - 1, i + n * count - 1)][1], 2),
            "phrase": " ".join(t[2] for t in toks[i:i + n]),
            "repeats": count,
        })
        i += n * count
    return found


def high_compression_segments(segments: list[AsrSegment], threshold: float) -> list[dict[str, Any]]:
    """Segments whose text stayed repetitive although faster-whisper tried all fallback temperatures."""
    if threshold <= 0:
        return []
    return [{"start": round(s.start, 2), "compression_ratio": round(float(s.compression_ratio), 2)}
            for s in segments if s.compression_ratio is not None and s.compression_ratio > threshold]


def check(segments: list[AsrSegment], min_repeats: int, compression_threshold: float) -> dict[str, Any]:
    reps = find_repetitions(segments, min_repeats)
    comp = high_compression_segments(segments, compression_threshold)
    return {"repetitions": reps, "high_compression_segments": comp, "suspicious": bool(reps or comp)}


def warning_lines(quality_by_track: dict[str, dict[str, Any]], fmt_ts: Any) -> list[str]:
    """German hints for the transcript header (one per track with findings)."""
    names = {"mic": "Ich", "remote": "Gegenseite"}
    out = []
    for track, q in quality_by_track.items():
        reps = q.get("repetitions") or []
        comp = q.get("high_compression_segments") or []
        if not reps and not comp:
            continue
        parts = [f"[{fmt_ts(r['start'])}] „{r['phrase']}“ ×{r['repeats']}" for r in reps[:3]]
        if len(reps) > 3:
            parts.append(f"und {len(reps) - 3} weitere")
        if comp and not reps:
            parts.append(f"{len(comp)} Abschnitt(e) mit auffällig gleichförmigem Text ab [{fmt_ts(comp[0]['start'])}]")
        out.append(f"{names.get(track, track)}: mögliche Wiederholungsschleife – " + ", ".join(parts))
    return out
