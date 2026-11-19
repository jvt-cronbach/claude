from __future__ import annotations

import codecs
import logging
import logging.handlers
import sys
from pathlib import Path

from callrec import paths

_CONFIGURED = False
# UTF-8 *with* BOM: Windows PowerShell 5.1 (Get-Content without -Encoding) otherwise reads the log
# as ANSI and shows mojibake for umlauts. Python writes the BOM only at the start of a new/empty
# file (TextIOWrapper skips it when appending), and readers using "utf-8-sig" or PowerShell skip it.
LOG_ENCODING = "utf-8-sig"


class RedactingFilter(logging.Filter):
    """Never let an API key reach the log (e.g. inside an exception message)."""

    def filter(self, record: logging.LogRecord) -> bool:
        from callrec.apikey import KEY_PATTERN, redact

        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - malformed record: leave it to the formatter
            return True
        if KEY_PATTERN.search(msg):
            record.msg, record.args = redact(msg), None
        return True


def has_bom(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(3) == codecs.BOM_UTF8
    except OSError:
        return False


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
    fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding=LOG_ENCODING)
    if log_file.exists() and log_file.stat().st_size > 0 and not has_bom(log_file):
        fh.doRollover()  # an older log without BOM becomes callrec.log.1; the new file starts with a BOM
    fh.setFormatter(fmt)
    redacting = RedactingFilter()
    fh.addFilter(redacting)
    root.addHandler(fh)
    # the SDK/HTTP libraries log request details at DEBUG level - keep them out of the file
    for noisy in ("anthropic", "httpx2", "httpcore2", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(level, logging.INFO))
    # pythonw.exe has no stderr
    if console and sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.setLevel(level)
        sh.addFilter(redacting)
        root.addHandler(sh)
    _CONFIGURED = True
    return log_file
