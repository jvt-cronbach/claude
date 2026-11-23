"""Named transcription profiles: speed against quality, with concrete faster-whisper 1.2.1 settings.

Where the time goes (measured with the large-v3-turbo architecture, int8, CTranslate2 4.9.0, see
README "Geschwindigkeit"): the 32-layer *encoder* costs about 4 s per 30-s window on 4 cores, the
4-layer decoder 0.6 s (greedy) to 1.0 s (beam 5) for ~100 tokens, word timestamps 0.09 s. So the
decoding options below change the speed only by about 5-20 %; the large gains come from using
all cores (two parallel lanes), loading the model once and skipping repeated passes - and those
apply to every profile.

``eigene`` ("Eigene Einstellungen") uses the individual values of ``[transcription]`` as before
(beam_size, temperature_fallback, vad_*, language_detection_windows); a named profile overrides
exactly the values listed in :data:`PROFILES`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

SCHNELL, AUSGEWOGEN, GENAU, EIGENE = "schnell", "ausgewogen", "genau", "eigene"
PROFILE_NAMES = (SCHNELL, AUSGEWOGEN, GENAU)
PROFILE_CHOICES = (*PROFILE_NAMES, EIGENE)
DEFAULT_PROFILE = AUSGEWOGEN
FULL_LADDER = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)  # faster-whisper 1.2.1 default

LABELS = {SCHNELL: "Schnell", AUSGEWOGEN: "Ausgewogen", GENAU: "Genau", EIGENE: "Eigene Einstellungen"}
# one honest line each (settings pages, retranscribe dialog, benchmark)
DESCRIPTIONS = {
    SCHNELL: "Etwa 10–20 % schneller als „Ausgewogen“: einfache Dekodierung in Stapeln, schwierige Stellen werden "
             "nicht neu dekodiert – etwas mehr Fehler, Wiederholungsschleifen bleiben eher stehen (werden gemeldet).",
    AUSGEWOGEN: "Empfohlen: praktisch gleiche Qualität wie „Genau“, spart vor allem bei schwierigen Stellen und bei "
                "der Spracherkennung Zeit.",
    GENAU: "Wie bis Version 0.3: größte Suchbreite und alle Wiederholungsversuche – am langsamsten, kaum genauer "
           "als „Ausgewogen“.",
    EIGENE: "Die einzelnen Werte unter „Feineinstellung“ (Beam-Größe, VAD, Temperatur-Neuversuche, Messabschnitte) "
            "gelten unverändert.",
}


@dataclass(frozen=True)
class Decoding:
    """The speed-relevant transcription settings of one run."""

    profile: str
    beam_size: int
    best_of: int  # candidates per temperature > 0 (sampling)
    temperatures: tuple[float, ...]
    batched: bool  # faster-whisper BatchedInferencePipeline (no temperature fallback, no conditioning)
    batch_size: int
    vad_threshold: float
    vad_min_silence_ms: int
    vad_speech_pad_ms: int
    detection_windows: int  # 30-s windows per track for the language detection (at most)
    detection_early_stop: bool  # stop measuring a track once its windows agree clearly
    word_timestamps: bool = True  # needed: words are assigned to the diarized speakers

    def vad(self) -> dict[str, Any]:
        d: dict[str, Any] = {"threshold": self.vad_threshold, "min_silence_duration_ms": self.vad_min_silence_ms,
                             "speech_pad_ms": self.vad_speech_pad_ms}
        if self.batched:  # as BatchedInferencePipeline does: no VAD chunk longer than one window
            d["max_speech_duration_s"] = 30.0
        return d

    def to_dict(self) -> dict[str, Any]:
        return {"profile": self.profile, "beam_size": self.beam_size, "best_of": self.best_of,
                "temperatures": list(self.temperatures), "batched": self.batched, "batch_size": self.batch_size,
                "vad": self.vad(), "detection_windows": self.detection_windows,
                "detection_early_stop": self.detection_early_stop, "word_timestamps": self.word_timestamps}


PROFILES: dict[str, Decoding] = {
    SCHNELL: Decoding(SCHNELL, beam_size=1, best_of=1, temperatures=(0.0,), batched=True, batch_size=8,
                      vad_threshold=0.5, vad_min_silence_ms=1000, vad_speech_pad_ms=300,
                      detection_windows=3, detection_early_stop=True),
    AUSGEWOGEN: Decoding(AUSGEWOGEN, beam_size=2, best_of=3, temperatures=(0.0, 0.4, 0.8), batched=False,
                         batch_size=1, vad_threshold=0.5, vad_min_silence_ms=2000, vad_speech_pad_ms=400,
                         detection_windows=6, detection_early_stop=True),
    GENAU: Decoding(GENAU, beam_size=5, best_of=5, temperatures=FULL_LADDER, batched=False, batch_size=1,
                    vad_threshold=0.5, vad_min_silence_ms=2000, vad_speech_pad_ms=400,
                    detection_windows=6, detection_early_stop=False),
}

# The [transcription] values that the profiles replace, with their defaults up to 0.3.1. A config
# written by 0.3.x always lists them (the template and the settings dialog wrote every value):
# unchanged -> "ausgewogen", any of them changed -> "eigene" (the user's explicit choice is kept).
LEGACY_DEFAULTS: dict[str, Any] = {
    "beam_size": 5, "temperature_fallback": True, "vad_threshold": 0.5, "vad_min_silence_ms": 2000,
    "vad_speech_pad_ms": 400, "language_detection_windows": 6,
}


def profile_for_legacy(section: dict[str, Any]) -> str:
    """Profile for a [transcription] table written before profiles existed."""
    for key, default in LEGACY_DEFAULTS.items():
        if key in section and section[key] != default and not (
                isinstance(default, float) and isinstance(section[key], int | float) and float(section[key]) == default):
            return EIGENE
    return DEFAULT_PROFILE


def label(profile: str) -> str:
    return LABELS.get(profile, profile)


def decoding_for(cfg: Any, profile: str | None = None) -> Decoding:
    """Settings of ``profile`` (default: ``cfg.profile``); "eigene" = the individual config values."""
    name = (profile or getattr(cfg, "profile", "") or DEFAULT_PROFILE).strip().lower()
    if name in PROFILES:
        return PROFILES[name]
    base = PROFILES[GENAU]
    return replace(base, profile=EIGENE, beam_size=int(cfg.beam_size),
                   temperatures=FULL_LADDER if cfg.temperature_fallback else (0.0,),
                   vad_threshold=float(cfg.vad_threshold), vad_min_silence_ms=int(cfg.vad_min_silence_ms),
                   vad_speech_pad_ms=int(cfg.vad_speech_pad_ms),
                   detection_windows=int(cfg.language_detection_windows), detection_early_stop=False)
