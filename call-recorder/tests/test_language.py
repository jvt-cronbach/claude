"""Language decision, language options in the worker/pipeline, config migration, repetition check."""

from __future__ import annotations

import inspect
import json
import tomllib
from pathlib import Path

import numpy as np
import pytest

from callrec.asr import language as lang
from callrec.asr import quality
from callrec.config import Config, apply_preset, config_from_dict, load_config
from callrec.models import AsrResult, AsrSegment, Word
from tests.test_pipeline import FakeTranscriber, make_pipeline, profiles, recording_with_voices  # noqa: F401


def w(start: float, de: float, en: float, speech: float = 30.0, other: float = 0.0) -> dict:
    probs = {"de": de, "en": en}
    if other:
        probs["nl"] = other
    return {"start": start, "speech_s": speech, "probs": probs}


# -- decision ------------------------------------------------------------------
def test_german_call_with_short_ambiguous_start_stays_german():
    mic = [w(0, 0.3, 0.7, speech=6), w(300, 0.97, 0.03), w(600, 0.95, 0.05)]
    remote = [w(10, 0.9, 0.1), w(400, 0.96, 0.04)]
    d = lang.decide({"mic": mic, "remote": remote}, ["de", "en"])
    assert d["mode"] == "detected" and d["language"] == "de"
    assert {t["language"] for t in d["tracks"].values()} == {"de"}
    assert "Deutsch (automatisch erkannt" in d["reason"]


def test_english_call_is_detected_from_windows_over_the_whole_recording():
    # first window German-ish (greeting in German), the rest of the call clearly English
    mic = [w(0, 0.8, 0.2), w(200, 0.05, 0.95), w(400, 0.04, 0.96), w(600, 0.02, 0.98)]
    remote = [w(5, 0.1, 0.9), w(300, 0.03, 0.97)]
    d = lang.decide({"mic": mic, "remote": remote}, ["de", "en"], threshold=0.75)
    assert d["language"] == "en" and d["mode"] == "detected"
    assert d["pooled_scores"]["en"] > 0.75
    assert "Englisch (automatisch erkannt" in d["reason"] and "en 0," in d["reason"]


def test_ambiguous_or_short_keeps_prior():
    d = lang.decide({"mic": [w(0, 0.4, 0.6)], "remote": [w(0, 0.45, 0.55)]}, ["de", "en"])
    assert d["language"] == "de" and d["mode"] == "prior" and "Vorgabe" in d["reason"]
    short = lang.decide({"mic": [w(0, 0.0, 1.0, speech=8)], "remote": []}, ["de", "en"], min_speech_s=20)
    assert short["language"] == "de" and "zu wenig Sprache" in short["reason"]
    none = lang.decide({"mic": [], "remote": []}, ["de", "en"])
    assert none["language"] == "de" and none["mode"] == "prior"


def test_non_candidate_windows_are_ignored_and_probs_renormalised():
    # music/noise: Whisper says "nl" - the candidates together have almost no mass -> ignored
    noise = w(0, 0.01, 0.01, other=0.98)
    est = lang.estimate([noise, w(30, 0.09, 0.01)], ["de", "en"])
    assert est.ignored_windows == 1 and est.windows == 1
    assert est.scores["de"] == pytest.approx(0.9)


def test_tracks_clearly_differ_get_their_own_language():
    mic = [w(0, 0.98, 0.02), w(300, 0.97, 0.03)]
    remote = [w(0, 0.02, 0.98), w(300, 0.05, 0.95)]
    d = lang.decide({"mic": mic, "remote": remote}, ["de", "en"])
    assert d["mode"] == "per_track"
    assert d["tracks"]["mic"]["language"] == "de" and d["tracks"]["remote"]["language"] == "en"
    assert "Ich Deutsch" in d["reason"] and "Gegenseite Englisch" in d["reason"]


