"""End-to-end check of the 0.4 transcription session against a synthetic ground truth.

The real session code (:func:`callrec.asr.worker.run_session`: piece planning, lanes, faster-whisper's
``restore_speech_timestamps``, clamping, reassembly, the language detection) runs on a deterministic
stand-in for Whisper that "transcribes" by reading the word ids encoded in the samples it is given.
Every word of the ground truth must come out exactly once, in order, on the recording's clock -
for many random pause layouts, lane counts and all profiles - and the result must equal the old
0.3 path (one concatenation of all speech chunks of a track, as faster-whisper's own VAD path does).
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from callrec.asr import profiles as prof
from callrec.asr import session as ses

SR = 16000


# -- synthetic ground truth -------------------------------------------------------------------------
def make_track(rng: random.Random, duration_s: float, first_id: int, lang_of: dict[int, str], en_share: float = 0.05,
               pauses: tuple[float, float] = (0.1, 9.0), monologue: bool = False, min_silence_s: float = 2.0,
               pad_s: float = 0.4, max_chunk_s: float | None = None):
    """Samples carrying word ids (silence = 0), the true words (id, start, end) and VAD-like chunks."""
    n = int(duration_s * SR)
    samples = np.zeros(n, np.float32)
    words: list[tuple[int, float, float]] = []
    utterances: list[tuple[int, int]] = []
    pos = int(rng.uniform(0, 3) * SR) if rng.random() < 0.7 else 0  # sometimes speech right at 0
    wid = first_id
    while pos < n:
        u_start = pos
        n_words = rng.randint(60, 140) if monologue else rng.randint(1, 25)
        lang = "en" if rng.random() < en_share else "de"
        for _ in range(n_words):
            w_len = int(rng.uniform(0.12, 0.7) * SR)
            if pos + w_len > n:
                break
            samples[pos:pos + w_len] = wid
            words.append((wid, pos / SR, (pos + w_len) / SR))
            lang_of[wid] = lang
            wid += 1
            pos += w_len + int(rng.uniform(0.0, 0.25) * SR)
        utterances.append((u_start, min(pos, n)))
        pos += int(rng.uniform(*pauses) * SR)
    # VAD: utterances closer than min_silence_s are one chunk, padded, never overlapping
    chunks: list[dict[str, int]] = []
    for s, e in utterances:
        if chunks and s - chunks[-1]["end"] < min_silence_s * SR:
            chunks[-1]["end"] = e
        else:
            chunks.append({"start": s, "end": e})
    pad = int(pad_s * SR)
    out: list[dict[str, int]] = []
    for i, c in enumerate(chunks):
        lo = max(0, c["start"] - pad, out[-1]["end"] if out else 0)
        hi = min(n, c["end"] + pad)
        if i + 1 < len(chunks):
            hi = min(hi, (c["end"] + chunks[i + 1]["start"]) // 2)
        out.append({"start": lo, "end": hi})
    if max_chunk_s:  # like Silero with max_speech_duration_s: split long chunks in pauses between words
        split: list[dict[str, int]] = []
        for c in out:
            s = c["start"]
            while c["end"] - s > max_chunk_s * SR:
                limit = s + int(max_chunk_s * SR)
                cut = max((round(w[2] * SR) for w in words if s < round(w[1] * SR) and round(w[2] * SR) <= limit),
                          default=limit)
                split.append({"start": s, "end": cut})
                s = cut
            split.append({"start": s, "end": c["end"]})
        out = split
    return samples, words, out


# -- deterministic Whisper stand-in ----------------------------------------------------------------
def _runs(audio: np.ndarray) -> list[tuple[int, int, int]]:
    """(word id, start, end) for each run of one non-zero value."""
    a = np.asarray(audio)
    if not a.size:
        return []
    change = np.flatnonzero(np.diff(a) != 0) + 1
    bounds = np.concatenate([[0], change, [a.size]])
    return [(int(a[s]), int(s), int(e)) for s, e in zip(bounds[:-1], bounds[1:], strict=True) if a[s] != 0]


def _segments(runs: list[tuple[int, int, int]], offset_s: float = 0.0) -> list[SimpleNamespace]:
    segs: list[SimpleNamespace] = []
    cur: list[SimpleNamespace] = []

    def flush() -> None:
        if cur:
            segs.append(SimpleNamespace(start=cur[0].start, end=cur[-1].end, text=" ".join(w.word for w in cur),
                                        words=list(cur), avg_logprob=-0.2, compression_ratio=1.3, temperature=0.0))
            cur.clear()

    for wid, s, e in runs:
        w = SimpleNamespace(start=offset_s + s / SR, end=offset_s + e / SR, word=f" w{wid}", probability=0.9)
        if cur and (w.start - cur[-1].end > 1.0 or len(cur) >= 12):
            flush()
        cur.append(w)
    flush()
    return segs


class _FE:
    n_samples, nb_max_frames, sampling_rate = 30 * SR, 3000, SR

    def __call__(self, seg):  # "features": the word ids of every 10-ms frame
        return np.tile(np.asarray(seg, np.float32)[::160], (2, 1))


class TruthWhisper:
    """WhisperModel stand-in: reads the word ids of the samples it gets; language detection from them."""

    instances: list[TruthWhisper] = []
    lang_of: dict[int, str] = {}

    def __init__(self, model, device, compute_type, download_root, local_files_only=False, **kw):
        self.kw = kw
        self.calls: list[dict] = []
        outer = self

        class _Inner:
            is_multilingual = True

            def detect_language(self, enc):
                ids = [int(x) for x in np.asarray(enc)[0] if x > 0]
                en = sum(1 for i in ids if outer.lang_of.get(i) == "en")
                p_en = (en + 0.5) / (len(ids) + 1.0)
                pairs = sorted([("<|en|>", p_en), ("<|de|>", 1.0 - p_en)], key=lambda x: -x[1])
                return [pairs]

        self.model = _Inner()
        self.feature_extractor = _FE()
        TruthWhisper.instances.append(self)

    def encode(self, features):
        return features

    def transcribe(self, audio, language=None, vad_filter=False, **kw):
        assert vad_filter is False
        self.calls.append({"n": len(audio), "language": language, **kw})
        return iter(_segments(_runs(audio))), SimpleNamespace(language=language)


class TruthBatched:
    """BatchedInferencePipeline stand-in: every clip is one 30-s window (longer clips are cut, as in 1.2.1)."""

    def __init__(self, model):
        self.model = model

    def transcribe(self, audio, language=None, vad_filter=False, clip_timestamps=None, **kw):
        assert vad_filter is False and clip_timestamps
        out: list[SimpleNamespace] = []
        for clip in clip_timestamps:
            s, e = int(round(clip["start"] * SR)), int(round(clip["end"] * SR))
            e = min(e, s + 30 * SR)  # only the first 30 s of a clip are transcribed
            runs = [r for r in _runs(audio[s:e]) if not (r[2] == e - s and e < int(round(clip["end"] * SR)))]
            out.extend(_segments(runs, s / SR))
        return iter(out), SimpleNamespace(language=language)


def _old_path(samples: np.ndarray, chunks: list[dict[str, int]]) -> list[tuple[str, float, float]]:
    """0.3: faster-whisper's own VAD path = all speech chunks of the track concatenated, restored once."""
    if not chunks:
        return []
    piece = ses.Piece("t", list(chunks))
    segs = ses.transcribe_piece(TruthWhisper("m", "cpu", "int8", None), samples, piece, "de", {}, False)
    return [(w["text"], w["start"], w["end"]) for s in segs for w in s["words"]]


