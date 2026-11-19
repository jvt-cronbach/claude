from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from callrec.asr import worker
from callrec.asr.cuda import nvidia_bin_dirs
from callrec.asr.transcriber import TranscriptionError, WhisperSubprocessTranscriber, compute_type_for
from callrec.config import TranscriptionConfig

FAKE_WORKER = r'''
import argparse, json, sys, os
p = argparse.ArgumentParser()
p.add_argument("--audio"); p.add_argument("--out"); p.add_argument("--model"); p.add_argument("--device")
p.add_argument("--compute-type"); p.add_argument("--language"); p.add_argument("--beam-size")
p.add_argument("--vad", action=argparse.BooleanOptionalAction); p.add_argument("--condition-on-previous-text", action=argparse.BooleanOptionalAction)
p.add_argument("--initial-prompt", default=""); p.add_argument("--download-root")
a, _rest = p.parse_known_args()
if a.device == "cuda" and os.environ.get("FAKE_CUDA_BROKEN"):
    sys.stderr.write("Could not load library cudnn_ops64_9.dll\n")
    os._exit(3)  # like a hard native crash
print("PROGRESS 0.5", flush=True)
print("noise line", flush=True)
json.dump({"language": a.language, "duration": 2.0, "model": a.model, "device": a.device,
           "compute_type": a.compute_type, "segments": [{"start": 0, "end": 1, "text": a.initial_prompt or "hallo",
           "words": [{"start": 0, "end": 1, "text": " hallo", "probability": 0.9}]}]}, open(a.out, "w"))
print("PROGRESS 1.0", flush=True)
'''


@pytest.fixture(autouse=True)
def reset_cuda_flag():
    WhisperSubprocessTranscriber._cuda_failed = False
    yield
    WhisperSubprocessTranscriber._cuda_failed = False


def _transcriber(tmp_path: Path, **cfg) -> WhisperSubprocessTranscriber:
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER, encoding="utf-8")
    return WhisperSubprocessTranscriber(TranscriptionConfig(**cfg), tmp_path / "models", [sys.executable, str(script)])


def test_cuda_success(tmp_path: Path):
    t = _transcriber(tmp_path)
    progress: list[float] = []
    res = t.transcribe(tmp_path / "mic.flac", tmp_path / "asr_mic.json", initial_prompt="Gespräch mit Anna.", progress=progress.append)
    assert res.device == "cuda" and res.compute_type == "auto" and res.language == "de"
    assert res.segments[0].text == "Gespräch mit Anna."
    assert progress == [0.5, 1.0]


def test_cpu_fallback_after_native_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_CUDA_BROKEN", "1")
    t = _transcriber(tmp_path, compute_type="float16")
    res = t.transcribe(tmp_path / "mic.flac", tmp_path / "asr_mic.json")
    assert res.device == "cpu" and res.compute_type == "auto"  # float16 not used on CPU
    assert "cudnn_ops64_9.dll" in (tmp_path / "asr_mic.log").read_text(encoding="utf-8")
    assert t.devices_to_try() == ["cpu"]  # remembered


def test_cpu_only_and_total_failure(tmp_path: Path):
    t = _transcriber(tmp_path, device="cpu")
    assert t.devices_to_try() == ["cpu"]
    bad = WhisperSubprocessTranscriber(TranscriptionConfig(), tmp_path, [sys.executable, "-c", "import sys; sys.exit(1)"])
    with pytest.raises(TranscriptionError):
        bad.transcribe(tmp_path / "a.flac", tmp_path / "o.json")


def test_command_line_matches_worker_parser(tmp_path: Path):
    t = WhisperSubprocessTranscriber(TranscriptionConfig(language="auto", vad_filter=False), tmp_path)
    cmd = t.build_command(Path("a.flac"), Path("o.json"), "cuda", "Gespräch mit Anna.")
    assert cmd[1:3] == ["-m", "callrec.asr.worker"]
    args = worker.build_parser().parse_args(cmd[3:])
    assert args.device == "cuda" and args.vad is False and args.language == "auto"
    assert args.initial_prompt == "Gespräch mit Anna." and args.model == "large-v3"
    assert worker.resolve_compute_type("cuda", "auto") == "float16"
    assert worker.resolve_compute_type("cpu", "auto") == "int8"
    assert compute_type_for("cpu", "int8_float16") == "auto"
    assert compute_type_for("cuda", "int8_float16") == "int8_float16"


