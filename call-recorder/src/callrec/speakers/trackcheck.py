"""Plausibility checks of the two recorded tracks (is the mic track really *your* voice?).

The mic track is attributed to the user and is not diarized. That is only right if it holds the
user's voice. These cheap checks catch the typical capture problems:

* **little or no speech on the mic track** while the other side talks a lot: wrong or muted
  microphone, a Bluetooth headset switched to another endpoint, ...
* **the mic track is (mostly) a copy/echo of the remote track**: loudspeaker instead of a
  headset, headset leakage, a Bluetooth hands-free endpoint, or "Dieses Gerät als
  Wiedergabequelle verwenden" routing the playback into the microphone;
* **the user's voice is on the remote track** (sidetone/monitoring, "Abhören"), where it is
  diarized as a remote "Sprecher n".

Signals (all already available, nothing is transcribed again):
* speech duration per track = union of the Whisper segments (they come out of Whisper's VAD);
* the same words at (nearly) the same time on both tracks - a remote word that is also on the mic
  track within ``WORD_TOLERANCE_S`` is a duplicate; robust against filtering/codec, unlike
  waveform correlation;
* correlation of the 20 ms loudness envelopes of both tracks over a +-0.5 s lag window;
* cosine similarity of a remote speaker's embedding to the embedding of the mic track.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from callrec.models import AsrResult, SpeakerTurn

FRAME_S = 0.02
MAX_LAG_S = 0.5
WORD_TOLERANCE_S = 1.5  # echo/sidetone delay plus Whisper's word-timing error
MIN_WORD_LEN = 3  # "ja", "und" ... occur everywhere and prove nothing

# A full call has the other side talking for minutes; below this the "little speech" check
# would fire on short test recordings.
REMOTE_MIN_SPEECH_S = 60.0
# "Little speech": less than 5 % of the remote speech time and less than 10 s in absolute terms.
MIC_LITTLE_FRACTION = 0.05
MIC_LITTLE_ABS_S = 10.0
# Fraction of mic words that are also on the remote track at the same time:
ECHO_WORD_FRACTION = 0.6  # most of what the mic "heard" is the other side -> copy/echo
LEAK_WORD_FRACTION = 0.15  # a noticeable part -> leakage into the microphone
MIN_WORDS = 20  # fractions of fewer words are noise
# Envelope correlation of both tracks: two independent talkers correlate around 0 (or slightly
# negative, they take turns); an acoustic echo of the loudspeaker correlates clearly.
ECHO_ENVELOPE_CORR = 0.6
# A remote speaker is "probably you" if most of its words are also on the mic track at the
# same time, or its voice is as close to the mic track as a voice is to itself across calls.
SELF_WORD_FRACTION = 0.5
SELF_MIN_WORDS = 8
SELF_EMBEDDING_SIM = 0.65

MIC_SETTINGS = ("Einstellungen → Allgemein → Mikrofon bzw. Windows-Einstellungen → System → Sound → Eingabe")
LISTEN_SETTINGS = ("Windows-Systemsteuerung → Sound → Aufnahme → Mikrofon → Eigenschaften → Abhören: "
                   "„Dieses Gerät als Wiedergabequelle verwenden“ ausschalten; ggf. Mithörton (Sidetone) in der "
                   "Headset-Software ausschalten")


@dataclass
class TrackReport:
    warnings: list[str] = field(default_factory=list)
    self_suspects: dict[str, dict[str, float]] = field(default_factory=dict)  # speaker -> metrics
    metrics: dict[str, Any] = field(default_factory=dict)
    mic_unreliable: bool = False  # mic track is not (only) the user's voice

    def to_dict(self) -> dict[str, Any]:
        return {"warnings": self.warnings, "self_suspects": self.self_suspects, "metrics": self.metrics,
                "mic_unreliable": self.mic_unreliable}


def speech_spans(asr: AsrResult | None) -> list[tuple[float, float]]:
    """Merged speech intervals of a track (from Whisper's VAD-based segments)."""
    if asr is None:
        return []
    spans = sorted((s.start, s.end) for s in asr.segments if s.text.strip() and s.end > s.start)
    out: list[tuple[float, float]] = []
    for s, e in spans:
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def total(spans: list[tuple[float, float]]) -> float:
    return float(sum(e - s for s, e in spans))


_TOKEN = re.compile(r"[^\w]+", re.UNICODE)


def _tokens(asr: AsrResult | None) -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    if asr is None:
        return out
    for seg in asr.segments:
        if seg.words:
            for w in seg.words:
                tok = _TOKEN.sub("", w.text.casefold())
                if len(tok) >= MIN_WORD_LEN:
                    out.append(((w.start + w.end) / 2, tok))
        else:
            words = [_TOKEN.sub("", x.casefold()) for x in seg.text.split()]
            words = [x for x in words if len(x) >= MIN_WORD_LEN]
            step = (seg.end - seg.start) / max(1, len(words))
            out.extend((seg.start + (i + 0.5) * step, x) for i, x in enumerate(words))
    return out


def _index(tokens: list[tuple[float, str]]) -> dict[str, list[float]]:
    idx: dict[str, list[float]] = {}
    for t, tok in tokens:
        idx.setdefault(tok, []).append(t)
    for v in idx.values():
        v.sort()
    return idx


def _is_dup(t: float, tok: str, idx: dict[str, list[float]]) -> bool:
    times = idx.get(tok)
    if not times:
        return False
    i = bisect.bisect_left(times, t - WORD_TOLERANCE_S)
    return i < len(times) and times[i] <= t + WORD_TOLERANCE_S


def duplicate_fraction(a: list[tuple[float, str]], b_index: dict[str, list[float]]) -> tuple[float, int]:
    if not a:
        return 0.0, 0
    dup = sum(1 for t, tok in a if _is_dup(t, tok, b_index))
    return dup / len(a), len(a)


def envelope(samples: np.ndarray, sr: int) -> np.ndarray:
    n = max(1, int(FRAME_S * sr))
    frames = samples[: (samples.size // n) * n].reshape(-1, n) if samples.size >= n else samples.reshape(1, -1)
    return np.sqrt(np.mean(np.square(frames, dtype=np.float32), axis=1)).astype(np.float32)


def envelope_correlation(mic_env: np.ndarray, remote_env: np.ndarray) -> tuple[float, float]:
    """(max Pearson correlation of the log envelopes over +-MAX_LAG_S, lag in s; mic later = > 0)."""
    n = min(mic_env.size, remote_env.size)
    if n < 100:
        return 0.0, 0.0
    a = np.log10(mic_env[:n] + 1e-4)
    b = np.log10(remote_env[:n] + 1e-4)
    best, best_lag = -1.0, 0
    max_lag = int(MAX_LAG_S / FRAME_S)
    for lag in range(-max_lag, max_lag + 1):
        x = a[lag:] if lag >= 0 else a[: n + lag]
        y = b[: n - lag] if lag >= 0 else b[-lag:]
        if x.size < 100 or float(np.std(x)) == 0 or float(np.std(y)) == 0:
            continue
        c = float(np.corrcoef(x, y)[0, 1])
        if c > best:
            best, best_lag = c, lag
    return (max(best, 0.0) if best > -1.0 else 0.0), best_lag * FRAME_S


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    return float(np.dot(a, b) / (na * nb)) if na and nb else 0.0


def _label(spk: str) -> str:
    return f"Sprecher {spk[1:]}" if spk[:1] == "S" and spk[1:].isdigit() else spk


def _is_hands_free(name: str) -> bool:
    n = name.casefold()
    return any(k in n for k in ("hands-free", "handsfree", "freisprech", "hf audio", "headset (bluetooth"))


def check(
    mic_asr: AsrResult | None,
    remote_asr: AsrResult | None,
    turns: list[SpeakerTurn],
    embeddings: dict[str, np.ndarray],
    self_embedding: np.ndarray | None,
    signal: dict[str, Any] | None = None,
    devices: dict[str, Any] | None = None,
) -> TrackReport:
    """``signal``: cached envelope metrics ({"envelope_corr", "lag_s"}) from the diarization step."""
    rep = TrackReport()
    mic_spans, remote_spans = speech_spans(mic_asr), speech_spans(remote_asr)
    mic_s, remote_s = total(mic_spans), total(remote_spans)
    mic_tok, remote_tok = _tokens(mic_asr), _tokens(remote_asr)
    remote_idx, mic_idx = _index(remote_tok), _index(mic_tok)
    mic_dup, mic_words = duplicate_fraction(mic_tok, remote_idx)
    env_corr = float((signal or {}).get("envelope_corr") or 0.0)
    mic_name = str(((devices or {}).get("mic") or {}).get("name") or "")
    mic_rate = int(((devices or {}).get("mic") or {}).get("sample_rate") or 0)
    rep.metrics = {"mic_speech_s": round(mic_s, 1), "remote_speech_s": round(remote_s, 1),
                   "mic_words": mic_words, "mic_words_also_remote": round(mic_dup, 3),
                   "envelope_corr": round(env_corr, 3), "lag_s": (signal or {}).get("lag_s")}
    hf = _is_hands_free(mic_name) or (0 < mic_rate <= 16000)
    device_hint = (f" Das Mikrofon „{mic_name}“ ist vermutlich ein Bluetooth-Freisprechgerät (schlechte Qualität, "
                   "nimmt oft den Lautsprecher mit) – besser das Stereo-/Headset-Mikrofon wählen."
                   if hf and mic_name else "")
    if remote_s >= REMOTE_MIN_SPEECH_S and mic_s < max(MIC_LITTLE_ABS_S, MIC_LITTLE_FRACTION * remote_s):
        rep.warnings.append(
            f"Die Mikrofonspur enthält kaum Sprache ({mic_s:.0f} s gegenüber {remote_s:.0f} s der Gegenseite). Falls Sie "
            f"gesprochen haben, wurde vermutlich das falsche oder ein stummgeschaltetes Mikrofon aufgenommen – "
            f"Mikrofon prüfen: {MIC_SETTINGS}.{device_hint}")
        rep.mic_unreliable = mic_s < 1.0
    echo = (mic_words >= MIN_WORDS and mic_dup >= ECHO_WORD_FRACTION) or env_corr >= ECHO_ENVELOPE_CORR
    if echo:
        rep.mic_unreliable = True
        rep.warnings.append(
            f"Die Mikrofonspur ist im Wesentlichen ein Echo/eine Kopie der Gegenseite ({mic_dup:.0%} der Wörter gleichzeitig "
            f"auf beiden Spuren, Lautstärkeverlauf-Korrelation {env_corr:.2f}). Wahrscheinliche Ursachen: Lautsprecher statt "
            f"Headset, Bluetooth-Freisprechprofil oder „Abhören“ der Wiedergabe. Mikrofon prüfen: {MIC_SETTINGS}; "
            f"{LISTEN_SETTINGS}.{device_hint}")
    elif mic_words >= MIN_WORDS and mic_dup >= LEAK_WORD_FRACTION:
        rep.warnings.append(
            f"Ein Teil der Gegenseite ist auch auf der Mikrofonspur ({mic_dup:.0%} der Wörter) – Übersprechen des Headsets "
            f"oder Lautsprecher. Solche Stellen können im Transkript doppelt (auch unter Ihrem Namen) erscheinen. "
            f"Abhilfe: Headset verwenden, Lautstärke senken; {MIC_SETTINGS}.{device_hint}")
    if not rep.mic_unreliable:
        by_spk: dict[str, list[tuple[float, str]]] = {}
        if turns:
            from callrec.speakers.assign import TurnIndex

            index = TurnIndex(turns)
            for t, tok in remote_tok:
                spk = index.speaker_for(t - 0.01, t + 0.01, max_distance=0.0)
                if spk:
                    by_spk.setdefault(spk, []).append((t, tok))
        for spk in dict.fromkeys(t.speaker for t in turns):
            frac, n = duplicate_fraction(by_spk.get(spk, []), mic_idx)
            sim = _cos(np.asarray(embeddings[spk]).reshape(-1), np.asarray(self_embedding).reshape(-1)) \
                if self_embedding is not None and spk in embeddings else 0.0
            if (n >= SELF_MIN_WORDS and frac >= SELF_WORD_FRACTION) or sim >= SELF_EMBEDDING_SIM:
                rep.self_suspects[spk] = {"words_also_on_mic": round(frac, 3), "words": n,
                                          "similarity_to_mic": round(sim, 3)}
        if rep.self_suspects:
            rep.warnings.append(
                "Ihre eigene Stimme ist vermutlich auch auf der Spur der Gegenseite (Mithören/Sidetone) und wurde dort als "
                f"Sprecher erkannt ({', '.join(_label(s) for s in sorted(rep.self_suspects))}). Im Dialog „Sprecher zuordnen“ mit „Das bin ich“ "
                f"markieren; abstellen: {LISTEN_SETTINGS}.")
    return rep
