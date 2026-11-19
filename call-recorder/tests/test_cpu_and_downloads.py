"""CPU-only machines (GPU detection, model choice, real-time factor), model download progress, log encoding."""

from __future__ import annotations

import codecs
import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from callrec.asr import gpu
from callrec.asr.download import (
    ProgressAggregator,
    describe_progress,
    download_whisper_model,
    make_tqdm_class,
)
from callrec.asr.transcriber import WhisperSubprocessTranscriber, WorkerOutput
from callrec.config import TranscriptionConfig


@pytest.fixture(autouse=True)
def reset_cuda_flag():
    WhisperSubprocessTranscriber._cuda_failed = False
    yield
    WhisperSubprocessTranscriber._cuda_failed = False


# --- GPU detection ------------------------------------------------------------------------
def test_no_driver_means_no_probe_process():
    def runner(*a, **k):
        raise AssertionError("no child process without NVIDIA driver")

    st = gpu.probe_gpu(runner=runner, driver_present=lambda: False)
    assert st.usable is False and "nvcuda.dll" in st.reason


@pytest.mark.parametrize("stdout, rc, usable, reason", [
    ("CUDA_DEVICES 1\n", 0, True, ""),
    ("CUDA_DEVICES 0\n", 0, False, "kein Gerät"),
    ("", 1, False, "Exit-Code 1"),
])
def test_probe_parses_child_output(stdout, rc, usable, reason):
    def runner(cmd, **kw):
        assert cmd[1] == "-c" and "get_cuda_device_count" in cmd[2] and kw["timeout"] > 0
        return SimpleNamespace(stdout=stdout, stderr="RuntimeError: CUDA driver version is insufficient\n", returncode=rc)

    st = gpu.probe_gpu(python="py", runner=runner, driver_present=lambda: True)
    assert st.usable is usable and reason in st.reason


