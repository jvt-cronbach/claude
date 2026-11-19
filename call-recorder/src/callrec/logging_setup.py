from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from callrec import paths

_CONFIGURED = False


def setup_logging(level: int = logging.INFO, log_dir: Path | None = None, console: bool = True) -> Path:
    """Log to a rotating file in %LOCALAPPDATA%\\CallRecorder\\logs (and stderr if available)."""
    global _CONFIGURED
    log_dir = log_dir or paths.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "callrec.log"
    if _CONFIGURED:
        return log_file
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    # pythonw.exe has no stderr
    if console and sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.setLevel(level)
        root.addHandler(sh)
    _CONFIGURED = True
    return log_file
