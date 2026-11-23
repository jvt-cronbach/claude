"""Parent side of the Whisper worker: builds the command, streams progress, falls back to CPU."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from callrec.asr.gpu import AsrPlan, plan_asr
from callrec.config import TranscriptionConfig
from callrec.models import AsrResult
from callrec.speakers.threads import AsrThreads, busy_reason, pc_busy

log = logging.getLogger(__name__)

ProgressFn = Callable[..., None]  # progress(fraction) or progress(fraction, message); fraction < 0 = indeterminate
CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
CONTROL_POLL_S = 2.0


class TranscriptionError(RuntimeError):
    pass


@dataclass
class SessionResult:
    asr: dict[str, AsrResult]
    decision: dict[str, Any]  # language decision (callrec.asr.language)
    stats: dict[str, Any]  # timing, threads, per-track figures


class Transcriber(Protocol):
    def transcribe(
        self, audio: Path, out_json: Path, *, initial_prompt: str, hotwords: str, progress: ProgressFn
    ) -> AsrResult: ...


def console_python(executable: str | None = None) -> str:
    """The tray app runs under pythonw.exe; the worker uses python.exe (reliable stdout pipe).

    CREATE_NO_WINDOW keeps the console window hidden.
    """
    exe = Path(executable or sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


_GPU_ONLY_TYPES = {"float16", "int8_float16", "bfloat16", "int8_bfloat16"}


def compute_type_for(device: str, configured: str) -> str:
    """GPU-only precisions are replaced by 'auto' (= int8) for the CPU fallback."""
    if device == "cpu" and configured in _GPU_ONLY_TYPES:
        return "auto"
    return configured or "auto"


class TranscriptionCancelled(TranscriptionError):
    pass


_ACTIVE: set[subprocess.Popen[str]] = set()
_ACTIVE_LOCK = threading.Lock()
_CANCEL = threading.Event()


def reset_cancel() -> None:
    """A new processing run starts: an earlier cancellation no longer applies."""
    _CANCEL.clear()


def cancel_running_workers() -> int:
    """Stop running Whisper workers (tray "Verarbeitung abbrechen", app exit, Ctrl+C)."""
    _CANCEL.set()
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE)
    for proc in procs:
        try:
            proc.kill()
        except OSError:
            pass
    return len(procs)


class WorkerOutput:
    """Parses the worker's stdout protocol (see :mod:`callrec.asr.worker`)."""

    def __init__(self, progress: Callable[..., None]) -> None:
        from callrec.asr.download import DownloadDisplay

        self.progress = progress
        self.timing: dict[str, float] = {}
        self.stage = ""  # last STAGE reported by the worker ("download" = failed while downloading)
        self.download = DownloadDisplay()

    def _report(self, frac: float, msg: str = "") -> None:
        try:
            self.progress(frac, msg) if msg else self.progress(frac)
        except TypeError:  # progress(fraction) only
            if frac >= 0:
                self.progress(frac)

    def feed(self, line: str) -> None:
        parts = line.strip().split()
        if not parts:
            return
        kind = parts[0]
        try:
            if kind == "PROGRESS":
                self._report(float(parts[1]))
            elif kind == "STAGE" and len(parts) > 1:
                self.stage = parts[1]
                if parts[1] == "load":
                    self._report(-1.0, "Modell wird geladen")
                elif parts[1] == "download":
                    self._report(-1.0, "Modell wird heruntergeladen")
                elif parts[1] == "transcribe":
                    self._report(0.0, "")
                elif parts[1] == "detect":
                    self._report(-1.0, "Sprache wird erkannt")
                elif parts[1] == "prepare":
                    self._report(-1.0, "Audio wird vorbereitet")
            elif kind in ("DOWNLOAD", "DOWNLOADDISK", "DOWNLOADED"):
                text = self.download.feed(parts)
                if text:
                    self._report(-1.0, text)
            elif kind == "TIMING" and len(parts) >= 4:
                self.timing = {"download_s": float(parts[1]), "load_s": float(parts[2]), "transcribe_s": float(parts[3])}
        except (ValueError, IndexError):
            pass