def run_truth_session(tmp_path: Path, monkeypatch, tracks: dict[str, tuple], lanes: int, profile: str,
                      language: dict | None = None, vad_filter: bool = True) -> dict:
    import faster_whisper

    from callrec.asr import language as lang
    from callrec.asr import worker

    audio = {name: t[0] for name, t in tracks.items()}
    chunks = {name: t[2] for name, t in tracks.items()}
    monkeypatch.setattr(worker, "_decode_track", lambda p: audio[p])
    if vad_filter:
        monkeypatch.setattr(worker, "_vad_chunks", lambda x, vad: [dict(c) for c in chunks[_name_of(audio, x)]])
    monkeypatch.setattr(faster_whisper, "BatchedInferencePipeline", TruthBatched)
    TruthWhisper.instances = []
    job = {"model": "large-v3-turbo", "device": "cpu", "compute_type": "auto", "lanes": lanes, "threads": 2,
           "tracks": {n: {"audio": n, "out": str(tmp_path / f"asr_{n}.json")} for n in tracks},
           "language": language or {"mode": "forced", "decision": lang.forced("de", list(tracks))},
           "decoding": prof.PROFILES[profile].to_dict(), "options": {}, "vad_filter": vad_filter}
    res = worker.run_session(job, model_factory=TruthWhisper)
    res["asr"] = {n: json.loads((tmp_path / f"asr_{n}.json").read_text(encoding="utf-8")) for n in tracks}
    return res