def test_probe_timeout_does_not_crash():
    def runner(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    assert gpu.probe_gpu(python="py", runner=runner, driver_present=lambda: True).usable is False


def test_probe_really_runs_in_a_child_process(monkeypatch):
    """Without CUDA in this test environment the real child process reports 'not usable' cleanly."""
    st = gpu.probe_gpu(python=sys.executable, driver_present=lambda: True)
    assert st.usable is False and st.checked_at


def test_status_is_cached_and_can_be_rechecked(tmp_path: Path):
    calls = []

    def probe():
        calls.append(1)
        return gpu.GpuStatus(False, 0, "kein NVIDIA-Treiber", "t")

    path = tmp_path / "gpu.json"
    assert gpu.gpu_status(probe=probe, path=path).usable is False
    assert gpu.gpu_status(probe=probe, path=path).reason == "kein NVIDIA-Treiber" and len(calls) == 1
    gpu.gpu_status(force=True, probe=probe, path=path)
    assert len(calls) == 2
    gpu.save_cached(gpu.GpuStatus(True, 1, "", "t"), path)
    gpu.mark_cuda_failed("Transkription auf CUDA fehlgeschlagen (Exit-Code 1)", path)
    assert gpu.load_cached(path).usable is False  # type: ignore[union-attr]


def test_plan_without_gpu_uses_cpu_and_turbo():
    plan = gpu.plan_asr(TranscriptionConfig(), gpu_usable=False)
    assert plan.devices == ["cpu"] and plan.model_for("cpu") == "large-v3-turbo"
    assert "large-v3-turbo (int8)" in plan.note and "keine nutzbare NVIDIA-GPU" in plan.note
    with_gpu = gpu.plan_asr(TranscriptionConfig(), gpu_usable=True)
    assert with_gpu.devices == ["cuda", "cpu"] and with_gpu.model_for("cuda") == "large-v3"
    assert with_gpu.model_for("cpu") == "large-v3-turbo" and with_gpu.note == ""
    # an explicit model choice is respected, but a slow choice on the CPU is pointed out
    explicit = gpu.plan_asr(TranscriptionConfig(model="large-v3"), gpu_usable=False)
    assert explicit.model_for("cpu") == "large-v3" and "sehr langsam" in explicit.note
    assert gpu.plan_asr(TranscriptionConfig(model="medium"), False).note == ""
    # device = "cuda" is an explicit wish: still tried first
    assert gpu.plan_asr(TranscriptionConfig(device="cuda"), False).devices == ["cuda", "cpu"]
    assert gpu.plan_asr(TranscriptionConfig(device="cpu"), True).devices == ["cpu"]
    text = gpu.describe_effective(TranscriptionConfig(), gpu.GpuStatus(False, 0, "kein NVIDIA-Treiber"))
    assert text.startswith("Effektiv: CPU, Modell large-v3-turbo (int8)")


FAKE_WORKER = r'''
import argparse, json, sys
p = argparse.ArgumentParser(allow_abbrev=False)
for a in ("--audio", "--out", "--model", "--device", "--compute-type", "--language", "--beam-size", "--download-root",
          "--initial-prompt", "--vad-threshold", "--vad-min-silence-ms", "--vad-speech-pad-ms", "--hallucination-silence-threshold"):
    p.add_argument(a)
a, _ = p.parse_known_args()
if a.device == "cuda":
    sys.exit(1)
print("STAGE download", flush=True)
print("DOWNLOAD 0 1600000000 1000000", flush=True)
print("DOWNLOAD 1600000000 1600000000 1500000000", flush=True)
print("STAGE load", flush=True)
print("STAGE transcribe 4.00", flush=True)
print("PROGRESS 0.5", flush=True)
json.dump({"language": "de", "duration": 19.0, "model": a.model, "device": a.device, "compute_type": "int8",
           "segments": []}, open(a.out, "w"))
print("PROGRESS 1.0", flush=True)
print("TIMING 30.00 4.00 9.50", flush=True)
'''


def test_cpu_only_machine_never_tries_cuda(tmp_path: Path):
    script = tmp_path / "w.py"
    script.write_text(FAKE_WORKER, encoding="utf-8")
    failed = []
    t = WhisperSubprocessTranscriber(TranscriptionConfig(), tmp_path / "m", [sys.executable, str(script)],
                                     gpu_usable=lambda: False, on_cuda_failed=failed.append)
    seen: list[tuple[float, str]] = []
    res = t.transcribe(tmp_path / "mic.flac", tmp_path / "asr_mic.json", progress=lambda f, m="": seen.append((f, m)))
    assert res.device == "cpu" and res.model == "large-v3-turbo" and failed == []
    log = (tmp_path / "asr_mic.log").read_text(encoding="utf-8-sig")
    assert "--device cuda" not in log and log.count("$ ") == 1
    assert t.selection_note.startswith("keine nutzbare NVIDIA-GPU")
    msgs = [m for f, m in seen if f < 0]
    assert msgs[0] == "Modell wird heruntergeladen"
    assert "Modell wird heruntergeladen: 1,60 GB von 1,60 GB (100 %)" in msgs
    assert "Modell wird geladen" in msgs and (0.5, "") in seen
    assert t.last_stats["rtf"] == 0.5 and t.last_stats["load_s"] == 4.0 and t.last_stats["download_s"] == 30.0
    assert t.last_stats["audio_s"] == 19.0


def test_cuda_failure_is_remembered_for_later_runs(tmp_path: Path):
    script = tmp_path / "w.py"
    script.write_text(FAKE_WORKER, encoding="utf-8")
    failed: list[str] = []
    t = WhisperSubprocessTranscriber(TranscriptionConfig(), tmp_path / "m", [sys.executable, str(script)],
                                     gpu_usable=lambda: True, on_cuda_failed=failed.append)
    res = t.transcribe(tmp_path / "a.flac", tmp_path / "o.json")
    assert res.device == "cpu" and res.model == "large-v3-turbo" and "Exit-Code 1" in failed[0]
    assert t.devices_to_try() == ["cpu"]


def test_worker_output_with_old_style_progress_callback():
    got: list[float] = []
    out = WorkerOutput(got.append)
    for line in ("STAGE load", "DOWNLOAD 1 2 1", "PROGRESS 0.25", "garbage", "PROGRESS x", "TIMING 0 1 2"):
        out.feed(line)
    assert got == [0.25] and out.timing == {"download_s": 0.0, "load_s": 1.0, "transcribe_s": 2.0}


def test_pipeline_records_rtf_and_selection(tmp_path: Path):
    from callrec.config import Config
    from callrec.jobs import status_text
    from callrec.speakers.profiles import PlainFilePersistence, ProfileStore
    from tests.test_pipeline import make_pipeline, recording_with_voices

    pipeline, tr, _ = make_pipeline(ProfileStore(PlainFilePersistence(tmp_path / "vp.json")), cfg=Config())
    tr.selection_note = "keine nutzbare NVIDIA-GPU: Modell large-v3-turbo (int8) auf der CPU automatisch gewählt"
    tr.last_stats = {"audio_s": 6.0, "transcribe_s": 3.0, "load_s": 1.0, "download_s": 0.0, "rtf": 0.5}
    rec = recording_with_voices(tmp_path / "r", 330, 550)
    statuses: list[str] = []
    res = pipeline.run(rec, progress=lambda s, f, m: statuses.append(status_text(s, f, m)))
    t = rec.load_meta()["processing"]["transcription"]
    assert t["mic"]["timing"]["rtf"] == 0.5 and "large-v3-turbo" in t["selection_note"]
    assert tr.selection_note in res.messages
    md = rec.transcript_path.read_text(encoding="utf-8")
    assert "Echtzeitfaktor 0.50" in md and "automatisch gewählt" in md
    assert status_text("transcribe", -1.0, "Spur mic: Modell wird geladen", "X") == "Transkription: Spur mic: Modell wird geladen – X"
    assert status_text("summarize", 0.0, "Protokoll: warte auf anthropic – 12 s") == "Zusammenfassung 0 % (Protokoll: warte auf anthropic – 12 s)"


# --- downloads -----------------------------------------------------------------------------
def test_reporting_tqdm_with_huggingface_hub_helpers():
    """Our tqdm class driven by the same helpers huggingface_hub 2.4 uses in snapshot_download."""
    from huggingface_hub.utils._xet_progress_reporting import (
        XET_BYTES_BAR_FORMAT,
        XET_TRANSFER_BAR_FORMAT,
        _update_transfer_bar,
    )
    from huggingface_hub.utils.tqdm import _create_progress_bar

    reports: list[tuple[int, int, int]] = []
    agg = ProgressAggregator(lambda *r: reports.append(r), interval_s=0.0)
    cls = make_tqdm_class(agg)
    transfer = _create_progress_bar(cls=cls, log_level=logging.WARNING, name="huggingface_hub.snapshot_download.transfer",
                                    desc="Downloading bytes", total=0, initial=0, unit="B", unit_scale=True,
                                    bar_format=XET_TRANSFER_BAR_FORMAT)
    recon = _create_progress_bar(cls=cls, log_level=logging.WARNING, name="huggingface_hub.snapshot_download",
                                 desc="Reconstructing (incomplete total...)", total=0, initial=0, unit="B",
                                 unit_scale=True, bar_format=XET_BYTES_BAR_FORMAT)
    files = cls(range(5), desc="Fetching 5 files")
    recon.total = (recon.total or 0) + 3_000_000_000  # what _AggregatedTqdm does per file
    _update_transfer_bar(transfer, 500_000_000)
    recon.update(250_000_000)
    files.update(1)
    assert reports[-1] == (250_000_000, 3_000_000_000, 500_000_000)
    frac, text = describe_progress(*reports[-1])
    assert frac == pytest.approx(1 / 6) and text == "Modell wird heruntergeladen: 500 MB von 3,00 GB (17 %)"
    assert describe_progress(0, 0, 2_000_000)[0] is None


def test_download_uses_same_files_and_cache_as_faster_whisper(tmp_path: Path):
    from faster_whisper import utils as fw_utils

    seen = {}

    def fake_snapshot(repo, **kw):
        seen.update(kw, repo=repo)
        bar = kw["tqdm_class"](desc="Reconstructing", total=100, unit="B")
        bar.update(100)
        return str(tmp_path / "snap")

    reports: list[tuple[int, int, int]] = []
    out = download_whisper_model("large-v3-turbo", tmp_path / "whisper", lambda *r: reports.append(r), snapshot_download=fake_snapshot)
    assert out == str(tmp_path / "snap") and seen["repo"] == fw_utils._MODELS["large-v3-turbo"]
    assert seen["cache_dir"] == str(tmp_path / "whisper")  # faster-whisper passes download_root as cache_dir
    import inspect

    src = inspect.getsource(fw_utils.download_model)
    for pattern in seen["allow_patterns"]:
        assert f'"{pattern}"' in src  # identical file selection as faster_whisper.utils.download_model
    assert reports[-1][0] == 100


def test_models_command_defaults_to_download():
    from callrec.cli import build_parser

    args = build_parser().parse_args(["models", "--whisper"])
    assert args.action == "download" and args.whisper and args.func.__name__ == "cmd_models"


def test_cli_download_shows_progress_and_stops_on_ctrl_c(tmp_path: Path, capsys, monkeypatch):
    from callrec import cli

    script = tmp_path / "dl.py"
    script.write_text("import sys\nprint('STAGE download', flush=True)\nprint('DOWNLOAD 800000000 1600000000 900000000', flush=True)\n"
                      "print('STAGE done', flush=True)\n", encoding="utf-8")
    assert cli.run_whisper_download("large-v3-turbo", tmp_path, command_prefix=[sys.executable, str(script)]) == 0
    out = capsys.readouterr().out
    assert "Strg+C" in out and "900 MB von 1,60 GB (56 %)" in out

    slow = tmp_path / "slow.py"
    slow.write_text("import time\nprint('STAGE download', flush=True)\ntime.sleep(30)\n", encoding="utf-8")
    import subprocess as sp

    real_popen = sp.Popen

    class InterruptingPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.stdout = iter(_interrupt_after_first(self.stdout))  # type: ignore[assignment]

    def _interrupt_after_first(stream):
        yield next(stream)
        raise KeyboardInterrupt

    monkeypatch.setattr(sp, "Popen", InterruptingPopen)
    assert cli.run_whisper_download("x", tmp_path, command_prefix=[sys.executable, str(slow)]) == 130
    assert "Download abgebrochen" in capsys.readouterr().out


def test_cancel_running_worker(tmp_path: Path):
    import threading

    from callrec.asr import transcriber as tmod

    script = tmp_path / "hang.py"
    script.write_text("import time\nprint('STAGE download', flush=True)\ntime.sleep(60)\n", encoding="utf-8")
    t = WhisperSubprocessTranscriber(TranscriptionConfig(device="cpu"), tmp_path, [sys.executable, str(script)])
    err: list[BaseException] = []

    def run():
        try:
            t.transcribe(tmp_path / "a.flac", tmp_path / "o.json")
        except BaseException as exc:  # noqa: BLE001
            err.append(exc)

    th = threading.Thread(target=run)
    th.start()
    for _ in range(100):
        if tmod._ACTIVE:
            break
        threading.Event().wait(0.05)
    assert tmod.cancel_running_workers() == 1
    th.join(10)
    assert not th.is_alive() and isinstance(err[0], tmod.TranscriptionCancelled)


# --- log file encoding ------------------------------------------------------------------------
def test_log_file_has_bom_and_old_log_is_rotated(tmp_path: Path):
    from callrec import logging_setup

    old = tmp_path / "callrec.log"
    old.write_text("alter Eintrag ohne BOM: Größe\n", encoding="utf-8")
    root = logging.getLogger()
    before = list(root.handlers)
    logging_setup._CONFIGURED = False
    try:
        path = logging_setup.setup_logging(log_dir=tmp_path, console=False)
        logging.getLogger("callrec.test").warning("Prüfung äöü ß")
        for h in root.handlers:
            h.flush()
        raw = path.read_bytes()
        assert raw.startswith(codecs.BOM_UTF8) and raw.count(codecs.BOM_UTF8) == 1
        assert "Prüfung äöü ß" in raw.decode("utf-8-sig")
        assert (tmp_path / "callrec.log.1").read_text(encoding="utf-8").startswith("alter Eintrag")
        # appending in a later run does not add a second BOM
        for h in [h for h in root.handlers if h not in before]:
            h.close()
            root.removeHandler(h)
        logging_setup._CONFIGURED = False
        logging_setup.setup_logging(log_dir=tmp_path, console=False)
        logging.getLogger("callrec.test").warning("zweiter Lauf")
        for h in root.handlers:
            h.flush()
        raw = path.read_bytes()
        assert raw.count(codecs.BOM_UTF8) == 1 and "zweiter Lauf" in raw.decode("utf-8-sig")
    finally:
        for h in [h for h in root.handlers if h not in before]:
            h.close()
            root.removeHandler(h)
        logging_setup._CONFIGURED = False


# --- prompt templates (structured summary) ------------------------------------------------------
def test_prompt_templates_upgrade_only_unchanged_defaults(isolated_home: Path):
    from callrec import paths
    from callrec.userfiles import (
        _PREVIOUS_DEFAULTS,
        SUMMARY_FINAL_TEMPLATE,
        SUMMARY_SYSTEM_TEMPLATE,
        ensure_user_files,
        load_prompts,
        prompt_is_customised,
    )

    d = paths.prompts_dir()
    d.mkdir(parents=True)
    (d / "summary_final.txt").write_text(_PREVIOUS_DEFAULTS["summary_final.txt"][0].replace("\n", "\r\n"), encoding="utf-8")
    (d / "summary_system.txt").write_text("Meine eigene Anweisung", encoding="utf-8")
    ensure_user_files()
    assert (d / "summary_final.txt").read_text(encoding="utf-8") == SUMMARY_FINAL_TEMPLATE  # unchanged old default upgraded
    assert (d / "summary_system.txt").read_text(encoding="utf-8") == "Meine eigene Anweisung"  # customised: untouched
    assert (d / "summary_system.default.txt").read_text(encoding="utf-8") == SUMMARY_SYSTEM_TEMPLATE
    assert prompt_is_customised("summary_system.txt") and not prompt_is_customised("summary_final.txt")
    assert load_prompts().system == "Meine eigene Anweisung"
    for section in ("Projektstand und Updates", "Offene Fragen", "Nächste Termine", "offen"):
        assert section in SUMMARY_FINAL_TEMPLATE
    assert "Rate keine Namen und keine Daten" in SUMMARY_SYSTEM_TEMPLATE


def test_settings_snapshot_json_roundtrip():
    assert json.loads(json.dumps(gpu.GpuStatus(True, 2).to_dict()))["devices"] == 2
