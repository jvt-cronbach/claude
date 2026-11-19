"""Whisper model download with visible progress.

faster-whisper's ``download_model`` passes a *disabled* tqdm class to
``huggingface_hub.snapshot_download``, so a multi-GB download shows nothing. We call
``snapshot_download`` ourselves beforehand with the same repository, file patterns and cache
folder (``cache_dir`` = faster-whisper's ``download_root``), so the later
``WhisperModel(..., local_files_only=True)`` finds the files.

Progress (verified against huggingface_hub 2.4.0 ``_snapshot_download.py`` and
``utils/_xet_progress_reporting.py``): ``snapshot_download`` creates two byte bars from
``tqdm_class`` - "Downloading bytes" (bytes received from the network; with the Xet backend
the total is only an estimate because of de-duplication/compression) and "Reconstructing"
(bytes written into the files; its ``total`` grows to the sum of the file sizes as each file
starts) - plus a file-count bar from ``hf_thread_map``. A class that does not subclass
huggingface_hub's own tqdm is instantiated without ``disable``, i.e. it always receives updates.

The Xet backend writes into a temporary ``*.incomplete`` file next to the cache blobs and keeps
fetched chunks in its own cache (``HF_HOME\\xet``) - the model folder itself does not grow until a
file is complete, so progress must come from these callbacks, not from the folder size.

Resuming: files that finished stay in the cache and are not downloaded again. huggingface_hub
deletes a partially written file on interruption (unique temp name per process); with Xet,
chunks already fetched are reused from its chunk cache (not verified for every case).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tqdm.std import tqdm as _std_tqdm

# huggingface_hub file patterns used by faster_whisper.utils.download_model (1.2.1)
ALLOW_PATTERNS = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]

DownloadReport = Callable[[int, int, int], None]  # (bytes written, total bytes, bytes received)


class _NullFile:
    def write(self, s: str) -> int:
        return len(s)

    def flush(self) -> None:
        pass


class ProgressAggregator:
    """Collects the byte counters of all bars and reports them throttled."""

    def __init__(self, report: DownloadReport, interval_s: float = 0.5) -> None:
        self.report = report
        self.interval_s = interval_s
        self.written = 0
        self.total = 0
        self.received = 0
        self._last = 0.0
        self._lock = threading.Lock()

    def update(self, bar: Any) -> None:
        with self._lock:
            desc = str(getattr(bar, "desc", "") or "")
            n = int(getattr(bar, "n", 0) or 0)
            total = int(getattr(bar, "total", 0) or 0)
            if desc.startswith("Downloading bytes") or desc.startswith("Download complete"):
                self.received = n
            else:
                self.written = n
                self.total = max(self.total, total)
            now = time.monotonic()
            if now - self._last >= self.interval_s:
                self._last = now
                self.report(self.written, self.total, self.received)

    def finish(self) -> None:
        self.report(self.total or self.written, self.total or self.written, self.received)


def make_tqdm_class(agg: ProgressAggregator) -> type:
    class ReportingTqdm(_std_tqdm):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.pop("name", None)
            kwargs["disable"] = False
            kwargs["file"] = _NullFile()
            self._is_bytes = kwargs.get("unit") == "B"
            super().__init__(*args, **kwargs)

        def update(self, n: float | None = 1) -> bool | None:
            res = super().update(n)
            if self._is_bytes:
                agg.update(self)
            return res

    return ReportingTqdm


def repo_for(model: str) -> str:
    if "/" in model:
        return model
    try:
        from faster_whisper.utils import _MODELS
    except ImportError:  # pragma: no cover
        _MODELS = {}
    repo = _MODELS.get(model)
    if repo is None:
        raise ValueError(f"Unbekanntes Whisper-Modell {model!r}")
    return str(repo)


def is_local_path(model: str) -> bool:
    return Path(model).is_dir()


def download_whisper_model(model: str, download_root: str | Path, report: DownloadReport,
                           snapshot_download: Callable[..., str] | None = None) -> str:
    """Download (or complete) the model into ``download_root``; returns the snapshot folder."""
    if is_local_path(model):
        return model
    if snapshot_download is None:
        from huggingface_hub import snapshot_download as hf_snapshot_download

        snapshot_download = hf_snapshot_download
    agg = ProgressAggregator(report)
    path = snapshot_download(repo_for(model), cache_dir=str(download_root), allow_patterns=ALLOW_PATTERNS,
                             tqdm_class=make_tqdm_class(agg))
    agg.finish()
    return str(path)


def fmt_bytes(n: int) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.2f}".replace(".", ",") + " GB"
    return f"{n / 1e6:.0f} MB"


def describe_progress(written: int, total: int, received: int) -> tuple[float | None, str]:
    """(fraction or None, German status text) for a DOWNLOAD report."""
    done = max(written, received)
    if total > 0:
        frac = min(0.999, done / total) if done < total else 1.0
        return frac, f"Modell wird heruntergeladen: {fmt_bytes(done)} von {fmt_bytes(total)} ({frac * 100:.0f} %)"
    return None, f"Modell wird heruntergeladen: {fmt_bytes(done)} empfangen"