def _name_of(audio: dict[str, np.ndarray], x: np.ndarray) -> str:
    return next(n for n, a in audio.items() if a is x)


def _words(asr: dict) -> list[tuple[str, float, float]]:
    return [(w["text"], w["start"], w["end"]) for s in asr["segments"] for w in s["words"]]


def assert_exact(out: list[tuple[str, float, float]], truth: list[tuple[int, float, float]], tol: float = 0.011) -> None:
    got, want = [w[0] for w in out], [f" w{t[0]}" for t in truth]
    if got != want:
        lost = sorted(set(want) - set(got), key=want.index)
        dup = sorted({w for w in got if got.count(w) > 1})
        lost_t = [t for t in truth if f" w{t[0]}" in lost][:5]
        raise AssertionError(f"lost {len(lost)} {lost[:10]} at {lost_t}, duplicated {dup[:10]}, "
                             f"order ok: {sorted(got, key=want.index) == got if not dup and not set(got) - set(want) else '?'}")
    for (_t, s, e), (_i, ts, te) in zip(out, truth, strict=True):
        assert abs(s - ts) <= tol and abs(e - te) <= tol, (s, e, ts, te)
    starts = [w[1] for w in out]
    assert starts == sorted(starts)


# -- the checks ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("lanes", [1, 2, 3])
def test_every_word_once_on_the_recording_clock(tmp_path: Path, monkeypatch, seed: int, lanes: int):
    rng = random.Random(seed * 10 + lanes)
    profile = prof.PROFILE_NAMES[seed % 3]
    batched = prof.PROFILES[profile].batched
    lang_of: dict[int, str] = {}
    TruthWhisper.lang_of = lang_of
    mic = make_track(rng, rng.uniform(200, 900), 1, lang_of, pauses=(0.1, rng.choice([3.0, 9.0, 30.0])),
                     max_chunk_s=30.0 if batched else None)
    remote = make_track(rng, rng.uniform(200, 900), 100_000, lang_of, monologue=seed % 4 == 0,
                        max_chunk_s=30.0 if batched else None)
    res = run_truth_session(tmp_path, monkeypatch, {"mic": mic, "remote": remote}, lanes, profile)
    for name, (samples, truth, chunks) in {"mic": mic, "remote": remote}.items():
        out = _words(res["asr"][name])
        assert_exact(out, truth)
        old = _old_path(samples, chunks)  # the 0.3 path: same words, same times (up to rounding)
        assert [w[0] for w in out] == [w[0] for w in old]
        assert max((abs(a[1] - b[1]) + abs(a[2] - b[2]) for a, b in zip(out, old, strict=True)), default=0) <= 0.021
        assert res["asr"][name]["duration"] == pytest.approx(len(samples) / SR)
    assert res["pieces"] >= (1 if lanes == 1 else 2)


