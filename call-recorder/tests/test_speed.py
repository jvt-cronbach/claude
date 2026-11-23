"""0.4 processing speed: profiles, threads/lanes, the one-process session, parallel speaker separation,
timings and estimates, benchmark logic, power state - all without a Whisper model."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from callrec.asr import profiles as prof
from callrec.asr import session as ses
from callrec.config import Config, ConfigError, config_from_dict, load_config
from tests.conftest import make_recording, tone, write_wav

SR = 16000


# -- profiles ----------------------------------------------------------------------------------
def test_profiles_have_concrete_settings_and_honest_descriptions():
    assert set(prof.PROFILES) == {"schnell", "ausgewogen", "genau"}
    g, a, s = prof.PROFILES["genau"], prof.PROFILES["ausgewogen"], prof.PROFILES["schnell"]
    assert (g.beam_size, g.temperatures, g.batched) == (5, prof.FULL_LADDER, False)  # = up to 0.3.1
    assert (a.beam_size, a.temperatures, a.batched, a.best_of) == (2, (0.0, 0.4, 0.8), False, 3)
    assert (s.beam_size, s.temperatures, s.batched) == (1, (0.0,), True)
    assert all(p.word_timestamps for p in prof.PROFILES.values())  # words are assigned to the speakers
    assert s.vad()["max_speech_duration_s"] == 30.0 and "max_speech_duration_s" not in a.vad()
    assert a.vad() == {"threshold": 0.5, "min_silence_duration_ms": 2000, "speech_pad_ms": 400}
    for k in prof.PROFILE_CHOICES:
        assert prof.DESCRIPTIONS[k] and prof.LABELS[k]
    assert prof.DEFAULT_PROFILE == "ausgewogen" == Config().transcription.profile


def test_custom_profile_uses_the_individual_values():
    cfg = Config().transcription
    cfg.beam_size, cfg.temperature_fallback, cfg.vad_min_silence_ms, cfg.language_detection_windows = 3, False, 900, 4
    d = prof.decoding_for(cfg, "eigene")
    assert (d.profile, d.beam_size, d.temperatures, d.vad_min_silence_ms, d.detection_windows) == ("eigene", 3, (0.0,), 900, 4)
    assert not d.batched and not d.detection_early_stop
    cfg.profile = "eigene"
    assert prof.decoding_for(cfg).beam_size == 3
    assert prof.decoding_for(cfg, "schnell").beam_size == 1  # one-off override wins
    cfg.profile = "genau"
    assert prof.decoding_for(cfg).beam_size == 5  # a named profile ignores beam_size = 3


def test_legacy_values_decide_the_profile():
    assert prof.profile_for_legacy({"beam_size": 5, "vad_threshold": 0.5, "temperature_fallback": True}) == "ausgewogen"
    assert prof.profile_for_legacy({}) == "ausgewogen"
    assert prof.profile_for_legacy({"beam_size": 3}) == "eigene"
    assert prof.profile_for_legacy({"vad_min_silence_ms": 500}) == "eigene"
    assert prof.profile_for_legacy({"temperature_fallback": False}) == "eigene"


def test_config_validation_of_new_keys():
    with pytest.raises(ConfigError):
        config_from_dict({"transcription": {"profile": "turbo"}})
    with pytest.raises(ConfigError):
        config_from_dict({"processing": {"during_call": "stop"}})
    with pytest.raises(ConfigError):
        config_from_dict({"processing": {"parallel_diarization": "maybe"}})
    with pytest.raises(ConfigError):
        config_from_dict({"transcription": {"parallel_lanes": 9}})
    cfg = config_from_dict({"transcription": {"profile": "schnell", "cpu_threads": 6, "parallel_lanes": 2},
                            "processing": {"during_call": "pause", "parallel_diarization": "off"}})
    assert cfg.transcription.profile == "schnell" and cfg.processing.during_call == "pause"


OLD_SECTION = """[transcription]
# Kommentar bleibt
model = "auto"
beam_size = {beam}
vad_filter = true
vad_threshold = 0.5
vad_min_silence_ms = 2000
vad_speech_pad_ms = 400
temperature_fallback = true
language = "auto"
language_candidates = ["de", "en"]
language_detection_windows = 6

