"""Make the pip-installed NVIDIA DLLs (cuBLAS, cuDNN) findable on Windows.

ctranslate2's Windows wheel bundles only the cuDNN loader stub
(cudnn64_9.dll); cudnn_*64_9.dll and cublas64_12.dll / cublasLt64_12.dll come
from the ``nvidia-cudnn-cu12`` / ``nvidia-cublas-cu12`` wheels in
``site-packages/nvidia/<lib>/bin``. Those are loaded by plain LoadLibrary calls
inside the NVIDIA libraries, so they must be on PATH as well as registered via
``os.add_dll_directory``.
"""

from __future__ import annotations

import logging
import os
import site
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def nvidia_bin_dirs(site_dirs: list[str] | None = None) -> list[Path]:
    if site_dirs is None:
        site_dirs = list(site.getsitepackages())
        user = site.getusersitepackages()
        if isinstance(user, str):
            site_dirs.append(user)
    out: list[Path] = []
    for sd in site_dirs:
        root = Path(sd) / "nvidia"
        if not root.is_dir():
            continue
        for lib in sorted(root.iterdir()):
            b = lib / "bin"
            if b.is_dir():
                out.append(b)
    return out


def setup_cuda_dll_paths() -> list[Path]:
    dirs = nvidia_bin_dirs()
    if sys.platform != "win32":
        return dirs
    for d in dirs:
        try:
            os.add_dll_directory(str(d))
        except (OSError, AttributeError):
            log.warning("DLL-Verzeichnis konnte nicht registriert werden: %s", d)
    if dirs:
        os.environ["PATH"] = os.pathsep.join([str(d) for d in dirs] + [os.environ.get("PATH", "")])
    return dirs
