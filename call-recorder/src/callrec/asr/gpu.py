"""Is an NVIDIA GPU actually usable for faster-whisper? Checked once, cached, re-checkable.

On machines without an NVIDIA driver (e.g. only AMD/Intel graphics) the check returns at once
without starting anything: ``nvcuda.dll`` (the CUDA driver, installed only with the NVIDIA
driver) is missing. Otherwise a short child process asks CTranslate2 for the number of CUDA
devices - in a child process, so a broken driver/DLL setup cannot crash the app.
"""

from __future__ import annotations

import ctypes.util
import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from callrec import paths
from callrec.config import TranscriptionConfig

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000
AUTO_MODEL_GPU = "large-v3"
AUTO_MODEL_CPU = "large-v3-turbo"
PROBE_CODE = (
    "from callrec.asr.cuda import setup_cuda_dll_paths; setup_cuda_dll_paths(); "
    "import ctranslate2; print('CUDA_DEVICES', ctranslate2.get_cuda_device_count())"
)


@dataclass
class GpuStatus:
    usable: bool
    devices: int = 0
    reason: str = ""
    checked_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> GpuStatus:
        return GpuStatus(bool(d.get("usable")), int(d.get("devices", 0) or 0), str(d.get("reason", "")),
                         str(d.get("checked_at", "")))

    def describe(self) -> str:
        if self.usable:
            return f"NVIDIA-GPU nutzbar ({self.devices} Gerät(e))"
        return f"keine nutzbare NVIDIA-GPU ({self.reason})" if self.reason else "keine nutzbare NVIDIA-GPU"


def cache_file() -> Path:
    return paths.data_dir() / "gpu_status.json"


def nvidia_driver_present() -> bool:
    if sys.platform == "win32":
        root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        return Path(root, "System32", "nvcuda.dll").exists()
    return ctypes.util.find_library("cuda") is not None


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def probe_gpu(python: str | None = None, runner: Callable[..., Any] = subprocess.run,
              driver_present: Callable[[], bool] = nvidia_driver_present, timeout_s: float = 60.0) -> GpuStatus:
    if not driver_present():
        return GpuStatus(False, 0, "kein NVIDIA-Treiber (nvcuda.dll) gefunden", _now())
    from callrec.asr.transcriber import console_python

    cmd = [python or console_python(), "-c", PROBE_CODE]
    try:
        res = runner(cmd, capture_output=True, text=True, timeout=timeout_s,
                     creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except subprocess.TimeoutExpired:
        return GpuStatus(False, 0, "GPU-Prüfung reagiert nicht", _now())
    except OSError as exc:
        return GpuStatus(False, 0, f"GPU-Prüfung nicht startbar: {exc}", _now())
    out = str(getattr(res, "stdout", "") or "")
    for line in out.splitlines():
        if line.startswith("CUDA_DEVICES"):
            try:
                n = int(line.split()[1])
            except (IndexError, ValueError):
                break
            if n > 0:
                return GpuStatus(True, n, "", _now())
            return GpuStatus(False, 0, "CUDA meldet kein Gerät", _now())
    err = str(getattr(res, "stderr", "") or "").strip().splitlines()
    return GpuStatus(False, 0, f"CUDA nicht nutzbar (Exit-Code {res.returncode}: {err[-1][:160] if err else '-'})", _now())


def load_cached(path: Path | None = None) -> GpuStatus | None:
    try:
        return GpuStatus.from_dict(json.loads((path or cache_file()).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def save_cached(status: GpuStatus, path: Path | None = None) -> None:
    path = path or cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status.to_dict(), ensure_ascii=False), encoding="utf-8")


def gpu_status(force: bool = False, probe: Callable[[], GpuStatus] = probe_gpu, path: Path | None = None) -> GpuStatus:
    """Cached result; ``force`` re-checks (settings dialog: "GPU erneut prüfen")."""
    if not force:
        cached = load_cached(path)
        if cached is not None:
            return cached
    status = probe()
    log.info("GPU-Prüfung: %s", status.describe())
    try:
        save_cached(status, path)
    except OSError:
        log.warning("GPU-Status konnte nicht gespeichert werden")
    return status


def mark_cuda_failed(reason: str, path: Path | None = None) -> None:
    """Remember that CUDA failed in practice, so later runs go straight to the CPU."""
    try:
        save_cached(GpuStatus(False, 0, reason, _now()), path)
    except OSError:
        pass


@dataclass
class AsrPlan:
    devices: list[str]  # in order of attempts
    model_by_device: dict[str, str]
    note: str = ""

    def model_for(self, device: str) -> str:
        return self.model_by_device.get(device, self.model_by_device.get("cpu", AUTO_MODEL_CPU))


def plan_asr(cfg: TranscriptionConfig, gpu_usable: bool | None) -> AsrPlan:
    """Devices to try and model per device.

    ``gpu_usable=None`` means "not checked" (behaves like before: try CUDA, fall back to CPU).
    ``device="cuda"`` is an explicit wish and is always tried first.
    """
    if cfg.device == "cpu" or cfg.device == "auto" and gpu_usable is False:
        devices = ["cpu"]
    else:
        devices = ["cuda", "cpu"]
    auto = cfg.model.strip().lower() in ("", "auto")
    models = {d: (AUTO_MODEL_GPU if d == "cuda" else AUTO_MODEL_CPU) if auto else cfg.model for d in devices}
    note = ""
    if devices == ["cpu"]:
        why = "keine nutzbare NVIDIA-GPU" if cfg.device == "auto" else "CPU eingestellt"
        if auto:
            note = f"{why}: Modell {AUTO_MODEL_CPU} (int8) auf der CPU automatisch gewählt"
        elif cfg.model in ("large-v3", "large-v2", "large-v1", "large"):
            note = f"{why}: {cfg.model} ist auf der CPU sehr langsam - Modell 'auto' oder '{AUTO_MODEL_CPU}' empfohlen"
    return AsrPlan(devices, models, note)


def describe_effective(cfg: TranscriptionConfig, status: GpuStatus | None) -> str:
    plan = plan_asr(cfg, status.usable if status is not None else None)
    first = plan.devices[0]
    ctype = cfg.compute_type if cfg.compute_type != "auto" else ("float16" if first == "cuda" else "int8")
    gpu = status.describe() if status is not None else "GPU noch nicht geprüft"
    text = f"Effektiv: {first.upper()}, Modell {plan.model_for(first)} ({ctype}) – {gpu}"
    if len(plan.devices) > 1:
        text += f"; bei Problemen CPU mit {plan.model_for('cpu')}"
    return text