[consent]
mode = "ask"
"""


@pytest.mark.parametrize("beam,expected", [(5, "ausgewogen"), (3, "eigene")])
def test_migration_of_a_0_3_config(tmp_path: Path, beam: int, expected: str):
    from callrec import notices
    from callrec.config import PROFILE_NOTES

    path = tmp_path / "config.toml"
    path.write_text(OLD_SECTION.format(beam=beam), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.transcription.profile == expected and cfg.transcription.beam_size == beam
    text = path.read_text(encoding="utf-8")
    assert f'profile = "{expected}"' in text and "# Kommentar bleibt" in text
    assert PROFILE_NOTES[expected] in cfg.warnings
    assert PROFILE_NOTES[expected] in notices.pending()
    # loaded again: nothing changes any more, no second notice
    again = load_config(path)
    assert again.transcription.profile == expected and PROFILE_NOTES[expected] not in again.warnings
    assert path.read_text(encoding="utf-8") == text


def test_settings_saved_later_keep_the_profile(tmp_path: Path):
    from callrec.config import save_config

    path = tmp_path / "config.toml"
    cfg = Config()
    cfg.transcription.profile = "genau"
    save_config(cfg, path)
    assert load_config(path).transcription.profile == "genau"


def test_legacy_preset_with_own_values_becomes_custom(tmp_path: Path):
    from callrec.config import apply_preset

    out = apply_preset(Config(), {"transcription": {"beam_size": 8, "vad_threshold": 0.5}}, "Alt")
    assert out.transcription.profile == "eigene" and out.transcription.beam_size == 8
    out = apply_preset(Config(), {"transcription": {"beam_size": 5}}, "Standard")
    assert out.transcription.profile == "ausgewogen"
    out = apply_preset(Config(), {"transcription": {"profile": "schnell", "beam_size": 8}}, "Neu")
    assert out.transcription.profile == "schnell"


# -- threads -----------------------------------------------------------------------------------
def test_asr_threads_use_all_cores_in_two_lanes_and_leave_room_during_calls():
    from callrec.speakers.threads import asr_threads

    t = asr_threads(cores=8, busy=False)
    assert (t.lanes, t.threads, t.total, t.source) == (2, 4, 8, "auto")
    assert asr_threads(cores=4, busy=False).to_dict()["lanes"] == 2
    assert (asr_threads(cores=3, busy=False).lanes, asr_threads(cores=3, busy=False).threads) == (1, 3)
    busy = asr_threads(cores=3, busy=True)
    assert (busy.lanes, busy.threads) == (1, 1)  # no second lane to switch off: start with half
    assert asr_threads(cores=8, busy=True).busy_lanes == 1  # one of two lanes keeps working = half the cores
    red = asr_threads(cores=8, busy=False, reserve=2)
    assert (red.lanes, red.threads) == (2, 3)
    fixed = asr_threads(configured_threads=6, configured_lanes=3, cores=8, busy=False)
    assert (fixed.lanes, fixed.threads, fixed.source) == (3, 2, "fest")
    tuned = asr_threads(cores=8, busy=False, tuned={"cores": 8, "lanes": 1, "threads": 8})
    assert (tuned.lanes, tuned.threads, tuned.source) == (1, 8, "Messung")
    other_pc = asr_threads(cores=6, busy=False, tuned={"cores": 8, "lanes": 1, "threads": 8})
    assert other_pc.source == "auto"
    assert "2 parallele Spuren à 4 Threads" in t.describe()


def test_call_state_marks_the_pc_busy():
    from callrec.speakers import threads

    threads.set_call_active(True)
    try:
        assert threads.pc_busy() and threads.busy_reason() == "Teams-Anruf läuft"
        threads.set_recording_active(True)
        assert threads.busy_reason() == "Aufnahme läuft"
    finally:
        threads.set_call_active(False)
        threads.set_recording_active(False)
    assert not threads.pc_busy() and threads.busy_reason() == ""


# -- session building blocks --------------------------------------------------------------------
def _chunks(*spans: tuple[float, float]) -> list[dict[str, int]]:
    return [{"start": int(a * SR), "end": int(b * SR)} for a, b in spans]


def test_pieces_balance_the_lanes_and_never_split_a_chunk():
    mic = _chunks(*[(i * 20.0, i * 20.0 + 10.0) for i in range(30)])  # 300 s speech
    remote = _chunks(*[(i * 20.0 + 10, i * 20.0 + 19.0) for i in range(100)])  # 900 s speech
    pieces = ses.plan_pieces({"mic": mic, "remote": remote}, lanes=2)
    assert sum(p.speech_s for p in pieces) == pytest.approx(1200.0)
    assert [p.speech_s for p in pieces] == sorted((p.speech_s for p in pieces), reverse=True)
    assert all(60.0 <= p.speech_s <= 300.0 for p in pieces)
    by_track = {t: [c for p in sorted(pieces, key=lambda p: p.index) if p.track == t for c in p.chunks] for t in ("mic", "remote")}
    assert by_track == {"mic": mic, "remote": remote}
    one = ses.plan_pieces({"mic": mic}, lanes=1)
    assert len(one) == 1 and one[0].speech_s == 300.0  # one lane: up to 10 min per piece
    assert ses.plan_pieces({"mic": []}, lanes=2) == []


def test_merge_clips_like_collect_chunks():
    clips = ses.merge_clips(_chunks((0, 10), (12, 22), (30, 42), (50, 51)))
    assert clips == [{"start": 0.0, "end": 20.0}, {"start": 20.0, "end": 33.0}]
    assert all(c["end"] - c["start"] <= 30.0 for c in clips)


def test_window_order_and_clear_agreement():
    assert ses.window_order(6) == [0, 5, 3, 1, 2, 4]
    assert ses.window_order(1) == [0] and ses.window_order(0) == []
    w = {"top": "de", "top_prob": 0.97, "speech_s": 30.0}
    assert ses.clear_agreement([w, w], ["de", "en"])
    assert not ses.clear_agreement([w], ["de", "en"])  # at least two windows
    assert not ses.clear_agreement([w, {**w, "top": "en"}], ["de", "en"])
    assert not ses.clear_agreement([w, {**w, "top_prob": 0.6}], ["de", "en"])
    assert not ses.clear_agreement([{**w, "speech_s": 5}, {**w, "speech_s": 5}], ["de", "en"])  # too little speech
    assert not ses.clear_agreement([{**w, "top": "fr"}] * 2, ["de", "en"])


def test_detect_windows_stops_early_once_clear():
    from faster_whisper.feature_extractor import FeatureExtractor

    encoded: list[int] = []

    class Inner:
        def detect_language(self, enc):
            return [[("<|de|>", 0.97), ("<|en|>", 0.02)]]

    class Stub:
        feature_extractor = FeatureExtractor()
        model = Inner()

        def encode(self, features):
            encoded.append(1)
            return features

    audio = np.zeros(SR * 400, dtype=np.float32)
    chunks = _chunks((0, 100), (200, 400))
    res = ses.detect_windows(Stub(), audio, chunks, 6, ["de", "en"], early_stop=True)
    assert res["measured"] == 3 and res["planned"] == 6 and len(encoded) == 3  # first, last, middle
    assert [w["start"] for w in res["windows"]] == sorted(w["start"] for w in res["windows"])
    assert res["windows"][-1]["start"] == pytest.approx(370.0, abs=0.1)  # first and last window measured
    full = ses.detect_windows(Stub(), audio, chunks, 6, ["de", "en"], early_stop=False)
    assert full["measured"] == 6
    # windows naming another language than the prior never stop the measurement early
    Inner.detect_language = lambda self, enc: [[("<|en|>", 0.97), ("<|de|>", 0.02)]]
    assert ses.detect_windows(Stub(), audio, chunks, 6, ["de", "en"], early_stop=True)["measured"] == 6
    assert ses.detect_windows(Stub(), audio, [], 6)["windows"] == []


def test_lane_gate_reduces_and_pauses_without_losing_pieces():
    gate = ses.LaneGate(2)
    pieces = [ses.Piece("mic", _chunks((i, i + 1)), i) for i in range(8)]
    done: list[tuple[int, int]] = []
    lock = threading.Lock()

    def work(lane: int, piece: ses.Piece) -> float:
        with lock:
            done.append((lane, piece.index))
            if len(done) == 2:
                gate.set_active(0)  # "pause": a call started
                threading.Timer(0.3, lambda: gate.set_active(1)).start()
        time.sleep(0.02)
        return 0.0

    ses.run_lanes(pieces, 2, gate, work)
    assert sorted(i for _l, i in done) == list(range(8))
    assert all(lane == 0 for lane, _i in done[3:])  # after the pause only one lane worked
    gate2 = ses.LaneGate(2)
    ses.read_control(["LANES 1\n", "garbage\n", "LANES x\n", "LANES 5\n"], gate2)
    assert gate2.active == 2  # clamped to the lanes there are


def test_run_lanes_reraises_the_first_error():
    def work(lane: int, piece: ses.Piece) -> float:
        raise ValueError("kaputt")

    with pytest.raises(ValueError, match="kaputt"):
        ses.run_lanes([ses.Piece("mic", _chunks((0, 1)))], 2, ses.LaneGate(2), work)


def test_transcribe_piece_maps_times_back_like_faster_whisper():
    from faster_whisper.transcribe import Segment, Word

    piece = ses.Piece("remote", _chunks((100, 110), (200, 205)))
    samples = np.zeros(SR * 300, dtype=np.float32)
    seen: dict = {}

    class Engine:
        def transcribe(self, audio, **kw):
            seen["n"], seen["kw"] = len(audio), kw
            seg = Segment(id=1, seek=0, start=8.0, end=13.0, text=" Hallo Welt", tokens=[], avg_logprob=-0.2,
                          compression_ratio=1.2, no_speech_prob=0.0, temperature=0.0,
                          words=[Word(8.0, 9.0, " Hallo", 0.9), Word(11.0, 13.0, " Welt", 0.8)])
            late = Segment(id=2, seek=0, start=14.0, end=29.0, text=" Ende", tokens=[], avg_logprob=-0.2,
                           compression_ratio=1.0, no_speech_prob=0.0, temperature=0.0,
                           words=[Word(14.0, 29.0, " Ende", 0.9)])
            return iter([seg, late]), None

    progress: list[float] = []
    out = ses.transcribe_piece(Engine(), samples, piece, "de", {"beam_size": 2}, False, progress.append)
    assert seen["n"] == 15 * SR and seen["kw"]["vad_filter"] is False and seen["kw"]["language"] == "de"
    assert out[0]["words"][0]["start"] == 108.0 and out[0]["words"][1]["start"] == 201.0  # across the gap
    assert out[0]["start"] == 108.0 and out[0]["end"] == 203.0
    assert out[1]["end"] == 205.0  # a timestamp past the piece's speech is clamped
    assert progress and progress[-1] == 1.0
    ses.transcribe_piece(Engine(), samples, piece, "de", {}, True)
    assert seen["kw"]["clip_timestamps"] == [{"start": 0.0, "end": 15.0}]


def test_decoding_kwargs_per_mode():
    dec = prof.PROFILES["ausgewogen"].to_dict()
    opts = {"compression_ratio_threshold": 2.4, "log_prob_threshold": -1.0, "no_speech_threshold": 0.6,
            "initial_prompt": "Gespräch mit Anna.", "hallucination_silence_threshold": 0.0}
    kw = ses.decoding_kwargs(dec, opts, False)
    assert kw["beam_size"] == 2 and kw["temperature"] == [0.0, 0.4, 0.8] and kw["best_of"] == 3
    assert kw["word_timestamps"] is True and kw["condition_on_previous_text"] is False
    assert kw["hallucination_silence_threshold"] is None and kw["initial_prompt"] == "Gespräch mit Anna."
    import inspect

    from faster_whisper import BatchedInferencePipeline, WhisperModel

    assert set(kw) <= set(inspect.signature(WhisperModel.transcribe).parameters)
    bkw = ses.decoding_kwargs(prof.PROFILES["schnell"].to_dict(), opts, True)
    assert bkw["temperature"] == 0.0 and bkw["batch_size"] == 8 and "condition_on_previous_text" not in bkw
    assert set(bkw) <= set(inspect.signature(BatchedInferencePipeline.transcribe).parameters)


# -- the worker's session ------------------------------------------------------------------------
class StubWhisper:
    instances: list[dict] = []

    def __init__(self, model, device, compute_type, download_root, local_files_only=False, **kw):
        StubWhisper.instances.append({"model": model, "device": device, **kw})
        self.model = type("M", (), {"is_multilingual": True})()
        self.calls: list[int] = []

    def transcribe(self, audio, **kw):
        from faster_whisper.transcribe import Segment, Word

        self.calls.append(len(audio))
        dur = len(audio) / SR
        return iter([Segment(id=0, seek=0, start=0.0, end=dur, text=f" {kw.get('language')}", tokens=[],
                             avg_logprob=-0.1, compression_ratio=1.0, no_speech_prob=0.0, temperature=0.0,
                             words=[Word(0.0, 1.0, f" {kw.get('language')}", 0.9),
                                    Word(dur - 1.0, dur, " Ende", 0.9)])]), None


def _session_job(tmp_path: Path, language: dict, lanes: int = 2) -> dict:
    return {"model": "large-v3-turbo", "device": "cpu", "compute_type": "auto", "lanes": lanes, "threads": 3,
            "tracks": {"mic": {"audio": "mic.flac", "out": str(tmp_path / "asr_mic.json")},
                       "remote": {"audio": "remote.flac", "out": str(tmp_path / "asr_remote.json")}},
            "language": language, "decoding": prof.PROFILES["ausgewogen"].to_dict(), "options": {}}


def test_run_session_loads_once_skips_silent_tracks_and_writes_both(tmp_path: Path, monkeypatch, capsys):
    from callrec.asr import worker

    StubWhisper.instances = []
    audio = {"mic.flac": np.zeros(SR * 60, np.float32), "remote.flac": np.zeros(SR * 120, np.float32)}
    chunks = {60 * SR: [], 120 * SR: _chunks((10, 40), (50, 100))}
    monkeypatch.setattr(worker, "_decode_track", lambda p: audio[p])
    monkeypatch.setattr(worker, "_vad_chunks", lambda x, vad: chunks[len(x)])
    from callrec.asr import language as lang

    res = worker.run_session(_session_job(tmp_path, {"mode": "forced", "decision": lang.forced("de", ["mic", "remote"])}),
                             model_factory=StubWhisper)
    assert len(StubWhisper.instances) == 1  # one model for everything
    assert StubWhisper.instances[0]["cpu_threads"] == 3 and StubWhisper.instances[0]["num_workers"] == 2
    mic = json.loads((tmp_path / "asr_mic.json").read_text(encoding="utf-8"))
    remote = json.loads((tmp_path / "asr_remote.json").read_text(encoding="utf-8"))
    assert mic["segments"] == [] and res["tracks"]["mic"]["skipped"]
    assert remote["duration"] == 120.0 and remote["segments"][0]["start"] == 10.0 and remote["language"] == "de"
    assert remote["segments"][0]["end"] == 100.0  # mapped back to the recording time
    assert res["tracks"]["remote"]["speech_s"] == 80.0 and res["language_detection"]["mode"] == "forced"
    out = capsys.readouterr().out
    assert "STAGE load" in out and "STAGE transcribe" in out and "PROGRESS 1.0" in out


def test_run_session_detects_the_language_itself(tmp_path: Path, monkeypatch):
    from callrec.asr import worker

    audio = {"mic.flac": np.zeros(SR * 100, np.float32), "remote.flac": np.zeros(SR * 100, np.float32)}
    monkeypatch.setattr(worker, "_decode_track", lambda p: audio[p])
    monkeypatch.setattr(worker, "_vad_chunks", lambda x, vad: _chunks((0, 90)))
    english = {"windows": [{"start": 0, "speech_s": 30, "top": "en", "top_prob": 0.98, "probs": {"en": 0.98, "de": 0.01}}] * 3,
               "measured": 3, "planned": 6}
    calls: list[tuple] = []

    def fake_detect(model, samples, chunks, windows, candidates, early_stop, min_speech_s):
        calls.append((windows, early_stop))
        return english

    monkeypatch.setattr(ses, "detect_windows", fake_detect)
    spec = {"mode": "auto", "candidates": ["de", "en"], "threshold": 0.75, "min_speech_s": 20, "windows": 6,
            "early_stop": True}
    res = worker.run_session(_session_job(tmp_path, spec), model_factory=StubWhisper)
    d = res["language_detection"]
    assert d["language"] == "en" and d["windows_measured"] == {"mic": 3, "remote": 3} and calls == [(6, True)] * 2
    assert json.loads((tmp_path / "asr_mic.json").read_text(encoding="utf-8"))["segments"][0]["text"] == "en"

    def broken(*a, **k):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(ses, "detect_windows", broken)
    res = worker.run_session(_session_job(tmp_path, spec), model_factory=StubWhisper)
    assert res["language_detection"]["mode"] == "prior" and res["language_detection"]["language"] == "de"


def test_session_main_reads_control_and_writes_result(tmp_path: Path, monkeypatch):
    from callrec.asr import worker

    job = _session_job(tmp_path, {"mode": "forced", "decision": {"tracks": {"mic": {"language": "de"},
                                                                            "remote": {"language": "de"}}}})
    job["no_control"] = True
    (tmp_path / "job.json").write_text(json.dumps(job), encoding="utf-8")
    monkeypatch.setattr(worker, "run_session", lambda job, gate: {"ok": True, "lanes": gate.lanes})
    assert worker.main(["--session", str(tmp_path / "job.json"), "--out", str(tmp_path / "r.json")]) == 0
    assert json.loads((tmp_path / "r.json").read_text(encoding="utf-8")) == {"ok": True, "lanes": 2}


# -- parent side: transcribe_session ----------------------------------------------------------------
FAKE_SESSION_WORKER = r'''
import json, sys, time, os
args = sys.argv[1:]
job = json.load(open(args[args.index("--session") + 1], encoding="utf-8"))
out = args[args.index("--out") + 1]
if job["device"] == "cuda" and os.environ.get("FAKE_CUDA_BROKEN"):
    os._exit(3)
print("STAGE load", flush=True)
print("STAGE transcribe 0.5", flush=True)
seen = []
if os.environ.get("FAKE_WAIT_CONTROL"):
    import threading
    def rd():
        for line in sys.stdin:
            seen.append(line.strip())
    threading.Thread(target=rd, daemon=True).start()
    time.sleep(float(os.environ["FAKE_WAIT_CONTROL"]))
for t, spec in job["tracks"].items():
    json.dump({"language": "de", "duration": 6.0, "model": job["model"], "device": job["device"], "compute_type": "int8",
               "segments": [{"start": 0, "end": 1, "text": "hallo " + t, "words": [{"start": 0, "end": 1, "text": " hallo", "probability": 0.9}]}]},
              open(spec["out"], "w", encoding="utf-8"))
print("PROGRESS 1.0", flush=True)
json.dump({"language_detection": {"mode": "forced", "reason": "Deutsch (fest eingestellt)", "tracks": {}},
           "tracks": {t: {"audio_s": 6.0, "speech_s": 1.0, "lane_s": 0.2, "skipped": ""} for t in job["tracks"]},
           "timing": {"load_s": 0.5, "transcribe_s": 0.3}, "decoding": job["decoding"], "pieces": 2,
           "job": {k: job[k] for k in ("lanes", "threads", "active_lanes")}, "control": seen},
          open(out, "w", encoding="utf-8"))
'''


def _session_transcriber(tmp_path: Path, **cfg):
    from callrec.asr.transcriber import WhisperSubprocessTranscriber
    from callrec.config import TranscriptionConfig

    script = tmp_path / "fake_session.py"
    script.write_text(FAKE_SESSION_WORKER, encoding="utf-8")
    return WhisperSubprocessTranscriber(TranscriptionConfig(**cfg), tmp_path / "models", [sys.executable, str(script)])


def test_transcribe_session_runs_one_worker_and_falls_back_to_cpu(tmp_path: Path, monkeypatch):
    from callrec.asr.transcriber import WhisperSubprocessTranscriber

    WhisperSubprocessTranscriber._cuda_failed = False
    monkeypatch.setenv("FAKE_CUDA_BROKEN", "1")
    t = _session_transcriber(tmp_path, profile="genau")
    tracks = {"mic": tmp_path / "mic.flac", "remote": tmp_path / "remote.flac"}
    outs = {k: tmp_path / f"asr_{k}.json" for k in tracks}
    progress: list = []
    res = t.transcribe_session(tracks, outs, language={"mode": "forced", "decision": {}}, work_dir=tmp_path / "w",
                               progress=lambda f, m="": progress.append((f, m)), busy=lambda: False)
    WhisperSubprocessTranscriber._cuda_failed = False
    assert res.asr["remote"].segments[0].text == "hallo remote" and res.asr["mic"].device == "cpu"
    assert res.stats["decoding"]["profile"] == "genau" and res.stats["timing"]["wall_s"] > 0
    assert res.stats["threads"]["lanes"] >= 1 and res.decision["reason"] == "Deutsch (fest eingestellt)"
    job = json.loads((tmp_path / "w" / "session_job.json").read_text(encoding="utf-8"))
    assert job["device"] == "cpu" and job["decoding"]["beam_size"] == 5
    assert (-1.0, "Modell wird geladen") in progress


def test_transcribe_session_tells_the_worker_when_a_call_starts(tmp_path: Path, monkeypatch):
    from callrec.asr import transcriber as trm

    monkeypatch.setenv("FAKE_WAIT_CONTROL", "1.5")
    monkeypatch.setattr(trm, "CONTROL_POLL_S", 0.1)
    t = _session_transcriber(tmp_path, device="cpu", parallel_lanes=2, cpu_threads=4)
    tracks = {"mic": tmp_path / "mic.flac", "remote": tmp_path / "remote.flac"}
    busy = {"v": False}
    threading.Timer(0.4, lambda: busy.__setitem__("v", True)).start()
    t.transcribe_session(tracks, {k: tmp_path / f"asr_{k}.json" for k in tracks}, language={"mode": "forced"},
                         work_dir=tmp_path / "w", busy=lambda: busy["v"], during_call="pause")
    res = json.loads((tmp_path / "w" / "session_result.json").read_text(encoding="utf-8"))
    assert res["job"] == {"lanes": 2, "threads": 2, "active_lanes": 2} and res["control"] == ["LANES 0"]


# -- pipeline --------------------------------------------------------------------------------------
class FakeSession:
    """Transcriber with the 0.4 session API."""

    selection_note = ""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def transcribe_session(self, tracks, outs, *, language, work_dir, initial_prompt="", hotwords="",
                           progress=lambda f, m="": None, profile=None, reserve_threads=0, during_call="reduce"):
        from callrec.asr import language as lang
        from callrec.asr.transcriber import SessionResult
        from callrec.models import AsrResult, AsrSegment, Word

        self.calls.append({"language": language, "profile": profile, "reserve": reserve_threads, "prompt": initial_prompt})
        progress(-1.0, "Modell wird geladen")
        progress(0.5)
        asr = {"mic": AsrResult("de", 6.0, [AsrSegment(0.2, 1.0, "Hallo zusammen.", [Word(0.2, 1.0, " Hallo zusammen.")])]),
               "remote": AsrResult("de", 6.0, [AsrSegment(1.2, 2.5, "Hallo, erste Stimme.", [Word(1.2, 2.5, " Hallo, erste Stimme.")]),
                                               AsrSegment(3.2, 5.5, "Und zweite Stimme.", [Word(3.2, 5.5, " Und zweite Stimme.")])])}
        decision = language.get("decision") or lang.forced("de", list(tracks), "Vorgabe")
        stats = {"timing": {"load_s": 1.0, "transcribe_s": 3.0, "wall_s": 4.5},
                 "threads": {"lanes": 2, "threads_per_lane": 2, "total_threads": 4}, "pieces": 2,
                 "decoding": {"profile": profile or "ausgewogen"},
                 "tracks": {"mic": {"audio_s": 6.0, "lane_s": 1.0, "skipped": ""},
                            "remote": {"audio_s": 6.0, "lane_s": 2.0, "skipped": ""}}}
        return SessionResult(asr, decision, stats)


def _session_pipeline(tmp_path: Path, worker=None, cfg: Config | None = None):
    from callrec.audio.finalize import load_mono
    from callrec.pipeline import Components, Pipeline
    from callrec.speakers.profiles import PlainFilePersistence, ProfileStore
    from tests.test_pipeline import FakeEmbedder, FakeSummarizer, short_cfg

    tr = FakeSession()
    comps = Components(transcriber_factory=lambda: tr, diarizer_factory=None, embedder_factory=FakeEmbedder,
                       profiles=ProfileStore(PlainFilePersistence(tmp_path / "vp.json")),
                       summarizer_factory=FakeSummarizer, audio_loader=load_mono, speaker_worker=worker)
    return Pipeline(cfg or short_cfg(), comps), tr


def _rec(tmp_path: Path):
    from callrec.audio.capture import REMOTE_PART

    rec = make_recording(tmp_path / "r")
    write_wav(rec.folder / "audio" / REMOTE_PART, np.concatenate([tone(330, 3.0, 44100), tone(550, 3.0, 44100)]), 44100)
    return rec


def _fake_speaker_worker(record: list):
    def run(audio, threads, work_dir, progress):
        record.append({"audio": Path(audio).name, "threads": threads, "thread": threading.current_thread().name})
        progress(0.5)
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        env = Path(work_dir) / "env.npy"
        np.save(env, np.ones(300, np.float32))
        return {"turns": [{"start": 1.0, "end": 2.9, "speaker": "S1"}, {"start": 3.0, "end": 5.9, "speaker": "S2"}],
                "embeddings": {"S1": [1.0] + [0.0] * 15, "S2": [0.0, 1.0] + [0.0] * 14}, "model_id": "fake-embedder",
                "silent": False, "envelope": str(env),
                "timing": {"audio_s": 6.0, "diarize_s": 2.0, "embed_s": 0.5, "num_threads": threads}}
    return run


def test_pipeline_session_path_records_profile_timings_and_mix_after_render(tmp_path: Path):
    record: list = []
    pipeline, tr = _session_pipeline(tmp_path, _fake_speaker_worker(record))
    pipeline.cfg.processing.parallel_diarization = "off"
    rec = _rec(tmp_path)
    result = pipeline.run(rec, profile="schnell")
    assert result.steps_done == ["finalize", "transcribe", "diarize", "identify", "render", "summarize"]
    assert tr.calls[0]["profile"] == "schnell" and tr.calls[0]["language"]["mode"] == "auto"
    assert tr.calls[0]["reserve"] == 0 and record[0]["thread"] == "MainThread" and record[0]["audio"] == "remote.flac"
    meta = rec.load_meta()
    p = meta["processing"]
    assert p["transcription"]["profile"] == "schnell" and p["transcription"]["timing"]["rtf"] == 0.75
    assert p["transcription"]["settings"]["effective_profile"]["profile"] == "schnell"
    assert p["diarization"]["speakers"] == 2 and p["diarization"]["timing"]["parallel"] is False
    t = p["timings"]
    assert set(t["steps"]) == {"finalize", "transcribe", "diarize", "identify", "render", "summarize"}
    assert t["transcription"]["threads"]["lanes"] == 2 and "mix_s" in t and p["timings_full"] == t
    assert (rec.folder / "audio" / "mix.flac").exists() and meta["audio"]["files"]["mix"] == "mix.flac"
    md = rec.transcript_path.read_text(encoding="utf-8")
    assert "Sprecher 1:** Hallo, erste Stimme." in md and "Sprecher 2:** Und zweite Stimme." in md
    from callrec.proctimes import load_history, recording_text

    assert recording_text(meta).startswith("Verarbeitung: ") and "Gespräch" in recording_text(meta)
    assert load_history()[-1]["profile"] == "schnell"


def test_pipeline_runs_the_speaker_separation_alongside(tmp_path: Path):
    record: list = []
    pipeline, tr = _session_pipeline(tmp_path, _fake_speaker_worker(record))
    pipeline.cfg.processing.parallel_diarization = "on"
    rec = _rec(tmp_path)
    pipeline.run(rec)
    assert record[0]["thread"] == "diarize-parallel" and record[0]["threads"] == 2
    from callrec.speakers.threads import physical_cores

    assert tr.calls[0]["reserve"] == (2 if physical_cores() > 2 else 0)
    timing = rec.load_meta()["processing"]["diarization"]["timing"]
    assert timing["parallel"] is True and "parallel_wall_s" in timing
    from callrec.proctimes import recording_text

    assert "gleichzeitig" in recording_text(rec.load_meta())


def test_parallel_speaker_separation_error_surfaces_in_its_step(tmp_path: Path):
    def broken(audio, threads, work_dir, progress):
        raise RuntimeError("Sprechertrennung kaputt")

    pipeline, _tr = _session_pipeline(tmp_path, broken)
    pipeline.cfg.processing.parallel_diarization = "on"
    rec = _rec(tmp_path)
    with pytest.raises(RuntimeError, match="kaputt"):
        pipeline.run(rec)
    assert rec.load_meta()["processing"]["status"].startswith("error")
    assert rec.load_asr("mic") is not None  # the transcript of the session is kept


def test_parallel_decision_auto_off_and_benchmark(tmp_path: Path, monkeypatch):
    from callrec import pipeline as pl
    from callrec.asr import benchmark as bm

    pipeline, _tr = _session_pipeline(tmp_path, _fake_speaker_worker([]))
    import callrec.speakers.threads as th

    monkeypatch.setattr(th, "physical_cores", lambda: 8)
    assert pipeline.parallel_diarization() is True  # auto without benchmark: >= 6 cores
    monkeypatch.setattr(th, "physical_cores", lambda: 4)
    assert pipeline.parallel_diarization() is False
    bm.result_file().parent.mkdir(parents=True, exist_ok=True)
    bm.result_file().write_text(json.dumps({"parallel_diarization": True}), encoding="utf-8")
    assert pipeline.parallel_diarization() is True  # measured
    pipeline.cfg.processing.parallel_diarization = "off"
    assert pipeline.parallel_diarization() is False
    no_worker, _ = _session_pipeline(tmp_path)
    no_worker.cfg.processing.parallel_diarization = "on"
    assert no_worker.parallel_diarization() is False
    assert pl.PARALLEL_DIAR_THREADS == bm.DIAR_PARALLEL_THREADS


def test_pause_mode_waits_while_a_call_runs(tmp_path: Path, monkeypatch):
    from callrec import pipeline as pl
    from callrec.speakers import threads as th

    monkeypatch.setattr(pl, "PAUSE_POLL_S", 0.05)
    pipeline, tr = _session_pipeline(tmp_path, _fake_speaker_worker([]))
    pipeline.cfg.processing.during_call = "pause"
    rec = _rec(tmp_path)
    th.set_call_active(True)
    seen: list = []
    threading.Timer(0.3, lambda: th.set_call_active(False)).start()
    try:
        pipeline.run(rec, {"finalize", "transcribe"}, lambda s, f, m: seen.append((s, m)))
    finally:
        th.set_call_active(False)
    assert ("transcribe", "pausiert – Teams-Anruf läuft") in seen and tr.calls
    # cancelled while paused
    th.set_call_active(True)
    try:
        from callrec.pipeline import ProcessingCancelled

        with pytest.raises(ProcessingCancelled):
            pipeline.run(rec, {"render"}, should_cancel=lambda: bool(seen.append(("x", "")) or len(seen) > 40))
    finally:
        th.set_call_active(False)


def test_language_spec_forced_and_auto(tmp_path: Path):
    pipeline, tr = _session_pipeline(tmp_path)
    rec = _rec(tmp_path)
    pipeline.run(rec, {"finalize", "transcribe"}, language="en")
    assert tr.calls[-1]["language"]["mode"] == "forced"
    assert rec.load_meta()["processing"]["transcription"]["language_detection"]["language"] == "en"
    pipeline.run(rec, {"transcribe"}, profile="genau")
    spec = tr.calls[-1]["language"]
    assert spec["mode"] == "auto" and spec["windows"] == 6 and spec["early_stop"] is False
    pipeline.cfg.transcription.language = "de"
    pipeline.run(rec, {"transcribe"})
    assert tr.calls[-1]["language"]["decision"]["mode"] == "forced"


# -- speaker worker ------------------------------------------------------------------------------
def test_speaker_worker_job_with_fakes(tmp_path: Path):
    from callrec.models import SpeakerTurn
    from callrec.speakers import worker as sw
    from tests.test_pipeline import FakeEmbedder

    wav = tmp_path / "remote.wav"
    write_wav(wav, np.concatenate([tone(330, 3.0, 16000), tone(550, 3.0, 16000)]), 16000)

    class D:
        def diarize(self, samples, sr, progress):
            progress(1.0)
            return [SpeakerTurn(0.0, 2.9, "S1"), SpeakerTurn(3.0, 5.9, "S2")]

    lines: list[str] = []
    res = sw.run_job({"audio": str(wav), "out": str(tmp_path / "o.json"), "threads": 2}, lines.append, D(), FakeEmbedder())
    assert [t["speaker"] for t in res["turns"]] == ["S1", "S2"] and set(res["embeddings"]) == {"S1", "S2"}
    assert res["model_id"] == "fake-embedder" and Path(res["envelope"]).exists() and "PROGRESS 1.0" in lines
    silent = tmp_path / "silent.wav"
    write_wav(silent, np.zeros(16000, np.float32), 16000)
    assert sw.run_job({"audio": str(silent), "out": str(tmp_path / "s.json")}, lines.append, D(), FakeEmbedder())["silent"]


def test_speaker_worker_child_process_roundtrip(tmp_path: Path):
    from callrec.asr.transcriber import TranscriptionError
    from callrec.speakers.worker import run_in_child

    script = tmp_path / "w.py"
    script.write_text("import json, sys\na = sys.argv\nprint('PROGRESS 0.5', flush=True)\n"
                      "json.dump({'turns': [], 'job': json.load(open(a[a.index('--job') + 1]))}, open(a[a.index('--out') + 1], 'w'))\n",
                      encoding="utf-8")
    seen: list[float] = []
    res = run_in_child({"audio": "x", "threads": 2}, tmp_path / "w", seen.append, [sys.executable, str(script)])
    assert res["job"]["threads"] == 2 and seen == [0.5]
    with pytest.raises(TranscriptionError):
        run_in_child({}, tmp_path / "w", command_prefix=[sys.executable, "-c", "import sys; sys.exit(2)"])


# -- timings and estimates -------------------------------------------------------------------------
def test_formatting_and_summary_text():
    from callrec import proctimes as pt

    assert pt.fmt_duration(400) == "6 min 40 s" and pt.fmt_duration(45) == "45 s" and pt.fmt_duration(3900) == "1 h 5 min"
    assert pt.fmt_minutes(2280) == "38 min" and pt.fmt_minutes(3600) == "1 h"
    entry = {"steps": {"transcribe": 250.0, "diarize": 120.0, "render": 0.3}, "total_s": 400.0, "audio_s": 2280.0,
             "transcription": {"threads": {"lanes": 2, "threads_per_lane": 4, "total_threads": 8}},
             "power": {"on_battery": True}}
    text = pt.summary_text(entry)
    assert text.startswith("Verarbeitung: 6 min 40 s für 38 min Gespräch (Transkription 4 min 10 s, Sprechertrennung 2 min")
    assert "Transkript " not in text and "2 Spuren × 4 Threads" in text and "Akkubetrieb" in text
    assert pt.recording_text({"processing": {"timings": entry}}) == text
    assert pt.recording_text({}) == ""


def test_history_rates_and_remaining_time(tmp_path: Path):
    from callrec import proctimes as pt

    path = tmp_path / "h.json"
    for i in range(45):
        pt.append_history({"audio_s": 1800.0, "profile": "ausgewogen" if i % 2 else "genau",
                           "steps": {"transcribe": 900.0 if i % 2 else 1800.0, "diarize": 180.0}}, path)
    hist = pt.load_history(path)
    assert len(hist) == pt.HISTORY_MAX
    rates = pt.stage_rates(hist, "ausgewogen")
    assert rates["transcribe"] == pytest.approx(0.5) and rates["diarize"] == pytest.approx(0.1)
    assert pt.stage_rates(hist, "genau")["transcribe"] == pytest.approx(1.0)
    steps = {"finalize", "transcribe", "diarize", "identify", "render", "summarize"}
    eta = pt.estimate_remaining(steps, "transcribe", 0.0, 3600.0, rates)
    assert eta == pytest.approx(1800.0 + 360.0)
    mid = pt.estimate_remaining(steps, "transcribe", 0.5, 3600.0, rates, current_elapsed_s=900.0)
    assert mid == pytest.approx(900.0 + 360.0)
    assert pt.estimate_remaining(steps, "diarize", 0.0, 3600.0, {"transcribe": 0.5}) is None  # never measured
    assert pt.estimate_remaining(steps, "transcribe", 0.0, 0.0, rates) is None
    assert pt.remaining_text(None) == "" and pt.remaining_text(30) == "noch weniger als 1 min"
    assert pt.remaining_text(725) == "noch etwa 12 min"


def test_status_text_with_estimate_and_job_profile(tmp_path: Path):
    from callrec.jobs import ProcessingQueue, status_text

    assert status_text("transcribe", 0.4, "", "Call", 725.0) == "Transkription 40 % – noch etwa 12 min – Call"
    assert status_text("transcribe", 0.4, "", "Call") == "Transkription 40 % – Call"
    seen: dict = {}

    class P:
        def run(self, rec, steps, progress, profile=None, should_cancel=None):
            seen["profile"] = profile
            progress("transcribe", 0.5, "")
            from callrec.pipeline import ProcessResult

            return ProcessResult()

    q = ProcessingQueue(lambda **kw: P())
    rec = make_recording(tmp_path / "x")
    done = threading.Event()
    q.submit(rec, {"transcribe"}, on_done=lambda *a: done.set(), profile="genau")
    assert done.wait(5)
    q.stop()
    assert seen["profile"] == "genau"


# -- benchmark logic ---------------------------------------------------------------------------------
def test_benchmark_candidates_choice_and_report():
    from callrec.asr import benchmark as bm

    cfgs = bm.thread_configs(8)
    assert [(c["lanes"], c["threads"], c["budget"]) for c in cfgs] == [(1, 4, 8), (1, 8, 8), (2, 4, 8), (2, 3, 6)]
    assert [(c["lanes"], c["threads"]) for c in bm.thread_configs(4)] == [(1, 4), (2, 2)]
    assert [(c["lanes"], c["threads"]) for c in bm.thread_configs(2)] == [(1, 2)]
    assert (3, 4) in [(c["lanes"], c["threads"]) for c in bm.thread_configs(12)]
    runs = [{"label": "bisher (Standard von CTranslate2)", "lanes": 1, "threads": 4, "budget": 8, "profile": "ausgewogen",
             "batched": False, "rtf": 0.40},
            {"label": "1 Spur mit allen Kernen", "lanes": 1, "threads": 8, "budget": 8, "profile": "ausgewogen",
             "batched": False, "rtf": 0.30},
            {"label": "2 parallele Spuren", "lanes": 2, "threads": 4, "budget": 8, "profile": "ausgewogen",
             "batched": False, "rtf": 0.22},
            {"label": "2 Spuren neben der Sprechertrennung", "lanes": 2, "threads": 3, "budget": 6,
             "profile": "ausgewogen", "batched": False, "rtf": 0.26},
            {"label": "2 parallele Spuren", "lanes": 2, "threads": 4, "budget": 8, "profile": "genau", "batched": False,
             "rtf": 0.25, "agreement": 1.0},
            {"label": "2 parallele Spuren", "lanes": 2, "threads": 4, "budget": 8, "profile": "schnell", "batched": True,
             "rtf": 0.19, "agreement": 0.962}]
    res = {"runs": runs, "diarization": {"rtf_full": 0.08, "rtf_parallel": 0.11, "threads_full": 7, "threads_parallel": 2},
           "clip": {"seconds": 90, "speech_s": 80}, "cores": 8, "model": "large-v3-turbo", "at": "2026-11-23T10:00:00+01:00"}
    ch = bm.choose(res, 8)
    assert ch["tuned"] == {"cores": 8, "lanes": 2, "threads": 4, "rtf": 0.22}
    assert ch["speedup_vs_before"] == pytest.approx(1.82)
    # sequential 0.22*0.9 + 0.08*0.7 = 0.254; parallel max(0.26*0.9, 0.11*0.7) = 0.234 -> parallel
    assert ch["parallel_diarization"] is True and ch["estimate_rtf"]["parallel"] < ch["estimate_rtf"]["sequential"]
    res.update(ch)
    text = "\n".join(bm.report_lines(res))
    assert "Schnell" in text and "Übereinstimmung mit „Genau“ 96,2 %" in text and "1,8× so schnell wie bisher" in text
    assert "gleichzeitig mit der Transkription" in text and "Geschätzt für ein Gespräch von 60 min: etwa 14 min" in text
    assert bm.choose({"runs": runs[:3]}, 8).get("parallel_diarization") is None


def test_benchmark_clip_helpers_and_storage(tmp_path: Path):
    from callrec.asr import benchmark as bm

    chunks = _chunks((0, 5), (100, 150), (160, 190), (400, 410))
    start, speech = bm.densest_window(chunks, 90)
    assert start == 100 * SR and speech == pytest.approx(80.0)
    assert bm.clip_chunks(chunks, 120 * SR, 170 * SR) == _chunks((120, 150), (160, 170))
    assert bm.word_agreement(["a", "b", "c"], ["a", "b", "c"]) == 1.0 and bm.word_agreement([], []) == 1.0
    assert bm.word_agreement(["a", "b"], ["a", "x"]) == 0.5
    assert bm.load_tuned() is None and bm.parallel_decision() is None
    bm.result_file().parent.mkdir(parents=True, exist_ok=True)
    bm.result_file().write_text(json.dumps({"tuned": {"cores": 4, "lanes": 2, "threads": 2}}), encoding="utf-8")
    assert bm.load_tuned() == {"cores": 4, "lanes": 2, "threads": 2}
    ov = bm.speed_overview(Config())
    assert ov["measured"] and ov["plan"].startswith("Whisper rechnet mit")


def test_benchmark_worker_main_with_stub_model(tmp_path: Path, monkeypatch):
    import faster_whisper

    from callrec.asr import benchmark as bm
    from callrec.asr import worker

    audio = np.zeros(SR * 200, np.float32)
    monkeypatch.setattr(worker, "_decode_track", lambda p: audio)
    monkeypatch.setattr(worker, "_vad_chunks", lambda x, vad: _chunks((0, 40), (50, 85)) if len(x) == len(audio)
                        else [{"start": 0, "end": min(len(x), 40 * SR)}])

    class StubBatched:
        def __init__(self, model):
            self.model = model

        def transcribe(self, audio, **kw):
            return self.model.transcribe(audio, **kw)

    monkeypatch.setattr(faster_whisper, "BatchedInferencePipeline", StubBatched)
    StubWhisper.instances = []
    job = {"model": "large-v3-turbo", "seconds": 60, "language": "de", "tracks": {"mic": "a", "remote": "b"},
           "configs": bm.thread_configs(4), "profiles": {k: v.to_dict() for k, v in prof.PROFILES.items()}, "options": {}}
    (tmp_path / "job.json").write_text(json.dumps(job), encoding="utf-8")
    assert bm.worker_main(str(tmp_path / "job.json"), str(tmp_path / "out.json"), model_factory=StubWhisper) == 0
    res = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert [(r["profile"], r["batched"]) for r in res["runs"]] == [
        ("ausgewogen", False), ("ausgewogen", False), ("schnell", True), ("ausgewogen", True), ("genau", False)]
    assert res["clip"]["track"] == "mic" and res["runs"][-1]["agreement"] == 1.0
    assert len(StubWhisper.instances) in (2, 3)  # one load per layout; the fastest again unless it is the last


# -- power -----------------------------------------------------------------------------------------
def test_power_state_mapping_and_hints():
    from callrec import power

    assert power.from_windows_status(0, 1, 80, 0) == power.PowerState(True, False, 80)
    assert power.from_windows_status(1, 8, 100, 1).saver is True
    assert power.from_windows_status(1, 128, 255, 0) == power.PowerState(None, False, None)  # desktop, no battery
    assert "Energiesparmodus" in power.PowerState(False, True).warning()
    assert "Akkubetrieb" in power.PowerState(True, False).warning()
    assert power.PowerState(False, False).warning() == "" and not power.PowerState().slow

    def broken() -> power.PowerState:
        raise OSError("x")

    assert power.power_state(broken) == power.PowerState()


# -- UI and CLI wiring ------------------------------------------------------------------------------
def test_settings_schema_offers_the_profiles_and_the_speed_panel():
    from callrec.ui import settingsmodel as sm

    page = next(p for p in sm.PAGES if p.key == "transcription")
    ids = {f.id: f for f in page.fields}
    assert [c[0] for c in ids["transcription.profile"].choices] == ["schnell", "ausgewogen", "genau", "eigene"]
    assert all(" – " in c[1] for c in ids["transcription.profile"].choices)
    assert "processing.during_call" in ids and "processing.parallel_diarization" in ids and "speed" in page.extras
    assert "distil-large-v3.5" not in sm.WHISPER_MODELS  # English only and not faster on a CPU
    cfg = sm.apply_patch(Config(), {"transcription.profile": "genau", "processing.during_call": "pause"})
    assert cfg.transcription.profile == "genau" and cfg.processing.during_call == "pause"
    with pytest.raises(ConfigError):
        sm.apply_patch(Config(), {"transcription.profile": "turbo"})


def test_recording_detail_shows_processing_time(tmp_path: Path):
    from callrec.ui.dto import _processing_text

    assert _processing_text({"processing": {"timings": {"steps": {"transcribe": 60.0}, "total_s": 61.0,
                                                        "audio_s": 600.0}}}).startswith("Verarbeitung: 1 min 1 s für 10 min")
    assert _processing_text({"processing": {"timings": "kaputt"}}) == ""


def test_cli_benchmark_and_profile_options():
    from callrec.cli import build_parser

    p = build_parser()
    a = p.parse_args(["benchmark", "--seconds", "60", "--no-diarization"])
    assert a.seconds == 60.0 and a.no_diarization and a.func.__name__ == "cmd_benchmark"
    assert p.parse_args(["retranscribe", "X", "--profile", "schnell"]).profile == "schnell"
    assert p.parse_args(["process", "X", "--profile", "genau"]).profile == "genau"
    assert p.parse_args(["summarize", "X"]).profile is None
    with pytest.raises(SystemExit):
        p.parse_args(["retranscribe", "X", "--profile", "turbo"])


def test_cli_benchmark_show(tmp_path: Path, capsys):
    from callrec.cli import main

    assert main(["benchmark", "--show"]) == 0
    assert "Noch keine Messung" in capsys.readouterr().out


def test_web_retranscribe_dialog_returns_the_profile():
    from callrec.ui.webdialogs import WebDialogPort

    class Broker:
        def __init__(self, answer):
            self.answer, self.asked = answer, None

        def ask(self, kind, data):
            self.asked = data
            return self.answer

    class Rec:
        def title(self):
            return "Call"

    d = WebDialogPort.__new__(WebDialogPort)
    d.broker = Broker({"ok": True, "language": "de", "preset": "", "profile": "schnell"})
    assert d.ask_retranscribe(Rec(), []) == ("de", None, "schnell")
    assert [p["key"] for p in d.broker.asked["profiles"]] == ["schnell", "ausgewogen", "genau"]
    d.broker = Broker({"ok": True, "language": "de", "preset": "", "profile": "turbo"})
    assert d.ask_retranscribe(Rec(), []) is None
    d.broker = Broker({"ok": True, "language": "en"})
    assert d.ask_retranscribe(Rec(), []) == ("en", None, None)


def test_distil_models_get_an_honest_note():
    from callrec.asr.gpu import plan_asr
    from callrec.config import TranscriptionConfig

    note = plan_asr(TranscriptionConfig(model="distil-large-v3.5", device="cpu"), False).note
    assert "nur für Englisch" in note and "nicht schneller" in note