def test_chunks_longer_than_one_window_lose_nothing_in_the_batched_profile(tmp_path: Path, monkeypatch):
    """VAD off (``vad_filter = false``) - the whole track is one chunk - and "Schnell" (batched)."""
    rng = random.Random(7)
    lang_of: dict[int, str] = {}
    TruthWhisper.lang_of = lang_of
    mic = make_track(rng, 150, 1, lang_of, pauses=(0.1, 1.0))
    remote = make_track(rng, 95, 100_000, lang_of, monologue=True)
    res = run_truth_session(tmp_path, monkeypatch, {"mic": mic, "remote": remote}, 2, "schnell", vad_filter=False)
    for name, (_s, truth, _c) in {"mic": mic, "remote": remote}.items():
        out = _words(res["asr"][name])
        assert {w[0] for w in out} >= {f" w{t[0]}" for t in truth[:3]}
        # words cut at a 30-s boundary may differ in the real model; none may vanish in bulk
        assert len(out) >= len(truth) - 4, (name, len(out), len(truth))


def test_merge_clips_never_returns_a_clip_longer_than_one_window():
    chunks = [{"start": 0, "end": 95 * SR}, {"start": 100 * SR, "end": 110 * SR}]
    clips = ses.merge_clips(chunks)
    assert all(c["end"] - c["start"] <= 30.0 + 1e-9 for c in clips)
    assert clips[0]["start"] == 0.0 and clips[-1]["end"] == pytest.approx(105.0)
    assert all(a["end"] == b["start"] for a, b in zip(clips, clips[1:], strict=False))


def test_silent_and_quiet_tracks(tmp_path: Path, monkeypatch):
    """A track without VAD speech is skipped (no words to lose); a quiet one with speech is transcribed."""
    rng = random.Random(3)
    lang_of: dict[int, str] = {}
    TruthWhisper.lang_of = lang_of
    silent = (np.zeros(SR * 120, np.float32), [], [])
    remote = make_track(rng, 300, 100_000, lang_of)
    res = run_truth_session(tmp_path, monkeypatch, {"mic": silent, "remote": remote}, 2, "ausgewogen")
    assert res["asr"]["mic"]["segments"] == [] and res["tracks"]["mic"]["skipped"]
    assert_exact(_words(res["asr"]["remote"]), remote[1])


@pytest.mark.parametrize("pattern, expected", [("greetings", "de"), ("german", "de"), ("english", "en")])
def test_language_detection_on_the_real_pattern(tmp_path: Path, monkeypatch, pattern: str, expected: str):
    """The user's calls: mostly German, sometimes English, unknown beforehand. English greetings at
    the start and the end of a German call (the first windows the early stop measures) must not
    turn the whole call into English; a real English call must still be recognised."""
    lang_of: dict[int, str] = {}
    TruthWhisper.lang_of = lang_of
    tracks = {}
    for name, first, seed in (("mic", 1, 11), ("remote", 100_000, 12)):
        rng = random.Random(seed)
        samples, words, chunks = make_track(rng, 1800, first, lang_of, en_share=1.0 if pattern == "english" else 0.0)
        if pattern == "greetings":  # the first and the last 90 s English
            for wid, s, _e in words:
                if s < words[0][1] + 90 or s > words[-1][1] - 90:
                    lang_of[wid] = "en"
        tracks[name] = (samples, words, chunks)
    spec = {"mode": "auto", "candidates": ["de", "en"], "threshold": 0.75, "min_speech_s": 20.0,
            "windows": 6, "early_stop": True}
    res = run_truth_session(tmp_path, monkeypatch, tracks, 2, "ausgewogen", language=spec)
    d = res["language_detection"]
    assert d["language"] == expected, d["reason"]
    assert {res["asr"][t]["language"] for t in tracks} == {expected}
    for name, (_s, truth, _c) in tracks.items():
        assert_exact(_words(res["asr"][name]), truth)
    if pattern == "greetings":  # the same as 0.3, which measured every window
        full = run_truth_session(tmp_path, monkeypatch, tracks, 2, "genau", language={**spec, "early_stop": False})
        assert full["language_detection"]["language"] == expected
    if pattern == "german":  # the early stop still saves time on the common case
        assert max(d["windows_measured"].values()) <= 3


