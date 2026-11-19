"""Parent side of the Whisper worker: builds the command, streams progress, falls back to CPU."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from callrec.config import TranscriptionConfig
from callrec.models import AsrResult

log = logging.getLogger(__name__)

ProgressFn = Callable[[float], None]
CREATE_NO_WINDOW = 0x08000000


class TranscriptionError(RuntimeError):
    pass


class Transcriber(Protocol):
    def transcribe(self, audio: Path, out_json: Path, *, initial_prompt: str, progress: ProgressFn) -> AsrResult: ...


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


_GPU_ONLY_TYPES ={"float16", "int8_float16", "bfloat16", "int8_bfloat16"}


def compute_type_for(device: str, configured: str) -> str:
    """GPU-only precisions are replaced by 'auto' (= int8) for the CPU fallback."""
    if device == "cpu" and configured in _GPU_ONLY_TYPES:
        return "auto"
    return configured or "auto"


class WhisperSubprocessTranscriber:
    _cuda_failed = False  # remembered for the lifetime of the app

    def __init__(
        self,
        cfg: TranscriptionConfig,
        download_root: Path,
        command_prefix: Sequence[str] | None = None,
    ) -> None:
        self.cfg = cfg
        self.download_root = download_root
        self.command_prefix = list(command_prefix) if command_prefix else [console_python(), "-m", "callrec.asr.worker"]

    def devices_to_try(self) -> list[str]:
        if self.cfg.device == "cpu":
            return ["cpu"]
        if self.cfg.device == "auto" and WhisperSubprocessTranscriber._cuda_failed:
            return ["cpu"]
        # "cuda" and "auto" both fall back to CPU rather than produce nothing
        return ["cuda", "cpu"]

    def build_command(self, audio: Path, out_json: Path, device: str, initial_prompt: str) -> list[str]:
        cmd = self.command_prefix + [
            "--audio", str(audio),
            "--out", str(out_json),
            "--model", self.cfg.model,
            "--device", device,
            "--compute-type", compute_type_for(device, self.cfg.compute_type),
            "--language", self.cfg.language,
            "--beam-size", str(self.cfg.beam_size),
            "--vad" if self.cfg.vad_filter else "--no-vad",
            "--condition-on-previous-text" if self.cfg.condition_on_previous_text else "--no-condition-on-previous-text",
            "--download-root", str(self.download_root),
        ]
        if initial_prompt:
            cmd += ["--initial-prompt", initial_prompt]
        return cmd

    def _run(self, cmd: list[str], progress: ProgressFn, log_path: Path) -> int:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as errlog:
            errlog.write("\n$ " + " ".join(cmd) + "\n")
            errlog.flush()
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                cmd,
                stdout=subprocess.PIPE,
                stderr=errlog,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("PROGRESS "):
                    try:
                        progress(float(line.split()[1]))
                    except ValueError:
                        pass
            return proc.wait()

    def transcribe(self, audio: Path, out_json: Path, *, initial_prompt: str = "", progress: ProgressFn = lambda f: None) -> AsrResult:
        errors: list[str] = []
        log_path = out_json.with_suffix(".log")
        for device in self.devices_to_try():
            if out_json.exists():
                out_json.unlink()
            cmd = self.build_command(audio, out_json, device, initial_prompt)
            log.info("Starte Transkription (%s) von %s", device, audio.name)
            rc = self._run(cmd, progress, log_path)
            if rc == 0 and out_json.exists():
                data = json.loads(out_json.read_text(encoding="utf-8"))
                return AsrResult.from_dict(data)
            msg = f"Transkription auf {device.upper()} fehlgeschlagen (Exit-Code {rc}), Details: {log_path}"
            log.warning(msg)
            errors.append(msg)
            if device == "cuda":
                WhisperSubprocessTranscriber._cuda_failed = True
        raise TranscriptionError("; ".join(errors))