def test_worker_main_with_fake_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Exercises worker.run() against the real faster-whisper API surface with a stub model."""
    import faster_whisper
    from faster_whisper.transcribe import Segment, TranscriptionInfo, Word

    calls = {}

    class StubModel:
        def __init__(self, model, device, compute_type, download_root):
            calls["init"] = (model, device, compute_type, download_root)

        def transcribe(self, audio, **kw):
            calls["kw"] = kw
            seg = Segment(id=0, seek=0, start=0.0, end=1.5, text=" Hallo Welt", tokens=[], avg_logprob=0.0,
                          compression_ratio=1.0, no_speech_prob=0.0, temperature=0.0,
                          words=[Word(start=0.0, end=0.7, word=" Hallo", probability=0.9), Word(start=0.7, end=1.5, word=" Welt", probability=0.8)])
            info = TranscriptionInfo(language="de", language_probability=0.99, duration=3.0, duration_after_vad=1.5,
                                     all_language_probs=None, transcription_options=None, vad_options=None)  # type: ignore[arg-type]
            return iter([seg]), info

    monkeypatch.setattr(faster_whisper, "WhisperModel", StubModel)
    out = tmp_path / "o.json"
    rc = worker.main(["--audio", "x.flac", "--out", str(out), "--device", "cpu", "--language", "auto", "--initial-prompt", "P"])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["segments"][0]["words"][1] == {"start": 0.7, "end": 1.5, "text": " Welt", "probability": 0.8}
    assert calls["init"][1:3] == ("cpu", "int8")
    assert calls["kw"]["language"] is None and calls["kw"]["word_timestamps"] is True
    assert calls["kw"]["initial_prompt"] == "P" and calls["kw"]["vad_filter"] is True


def test_nvidia_dirs(tmp_path: Path):
    (tmp_path / "nvidia" / "cudnn" / "bin").mkdir(parents=True)
    (tmp_path / "nvidia" / "cublas" / "bin").mkdir(parents=True)
    (tmp_path / "nvidia" / "nvjitlink").mkdir(parents=True)
    dirs = nvidia_bin_dirs([str(tmp_path)])
    assert [d.parent.name for d in dirs] == ["cublas", "cudnn"]


@pytest.mark.parametrize("fmt", ["flac", "opus"])
def test_faster_whisper_decodes_our_files(tmp_path: Path, fmt: str):
    """The worker hands the finalized FLAC/Opus file straight to faster-whisper (PyAV)."""
    from faster_whisper import decode_audio

    from callrec.audio.capture import MIC_PART, REMOTE_PART
    from callrec.audio.finalize import finalize_recording
    from tests.conftest import tone, write_wav

    folder = tmp_path / "rec"
    write_wav(folder / "audio" / MIC_PART, tone(200, 2.0, 48000), 48000)
    write_wav(folder / "audio" / REMOTE_PART, tone(300, 2.0, 44100), 44100)
    fa = finalize_recording(folder, fmt, write_mix=False)
    audio = decode_audio(str(fa.remote), sampling_rate=16000)
    assert abs(len(audio) - 32000) < 800


def test_console_python(tmp_path: Path):
    from callrec.asr.transcriber import console_python

    (tmp_path / "pythonw.exe").write_text("")
    assert console_python(str(tmp_path / "pythonw.exe")) == str(tmp_path / "pythonw.exe")  # no python.exe next to it
    (tmp_path / "python.exe").write_text("")
    assert console_python(str(tmp_path / "pythonw.exe")) == str(tmp_path / "python.exe")
    assert console_python("/usr/bin/python3") == "/usr/bin/python3"