# -- failure handling of the session ---------------------------------------------------------------
def test_end_of_the_control_pipe_ends_a_paused_worker():
    """App killed/updated while the worker pauses for a call: it must not wait forever."""
    gate = ses.LaneGate(2, 0)
    ended: list[bool] = []
    ses.read_control(iter(["LANES 0\n"]), gate, lambda: ended.append(True))
    assert ended == [True]
    gate.close()  # the session is finished: the end of stdin is then the normal end
    ses.read_control(iter([]), gate, lambda: ended.append(True))
    assert ended == [True]


def test_failed_session_falls_back_to_the_per_track_path(tmp_path: Path):
    from callrec.asr.transcriber import TranscriptionError
    from tests.test_pipeline import FakeTranscriber
    from tests.test_speed import _rec, _session_pipeline

    class BrokenSession(FakeTranscriber):
        selection_note = ""

        def transcribe_session(self, *a, **k):
            raise TranscriptionError("Transkription auf CPU fehlgeschlagen (Exit-Code 3221225477)")

    pipeline, _tr = _session_pipeline(tmp_path)
    broken = BrokenSession()
    pipeline.c.transcriber_factory = lambda: broken
    pipeline.cfg.processing.parallel_diarization = "off"
    rec = _rec(tmp_path)
    result = pipeline.run(rec)
    assert "transcribe" in result.steps_done and [c[0] for c in broken.calls] == ["mic.flac", "remote.flac"]
    t = rec.load_meta()["processing"]["transcription"]
    assert t["fallback"]["path"] == "per_track" and t["profile"] == "eigene"
    assert t["settings"]["effective_profile"]["beam_size"] == pipeline.cfg.transcription.beam_size
    assert any("Spur für Spur" in m for m in result.messages)
    assert rec.load_meta()["processing"]["status"] == "done"


def test_cancelled_session_does_not_fall_back(tmp_path: Path):
    from callrec.asr.transcriber import TranscriptionCancelled
    from tests.test_pipeline import FakeTranscriber
    from tests.test_speed import _rec, _session_pipeline

    class Cancelled(FakeTranscriber):
        selection_note = ""

        def transcribe_session(self, *a, **k):
            raise TranscriptionCancelled("abgebrochen")

    pipeline, _tr = _session_pipeline(tmp_path)
    tr = Cancelled()
    pipeline.c.transcriber_factory = lambda: tr
    rec = _rec(tmp_path)
    with pytest.raises(TranscriptionCancelled):
        pipeline.run(rec)
    assert tr.calls == [] and rec.load_meta()["processing"]["status"] == "cancelled"


def test_parallel_speaker_separation_failure_is_retried_alone(tmp_path: Path):
    from tests.test_speed import _fake_speaker_worker, _rec, _session_pipeline

    record: list = []
    ok = _fake_speaker_worker(record)
    calls: list[str] = []

    def flaky(audio, threads, work_dir, progress):
        import threading

        calls.append(threading.current_thread().name)
        if len(calls) == 1:
            raise RuntimeError("bad allocation")
        return ok(audio, threads, work_dir, progress)

    pipeline, _tr = _session_pipeline(tmp_path, flaky)
    pipeline.cfg.processing.parallel_diarization = "on"
    rec = _rec(tmp_path)
    pipeline.run(rec)
    assert calls == ["diarize-parallel", "MainThread"]
    d = rec.load_meta()["processing"]["diarization"]
    assert d["speakers"] == 2 and d["timing"]["parallel"] is False