def test_quiet_mic_follows_the_conversation():
    mic = [w(0, 0.5, 0.5, speech=5)]
    remote = [w(0, 0.02, 0.98), w(300, 0.03, 0.97), w(600, 0.05, 0.95)]
    d = lang.decide({"mic": mic, "remote": remote}, ["de", "en"])
    assert d["mode"] == "detected" and d["tracks"]["mic"]["language"] == "en"


# -- config migration ------------------------------------------------------------
OLD_CONFIG = """# Kommentar bleibt
[transcription]
model = "auto"
# "de" oder "auto" für automatische Spracherkennung
language = "de"
beam_size = 5
"""


def test_legacy_default_language_is_migrated_in_place(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text(OLD_CONFIG, encoding="utf-8")
    cfg = load_config(p)
    assert cfg.transcription.language == "auto" and cfg.transcription.language_candidates == ["de", "en"]
    assert any("Spracherkennung" in x for x in cfg.warnings)
    text = p.read_text(encoding="utf-8")
    assert "# Kommentar bleibt" in text and "beam_size = 5" in text  # only the one line changed
    data = tomllib.loads(text)["transcription"]
    assert data["language"] == "auto" and data["language_candidates"] == ["de", "en"]
    again = load_config(p)  # second start: nothing to migrate, no notice
    assert again.transcription.language == "auto" and not again.warnings


def test_explicit_language_is_respected(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text('[transcription]\nlanguage = "de"\nlanguage_candidates = ["de", "en"]\n', encoding="utf-8")
    assert load_config(p).transcription.language == "de"  # written by this version -> deliberate
    p.write_text('[transcription]\nlanguage = "en"\n', encoding="utf-8")
    assert load_config(p).transcription.language == "en"  # never a default -> deliberate


def test_legacy_preset_language_does_not_disable_detection():
    base = Config()
    out = apply_preset(base, {"transcription": {"language": "de", "beam_size": 3}}, "Alt")
    assert out.transcription.language == "auto" and out.transcription.beam_size == 3
    new = apply_preset(base, {"transcription": {"language": "de", "language_candidates": ["de", "en"]}}, "Neu")
    assert new.transcription.language == "de"


@pytest.mark.parametrize("data", [
    {"transcription": {"language": "Deutsch"}},
    {"transcription": {"language_candidates": []}},
    {"transcription": {"language_switch_threshold": 0.3}},
    {"summary": {"language": "german"}},
])
def test_invalid_language_settings(data):
    from callrec.config import ConfigError

    with pytest.raises(ConfigError):
        config_from_dict(data)


# -- worker ----------------------------------------------------------------------
def test_decoding_options_exist_in_installed_faster_whisper():
    from faster_whisper import WhisperModel

    from callrec.asr import worker

    args = worker.build_parser().parse_args(["--audio", "a", "--out", "o", "--no-temperature-fallback",
                                             "--no-repeat-ngram-size", "3", "--multilingual"])
    opts = worker.decoding_options(args)
    params = inspect.signature(WhisperModel.transcribe).parameters
    assert set(opts) <= set(params), set(opts) - set(params)
    assert opts["temperature"] == 0.0 and opts["no_repeat_ngram_size"] == 3 and opts["multilingual"] is True
    assert opts["compression_ratio_threshold"] == 2.4 and opts["log_prob_threshold"] == -1.0
    default = worker.decoding_options(worker.build_parser().parse_args(["--audio", "a", "--out", "o"]))
    # the library's own defaults, now explicit
    assert default["temperature"] == [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    assert params["temperature"].default == default["temperature"]
    assert params["compression_ratio_threshold"].default == default["compression_ratio_threshold"]
    assert params["log_prob_threshold"].default == default["log_prob_threshold"]
    assert params["no_speech_threshold"].default == default["no_speech_threshold"]


def test_detect_track_language_samples_windows_over_the_recording(monkeypatch: pytest.MonkeyPatch):
    import faster_whisper.vad as vad
    from faster_whisper.feature_extractor import FeatureExtractor

    from callrec.asr import worker

    sr = 16000
    audio = np.zeros(sr * 400, dtype=np.float32)  # 400 s
    # speech: 0-100 s and 200-400 s (300 s of speech)
    monkeypatch.setattr(vad, "get_speech_timestamps", lambda a, opts: [{"start": 0, "end": 100 * sr},
                                                                       {"start": 200 * sr, "end": 400 * sr}])
    seen: list[int] = []

    class Inner:
        is_multilingual = True

        def detect_language(self, enc):
            seen.append(enc.shape[-1])
            return [[("<|en|>", 0.8), ("<|de|>", 0.15), ("<|nl|>", 0.05)]]

    class Stub:
        feature_extractor = FeatureExtractor()
        model = Inner()

        def encode(self, features):
            return features

    res = worker.detect_track_language(Stub(), "x", windows=6, vad={"threshold": 0.5}, audio=audio)
    assert res["speech_s"] == 300.0 and len(res["windows"]) == 6
    starts = [x["start"] for x in res["windows"]]
    assert starts[0] == 0.0 and starts[-1] == pytest.approx(370.0, abs=0.1)  # mapped back through the gap
    assert any(100 < s < 200 for s in starts) is False  # never inside the silence that VAD removed
    assert res["windows"][0]["probs"]["en"] == 0.8 and res["windows"][0]["top"] == "en"
    assert set(seen) == {3000}  # 30-s mel windows, as faster-whisper pads them


def test_worker_detect_mode_writes_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import faster_whisper

    from callrec.asr import worker

    class StubModel:
        def __init__(self, *a, **kw):
            self.model = type("M", (), {"is_multilingual": True})()

    monkeypatch.setattr(faster_whisper, "WhisperModel", StubModel)
    monkeypatch.setattr(worker, "detect_track_language",
                        lambda model, path, windows, vad: {"duration": 1.0, "speech_s": 30.0,
                                                           "windows": [{"start": 0, "speech_s": 30, "probs": {"en": 0.9}}]})
    out = tmp_path / "d.json"
    assert worker.main(["--detect-language", "--detect-audio", "mic=a.flac", "--detect-audio", "remote=b.flac",
                        "--out", str(out), "--device", "cpu"]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data["tracks"]) == {"mic", "remote"} and data["multilingual_model"] is True


# -- pipeline ----------------------------------------------------------------------
class DetectingTranscriber(FakeTranscriber):
    def __init__(self, windows: dict) -> None:
        super().__init__()
        self.windows = windows
        self.languages: list[str | None] = []
        self.detected = 0

    def detect_languages(self, tracks, out_json, progress=lambda f: None):
        self.detected += 1
        progress(-1.0, "Sprache wird erkannt")
        return {"multilingual_model": True, "detect_s": 1.0,
                "tracks": {t: {"windows": self.windows.get(t, [])} for t in tracks}}

    def transcribe(self, audio, out_json, *, language=None, **kw):
        self.languages.append(language)
        res: AsrResult = super().transcribe(audio, out_json, **kw)
        res.language = language or "?"
        return res


def _pipeline_with(profiles, transcriber):  # noqa: F811
    pipeline, _tr, _s = make_pipeline(profiles)
    pipeline.c.transcriber_factory = lambda: transcriber
    return pipeline


def test_pipeline_detects_english_and_records_it(tmp_path: Path, profiles):  # noqa: F811
    english = [w(0, 0.05, 0.95), w(300, 0.02, 0.98)]
    tr = DetectingTranscriber({"mic": english, "remote": english})
    rec = recording_with_voices(tmp_path / "r", 330, 550)
    pipeline = _pipeline_with(profiles, tr)
    result = pipeline.run(rec)
    assert tr.languages == ["en", "en"]
    meta = rec.load_meta()["processing"]["transcription"]
    assert meta["language_detection"]["language"] == "en" and meta["mic"]["language"] == "en"
    assert meta["language_detection"]["tracks"]["mic"]["scores"]["en"] > 0.9
    md = rec.transcript_path.read_text(encoding="utf-8")
    assert "- **Sprache:** Englisch (automatisch erkannt" in md
    assert any(m.startswith("Sprache: Englisch") for m in result.messages)

    # one-step re-transcription in another language: forced, no detection
    tr.languages.clear()
    pipeline.run(rec, {"transcribe", "identify", "render"}, language="de")
    assert tr.languages == ["de", "de"] and tr.detected == 1
    assert rec.load_meta()["processing"]["transcription"]["language_detection"]["mode"] == "forced"
    assert "Deutsch (für diese Verarbeitung gewählt)" in rec.transcript_path.read_text(encoding="utf-8")


def test_pipeline_respects_configured_language(tmp_path: Path, profiles):  # noqa: F811
    tr = DetectingTranscriber({})
    rec = recording_with_voices(tmp_path / "r", 330, 550)
    pipeline = _pipeline_with(profiles, tr)
    pipeline.cfg.transcription.language = "de"
    pipeline.run(rec, {"finalize", "transcribe"})
    assert tr.detected == 0 and tr.languages == ["de", "de"]
    pipeline.run(rec, {"transcribe"}, language="auto")  # "auto" = detect again despite the setting
    assert tr.detected == 1


def test_pipeline_without_detection_support_uses_prior(tmp_path: Path, profiles):  # noqa: F811
    rec = recording_with_voices(tmp_path / "r", 330, 550)
    pipeline, tr, _ = make_pipeline(profiles)  # FakeTranscriber has neither detect_languages nor language=
    pipeline.run(rec, {"finalize", "transcribe"})
    d = rec.load_meta()["processing"]["transcription"]["language_detection"]
    assert d["mode"] == "prior" and d["language"] == "de"


# -- repetition post-check ------------------------------------------------------------
def _seg(text: str, start: float = 10.0) -> AsrSegment:
    words = [Word(start + i * 0.3, start + i * 0.3 + 0.2, " " + t) for i, t in enumerate(text.split())]
    return AsrSegment(start, start + len(words) * 0.3, text, words)


def test_repetition_loop_is_flagged_but_normal_repeats_are_not():
    loop = _seg("Ich spreche " + "mit Ihnen, " * 9 + "danke.", start=754.0)
    found = quality.find_repetitions([loop], min_repeats=4)
    assert found and found[0]["phrase"].lower().startswith("mit ihnen") and found[0]["repeats"] == 9
    assert found[0]["start"] == pytest.approx(754.6, abs=0.01)
    assert quality.find_repetitions([_seg("ja ja ja ja genau so machen wir das")], 4) == []
    assert quality.find_repetitions([_seg("das das das das das das das das das")], 4)[0]["repeats"] == 9
    assert quality.find_repetitions([loop], 0) == []


def test_repetition_warning_in_transcript_header(tmp_path: Path, profiles):  # noqa: F811
    class LoopTranscriber(FakeTranscriber):
        def transcribe(self, audio, out_json, **kw):
            kw.pop("language", None)
            res = super().transcribe(audio, out_json, **kw)
            if audio.name.startswith("remote"):
                res.segments.append(AsrSegment(4.0, 5.9, "mit Ihnen " * 6,
                                               [Word(4.0 + i * 0.1, 4.05 + i * 0.1, " " + t) for i, t in
                                                enumerate(("mit Ihnen " * 6).split())], compression_ratio=3.1))
            return res

    rec = recording_with_voices(tmp_path / "r", 330, 550)
    pipeline = _pipeline_with(profiles, LoopTranscriber())
    result = pipeline.run(rec)
    md = rec.transcript_path.read_text(encoding="utf-8")
    assert "**Warnung:** Gegenseite: mögliche Wiederholungsschleife – [00:00:04] „mit Ihnen“ ×6" in md
    assert "mit Ihnen mit Ihnen" in md  # nothing deleted
    q = rec.load_meta()["processing"]["transcription"]["remote"]["quality"]
    assert q["suspicious"] and q["high_compression_segments"][0]["compression_ratio"] == 3.1
    assert any(m.startswith("Warnung:") for m in result.messages)