def run_child(cmd: list[str], log_path: Path, feed: Callable[[str], None],
              control: Callable[[Any], None] | None = None) -> int:
    """Start a worker process (below-normal priority, no console window), stream its stdout lines to
    ``feed`` and its stderr to ``log_path``; registered for ``cancel_running_workers``."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig: BOM only when the file is new, so Windows PowerShell 5.1 shows umlauts correctly
    with open(log_path, "a", encoding="utf-8-sig") as errlog:
        errlog.write("\n$ " + " ".join(cmd) + "\n")
        errlog.flush()
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            cmd,
            stdin=subprocess.PIPE if control is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=errlog,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            # below normal: a following call (Teams) and a new recording always come first
            creationflags=(CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS) if sys.platform == "win32" else 0,
        )
        with _ACTIVE_LOCK:
            _ACTIVE.add(proc)
        watcher = None
        if control is not None:
            watcher = threading.Thread(target=control, args=(proc,), name="worker-control", daemon=True)
            watcher.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                feed(line)
            return proc.wait()
        except KeyboardInterrupt:
            proc.kill()
            proc.wait()
            raise
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE.discard(proc)
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            if watcher is not None:
                watcher.join(timeout=5)


class WhisperSubprocessTranscriber:
    _cuda_failed = False  # remembered for the lifetime of the app

    def __init__(
        self,
        cfg: TranscriptionConfig,
        download_root: Path,
        command_prefix: Sequence[str] | None = None,
        gpu_usable: Callable[[], bool | None] | None = None,
        on_cuda_failed: Callable[[str], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.download_root = download_root
        self.command_prefix = list(command_prefix) if command_prefix else [console_python(), "-m", "callrec.asr.worker"]
        self._gpu_usable = gpu_usable or (lambda: None)
        self._on_cuda_failed = on_cuda_failed or (lambda reason: None)
        self.last_stats: dict[str, Any] = {}
        self._plan: AsrPlan | None = None

    def plan(self) -> AsrPlan:
        if self._plan is None:
            usable = False if (self.cfg.device == "auto" and WhisperSubprocessTranscriber._cuda_failed) else self._gpu_usable()
            self._plan = plan_asr(self.cfg, usable)
        return self._plan

    @property
    def selection_note(self) -> str:
        return self.plan().note

    def devices_to_try(self) -> list[str]:
        if self.cfg.device == "auto" and WhisperSubprocessTranscriber._cuda_failed:
            return ["cpu"]
        return list(self.plan().devices)

    def _common_args(self, device: str) -> list[str]:
        c = self.cfg
        return [
            "--model", self.plan().model_for(device),
            "--device", device,
            "--compute-type", compute_type_for(device, c.compute_type),
            "--download-root", str(self.download_root),
            "--vad-threshold", str(c.vad_threshold),
            "--vad-min-silence-ms", str(c.vad_min_silence_ms),
            "--vad-speech-pad-ms", str(c.vad_speech_pad_ms),
        ]

    def build_command(self, audio: Path, out_json: Path, device: str, initial_prompt: str, hotwords: str = "",
                      language: str | None = None) -> list[str]:
        c = self.cfg
        cmd = self.command_prefix + [
            "--audio", str(audio),
            "--out", str(out_json),
            *self._common_args(device),
            "--language", language or c.language,
            "--beam-size", str(c.beam_size),
            "--vad" if c.vad_filter else "--no-vad",
            "--condition-on-previous-text" if c.condition_on_previous_text else "--no-condition-on-previous-text",
            "--hallucination-silence-threshold", str(c.hallucination_silence_threshold),
            "--compression-ratio-threshold", str(c.compression_ratio_threshold),
            "--log-prob-threshold", str(c.log_prob_threshold),
            "--no-speech-threshold", str(c.no_speech_threshold),
            "--temperature-fallback" if c.temperature_fallback else "--no-temperature-fallback",
            "--repetition-penalty", str(c.repetition_penalty),
            "--no-repeat-ngram-size", str(c.no_repeat_ngram_size),
            "--multilingual" if c.multilingual else "--no-multilingual",
        ]
        if initial_prompt:
            cmd += ["--initial-prompt", initial_prompt]
        if hotwords:
            cmd += ["--hotwords", hotwords]
        return cmd

    def _run(self, cmd: list[str], progress: ProgressFn, log_path: Path, output: WorkerOutput | None = None,
             control: Callable[[Any], None] | None = None) -> int:
        """Run the worker; ``control(proc)`` (if given) runs on its own thread while it works and may
        write commands to its stdin (``LANES n``)."""
        output = output or WorkerOutput(progress)
        return run_child(cmd, log_path, output.feed, control)

    def detect_languages(self, tracks: dict[str, Path], out_json: Path,
                         progress: ProgressFn = lambda f: None) -> dict[str, Any]:
        """Language probabilities of speech windows per track (worker ``--detect-language``)."""
        errors: list[str] = []
        log_path = out_json.with_suffix(".log")
        _CANCEL.clear()
        for device in self.devices_to_try():
            if out_json.exists():
                out_json.unlink()
            cmd = self.command_prefix + ["--detect-language", "--out", str(out_json), *self._common_args(device),
                                         "--detect-windows", str(self.cfg.language_detection_windows)]
            for track, path in tracks.items():
                cmd += ["--detect-audio", f"{track}={path}"]
            output = WorkerOutput(progress)
            rc = self._run(cmd, progress, log_path, output)
            if _CANCEL.is_set():
                raise TranscriptionCancelled("Transkription abgebrochen.")
            if rc == 0 and out_json.exists():
                return dict(json.loads(out_json.read_text(encoding="utf-8")))
            msg = f"Spracherkennung auf {device.upper()} fehlgeschlagen (Exit-Code {rc}), Details: {log_path}"
            log.warning(msg)
            errors.append(msg)
            if device == "cuda" and output.stage != "download":
                WhisperSubprocessTranscriber._cuda_failed = True
                self._on_cuda_failed(f"Spracherkennung auf CUDA fehlgeschlagen (Exit-Code {rc})")
        raise TranscriptionError("; ".join(errors))

    def transcribe(
        self, audio: Path, out_json: Path, *, initial_prompt: str = "", hotwords: str = "",
        progress: ProgressFn = lambda f: None, language: str | None = None,
    ) -> AsrResult:
        errors: list[str] = []
        log_path = out_json.with_suffix(".log")
        _CANCEL.clear()
        self.last_stats = {}
        for device in self.devices_to_try():
            if out_json.exists():
                out_json.unlink()
            cmd = self.build_command(audio, out_json, device, initial_prompt, hotwords, language)
            log.info("Starte Transkription (%s, Modell %s) von %s", device, self.plan().model_for(device), audio.name)
            output = WorkerOutput(progress)
            started = time.monotonic()
            rc = self._run(cmd, progress, log_path, output)
            if _CANCEL.is_set():
                raise TranscriptionCancelled("Transkription abgebrochen.")
            if rc == 0 and out_json.exists():
                data = json.loads(out_json.read_text(encoding="utf-8"))
                result = AsrResult.from_dict(data)
                self.last_stats = self._stats(output.timing or dict(data.get("timing") or {}), result.duration,
                                              time.monotonic() - started)
                return result
            msg = f"Transkription auf {device.upper()} fehlgeschlagen (Exit-Code {rc}), Details: {log_path}"
            log.warning(msg)
            errors.append(msg)
            if device == "cuda" and output.stage != "download":
                # a failed model download (offline, Hub unreachable) says nothing about the GPU
                WhisperSubprocessTranscriber._cuda_failed = True
                self._on_cuda_failed(f"Transkription auf CUDA fehlgeschlagen (Exit-Code {rc})")
        raise TranscriptionError("; ".join(errors))

    # -- session (0.4): one worker for the whole recording ------------------------------------------
    def thread_plan(self, device: str, reserve: int = 0) -> AsrThreads:
        from callrec.asr.benchmark import load_tuned
        from callrec.speakers.threads import asr_threads, physical_cores

        if device != "cpu":
            return AsrThreads(1, 0, physical_cores(), pc_busy(), "GPU")
        return asr_threads(self.cfg.cpu_threads, self.cfg.parallel_lanes, reserve=reserve, tuned=load_tuned())

    def session_job(self, device: str, tracks: dict[str, Path], outs: dict[str, Path], language: dict[str, Any],
                    initial_prompt: str, hotwords: str, profile: str | None, plan: AsrThreads,
                    active_lanes: int) -> dict[str, Any]:
        from callrec.asr.profiles import decoding_for

        c = self.cfg
        return {
            "model": self.plan().model_for(device), "device": device,
            "compute_type": compute_type_for(device, c.compute_type), "download_root": str(self.download_root),
            "lanes": plan.lanes, "threads": plan.threads, "active_lanes": active_lanes,
            "tracks": {t: {"audio": str(p), "out": str(outs[t])} for t, p in tracks.items()},
            "language": language, "decoding": decoding_for(c, profile).to_dict(), "vad_filter": c.vad_filter,
            "options": {
                "condition_on_previous_text": c.condition_on_previous_text, "initial_prompt": initial_prompt,
                "hotwords": hotwords, "hallucination_silence_threshold": c.hallucination_silence_threshold,
                "compression_ratio_threshold": c.compression_ratio_threshold, "log_prob_threshold": c.log_prob_threshold,
                "no_speech_threshold": c.no_speech_threshold, "repetition_penalty": c.repetition_penalty,
                "no_repeat_ngram_size": c.no_repeat_ngram_size, "multilingual": c.multilingual,
            },
        }

    def transcribe_session(
        self, tracks: dict[str, Path], outs: dict[str, Path], *, language: dict[str, Any], work_dir: Path,
        initial_prompt: str = "", hotwords: str = "", progress: ProgressFn = lambda f: None,
        profile: str | None = None, reserve_threads: int = 0, during_call: str = "reduce",
        busy: Callable[[], bool] | None = None,
    ) -> SessionResult:
        """Language detection and both tracks in ONE worker process (model loaded once).

        ``language``: {"mode": "forced", "decision": ...} or {"mode": "auto", "candidates", ...}.
        While ``busy()`` (recording/call) only ``plan.busy_lanes`` lanes work, or none with
        ``during_call="pause"``."""
        busy = busy or pc_busy
        errors: list[str] = []
        work_dir.mkdir(parents=True, exist_ok=True)
        log_path = work_dir / "transcription.log"
        job_path, res_path = work_dir / "session_job.json", work_dir / "session_result.json"
        _CANCEL.clear()
        self.last_stats = {}
        for device in self.devices_to_try():
            for p in (res_path, *outs.values()):
                if p.exists():
                    p.unlink()
            plan = self.thread_plan(device, reserve_threads)

            def lanes_now(plan: AsrThreads = plan) -> int:
                if not busy():
                    return plan.lanes
                return 0 if during_call == "pause" else plan.busy_lanes

            job = self.session_job(device, tracks, outs, language, initial_prompt, hotwords, profile, plan, lanes_now())
            job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            cmd = self.command_prefix + ["--session", str(job_path), "--out", str(res_path)]
            log.info("Starte Transkription (%s, Modell %s, %s, Profil %s)", device, job["model"], plan.describe(),
                     job["decoding"]["profile"])
            output = WorkerOutput(progress)
            started = time.monotonic()

            def control(proc: Any, first: int = int(job["active_lanes"]), lanes_now: Any = lanes_now,
                        output: WorkerOutput = output) -> None:
                sent = first
                while proc.poll() is None:
                    time.sleep(CONTROL_POLL_S)
                    want = lanes_now()
                    if want != sent and proc.stdin is not None:
                        try:
                            proc.stdin.write(f"LANES {want}\n")
                            proc.stdin.flush()
                        except (OSError, ValueError):
                            return
                        log.info("Transkription: %s", "pausiert (Gespräch läuft)" if want == 0 else f"{want} Spur(en) aktiv")
                        if want == 0:
                            output._report(-1.0, f"pausiert – {busy_reason() or 'Gespräch läuft'}")
                        sent = want

            rc = self._run(cmd, progress, log_path, output, control=control)
            if _CANCEL.is_set():
                raise TranscriptionCancelled("Transkription abgebrochen.")
            if rc == 0 and res_path.exists() and all(p.exists() for p in outs.values()):
                res = json.loads(res_path.read_text(encoding="utf-8"))
                asr = {t: AsrResult.from_dict(json.loads(p.read_text(encoding="utf-8"))) for t, p in outs.items()}
                wall = time.monotonic() - started
                stats = {"timing": {**dict(res.get("timing") or {}), "wall_s": round(wall, 2)},
                         "threads": plan.to_dict(), "tracks": dict(res.get("tracks") or {}),
                         "decoding": dict(res.get("decoding") or {}), "pieces": res.get("pieces")}
                self.last_stats = stats
                return SessionResult(asr, dict(res.get("language_detection") or {}), stats)
            msg = f"Transkription auf {device.upper()} fehlgeschlagen (Exit-Code {rc}), Details: {log_path}"
            log.warning(msg)
            errors.append(msg)
            if device == "cuda" and output.stage != "download":
                WhisperSubprocessTranscriber._cuda_failed = True
                self._on_cuda_failed(f"Transkription auf CUDA fehlgeschlagen (Exit-Code {rc})")
        raise TranscriptionError("; ".join(errors))

    @staticmethod
    def _stats(timing: dict[str, float], audio_s: float, wall_s: float) -> dict[str, Any]:
        transcribe_s = float(timing.get("transcribe_s") or 0.0) or max(
            0.0, wall_s - float(timing.get("load_s") or 0.0) - float(timing.get("download_s") or 0.0))
        stats: dict[str, Any] = {
            "audio_s": round(audio_s, 2),
            "transcribe_s": round(transcribe_s, 2),
            "load_s": round(float(timing.get("load_s") or 0.0), 2),
            "download_s": round(float(timing.get("download_s") or 0.0), 2),
            "wall_s": round(wall_s, 2),
            # real-time factor: processing time / audio time (< 1 = faster than real time)
            "rtf": round(transcribe_s / audio_s, 3) if audio_s > 0 else None,
            "rtf_total": round(wall_s / audio_s, 3) if audio_s > 0 else None,
        }
        return stats