def test_speaker_worker_ends_when_the_app_is_gone(tmp_path: Path):
    import subprocess
    import sys
    import time

    code = ("import sys, time\nfrom callrec.speakers.worker import watch_parent\nwatch_parent(sys.stdin)\n"
            "print('ready', flush=True)\ntime.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, cwd=str(Path(__file__).resolve().parents[1] / "src"))
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
        t0 = time.monotonic()
        assert proc.stdin is not None
        proc.stdin.close()  # the app is gone
        assert proc.wait(timeout=20) == 4 and time.monotonic() - t0 < 20
    finally:
        if proc.poll() is None:
            proc.kill()


# -- benchmark ---------------------------------------------------------------------------------------
FAKE_BENCH = r'''
import json, os, sys, time
a = sys.argv[1:]
job = json.load(open(a[a.index("--benchmark") + 1], encoding="utf-8"))
time.sleep(float(os.environ.get("FAKE_BENCH_SLEEP", "0")))
runs = [{"label": c["label"], "lanes": c["lanes"], "threads": c["threads"], "budget": c["budget"],
         "profile": "ausgewogen", "batched": False, "rtf": 0.5 - 0.01 * i, "words": 10}
        for i, c in enumerate(job["configs"])]
json.dump({"clip": {"track": "mic", "start_s": 0.0, "seconds": 5.0, "speech_s": 4.0}, "runs": runs},
          open(a[a.index("--out") + 1], "w", encoding="utf-8"))
'''


def _bench_setup(tmp_path: Path):
    import sys

    from callrec.asr.transcriber import WhisperSubprocessTranscriber
    from callrec.audio.finalize import finalize_recording
    from callrec.config import Config
    from tests.conftest import make_recording

    rec = make_recording(tmp_path / "r")
    finalize_recording(rec.folder, "flac", False, False)
    script = tmp_path / "bench.py"
    script.write_text(FAKE_BENCH, encoding="utf-8")
    cfg = Config()
    tr = WhisperSubprocessTranscriber(cfg.transcription, tmp_path / "models", [sys.executable, str(script)])
    return cfg, rec, tr


def test_benchmark_on_battery_is_shown_but_not_applied(tmp_path: Path, monkeypatch):
    from callrec import paths, power
    from callrec.asr import benchmark as bm

    cfg, rec, tr = _bench_setup(tmp_path)
    monkeypatch.setattr(power, "power_state", lambda reader=None: power.PowerState(True, False, 40))
    calls: list[int] = []

    def diarize(wav: Path, threads: int) -> float:
        calls.append(threads)
        d = wav.parent / "diarize"
        d.mkdir(exist_ok=True)
        (d / "diarize_result.json").write_text('{"embeddings": {"S1": [0.1]}}', encoding="utf-8")
        return 1.0

    res = bm.run_benchmark(cfg, rec.folder, 5.0, diarize=diarize, transcriber=tr)
    assert "tuned" not in res and res["not_applied"]["tuned"]["lanes"] >= 1
    assert bm.load_tuned() is None and bm.parallel_decision() is None  # automatic layout stays
    assert any("nicht übernommen" in line for line in bm.report_lines(res))
    work = paths.data_dir() / "benchmark"
    assert not (work / "diarize").exists() and not (work / "job.json").exists() and not (work / "clip.wav").exists()


def test_benchmark_is_stopped_when_a_recording_starts(tmp_path: Path, monkeypatch):
    import threading
    import time

    from callrec import power
    from callrec.asr import benchmark as bm
    from callrec.speakers import threads as th

    cfg, rec, tr = _bench_setup(tmp_path)
    monkeypatch.setattr(power, "power_state", lambda reader=None: power.PowerState())
    monkeypatch.setattr(bm, "BUSY_POLL_S", 0.1)
    monkeypatch.setenv("FAKE_BENCH_SLEEP", "30")
    threading.Timer(0.5, lambda: th.set_recording_active(True)).start()
    t0 = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="Messung abgebrochen: Aufnahme läuft"):
            bm.run_benchmark(cfg, rec.folder, 5.0, transcriber=tr)
    finally:
        th.set_recording_active(False)
    assert time.monotonic() - t0 < 15 and bm.load_result() is None
