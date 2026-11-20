"""Per-recording language decision from language probabilities of several speech windows.

The worker (``--detect-language``) measures, for each track, Whisper's language probabilities in
up to N 30-second windows of *speech* (after VAD) spread evenly over the recording. This module
turns those measurements into one decision - pure logic, so it is testable without a model.

Rules
- Only the configured candidates count (default de, en); each window's candidate probabilities
  are renormalised to sum to 1. Windows where the candidates together get < 5 % (music, noise,
  another language) are ignored.
- Windows are weighted by their amount of speech, so a short, quiet start cannot dominate.
- The first candidate is the prior: another candidate wins only with a weighted probability of at
  least ``switch_threshold`` and at least ``min_speech_s`` seconds of measured speech.
- Both tracks normally share one language: their windows are pooled. Only when *each* track on its
  own clearly indicates a different language (both above the threshold with enough speech), each
  track is transcribed in its own language ("per_track").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

LANGUAGE_NAMES = {
    "de": "Deutsch", "en": "Englisch", "fr": "Französisch", "es": "Spanisch", "it": "Italienisch",
    "nl": "Niederländisch", "pl": "Polnisch", "pt": "Portugiesisch", "tr": "Türkisch", "ru": "Russisch",
    "uk": "Ukrainisch", "cs": "Tschechisch", "da": "Dänisch", "sv": "Schwedisch", "no": "Norwegisch",
    "fi": "Finnisch", "hu": "Ungarisch", "ro": "Rumänisch", "el": "Griechisch", "zh": "Chinesisch",
    "ja": "Japanisch", "ar": "Arabisch",
}
TRACK_NAMES = {"mic": "Ich", "remote": "Gegenseite"}
MIN_CANDIDATE_MASS = 0.05


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code, code)


def _fmt_p(p: float) -> str:
    return f"{p:.2f}".replace(".", ",")


@dataclass
class TrackEstimate:
    scores: dict[str, float] = field(default_factory=dict)  # candidate -> weighted probability
    speech_s: float = 0.0
    windows: int = 0
    ignored_windows: int = 0

    def best(self) -> str | None:
        return max(self.scores, key=lambda c: self.scores[c]) if self.scores else None


def estimate(windows: list[dict[str, Any]], candidates: list[str]) -> TrackEstimate:
    est = TrackEstimate()
    sums = dict.fromkeys(candidates, 0.0)
    for w in windows:
        probs = w.get("probs") or {}
        mass = sum(float(probs.get(c, 0.0)) for c in candidates)
        speech = float(w.get("speech_s") or 0.0)
        if mass < MIN_CANDIDATE_MASS or speech <= 0:
            est.ignored_windows += 1
            continue
        est.windows += 1
        est.speech_s += speech
        for c in candidates:
            sums[c] += speech * float(probs.get(c, 0.0)) / mass
    if est.speech_s > 0:
        est.scores = {c: round(v / est.speech_s, 4) for c, v in sums.items()}
    return est


def _verdict(est: TrackEstimate, candidates: list[str], threshold: float, min_speech_s: float) -> tuple[str, bool]:
    """(language, clearly_detected)"""
    prior = candidates[0]
    best = est.best()
    if best is None or est.speech_s < min_speech_s:
        return prior, False
    if best != prior:
        return (best, True) if est.scores[best] >= threshold else (prior, False)
    return prior, est.scores[prior] >= threshold


def _probs_text(est: TrackEstimate) -> str:
    return " / ".join(f"{c} {_fmt_p(p)}" for c, p in est.scores.items())


def decide(tracks: dict[str, list[dict[str, Any]]], candidates: list[str], threshold: float = 0.75,
           min_speech_s: float = 20.0) -> dict[str, Any]:
    """Language per track plus the full reasoning (stored in meta.json, log and transcript header)."""
    candidates = list(dict.fromkeys(candidates)) or ["de"]
    prior = candidates[0]
    per = {t: estimate(w, candidates) for t, w in tracks.items()}
    pooled = estimate([w for ws in tracks.values() for w in ws], candidates)
    conv, conv_clear = _verdict(pooled, candidates, threshold, min_speech_s)
    verdicts = {t: _verdict(e, candidates, threshold, min_speech_s) for t, e in per.items()}
    clear = {t: lang for t, (lang, ok) in verdicts.items() if ok}
    if len(clear) >= 2 and len(set(clear.values())) > 1:
        mode = "per_track"
        languages = {t: verdicts[t][0] for t in tracks}
        reason = "Spuren in unterschiedlichen Sprachen: " + ", ".join(
            f"{TRACK_NAMES.get(t, t)} {language_name(languages[t])} ({_probs_text(per[t])})" for t in tracks)
    else:
        languages = dict.fromkeys(tracks, conv)
        detail = (f"{_probs_text(pooled)}; {pooled.windows} Abschnitte, {pooled.speech_s:.0f} s Sprache"
                  if pooled.scores else "keine Sprache gemessen")
        if pooled.speech_s < min_speech_s:
            mode, why = "prior", "zu wenig Sprache für eine sichere Erkennung"
        elif conv != prior or conv_clear:
            mode, why = "detected", ""
        else:
            mode, why = "prior", "keine andere Sprache eindeutig erkannt"
        if mode == "detected":
            reason = f"{language_name(conv)} (automatisch erkannt: {detail})"
        else:
            reason = f"{language_name(conv)} (Vorgabe, {why}: {detail})"
    return {
        "mode": mode,  # detected | prior | per_track (forced is set by the caller)
        "candidates": candidates,
        "threshold": threshold,
        "min_speech_s": min_speech_s,
        "language": conv if mode != "per_track" else None,
        "tracks": {
            t: {"language": languages[t], "scores": per[t].scores, "speech_s": round(per[t].speech_s, 1),
                "windows": per[t].windows, "ignored_windows": per[t].ignored_windows,
                "raw": tracks[t]}
            for t in tracks
        },
        "pooled_scores": pooled.scores,
        "reason": reason,
    }


def forced(language: str, tracks: list[str], why: str = "fest eingestellt") -> dict[str, Any]:
    return {
        "mode": "forced",
        "language": language,
        "tracks": {t: {"language": language} for t in tracks},
        "reason": f"{language_name(language)} ({why})",
    }


def header_text(decision: dict[str, Any]) -> str:
    """One line for the transcript header."""
    return str((decision or {}).get("reason") or "")
