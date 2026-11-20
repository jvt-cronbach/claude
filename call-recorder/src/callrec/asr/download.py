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

Why "0 MB empfangen" could still appear: the byte bars are fed only by hf_xet's
``progress_callback`` (``XetDownloadProgressReporter.update_progress``) or by ``http_get``. Driving
the real ``snapshot_download`` -> ``xet_get`` -> ``XetDownloadProgressReporter`` -> ``_AggregatedTqdm``
chain with a fake Xet session (tests/test_cpu_and_downloads.py) shows the bytes do arrive in our bars,
so the zero must come from the Rust side not reporting during the transfer on that machine
(exceptions raised inside the callback are swallowed by hf_xet: "Python exception updating
progress"). That cannot be decided offline. Therefore progress no longer depends on the callback
alone: a watcher measures what really lands on disk (repository folder incl. ``*.incomplete`` plus
Hugging Face's Xet chunk cache ``HF_XET_CACHE``) and reports elapsed time and growth; the display
uses the callback bytes when there are any and otherwise says honestly that only the disk growth is
known. After the download the model is verified offline (:func:`verify_snapshot`) - no "Fertig"
without a complete model.

Resuming: files that finished stay in the cache and are not downloaded again. huggingface_hub
deletes a partially written file on interruption (unique temp name per process); with Xet,
chunks already fetched are reused from its chunk cache (not verified for every case).
"""

from __future__ import annotations

import os
import stat
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


STALE_PARTIAL_AGE_S = 600.0


def remove_stale_partials(download_root: str | Path, repo: str, max_age_s: float = STALE_PARTIAL_AGE_S,
                          now: float | None = None) -> int:
    """Delete partial files left behind by a *killed* download.

    huggingface_hub 2.4 writes each file to a process-unique ``<etag>.<random>.incomplete`` and only
    removes it in a ``finally`` block. "Verarbeitung abbrechen" / app exit kill the worker
    (TerminateProcess), so that cleanup never runs and every cancelled attempt would leave up to
    ~3 GB that is never reused. Files still being written by another process are recent (or locked
    on Windows) and are left alone.
    """
    blobs = Path(download_root) / ("models--" + repo.replace("/", "--")) / "blobs"
    if not blobs.is_dir():
        return 0
    now = time.time() if now is None else now
    removed = 0
    for f in blobs.glob("*.incomplete"):
        try:
            if now - f.stat().st_mtime >= max_age_s:
                f.unlink()
                removed += 1
        except OSError:  # in use by another download (Windows) or already gone
            continue
    return removed


DiskReport = Callable[[float, int], None]  # (elapsed seconds, bytes added on disk since start)


def repo_folder(download_root: str | Path, repo: str) -> Path:
    """huggingface_hub cache layout: <cache_dir>/models--<org>--<name>"""
    return Path(download_root) / ("models--" + repo.replace("/", "--"))


def tree_size(path: Path) -> int:
    """Bytes of regular files below ``path`` (symlinks are not followed, so blobs count once)."""
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total


def xet_cache_dir() -> Path | None:
    try:
        from huggingface_hub import constants

        return Path(constants.HF_XET_CACHE)
    except Exception:  # noqa: BLE001 - informational only
        return None


class DiskWatcher:
    """Reports how much has landed on disk (model folder + Xet chunk cache) every ``interval_s``."""

    def __init__(self, paths: list[Path], report: DiskReport, interval_s: float = 2.0) -> None:
        self.paths = paths
        self.report = report
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._baseline = self.measure()
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="download-disk", daemon=True)

    def measure(self) -> int:
        return sum(tree_size(p) for p in self.paths)

    def growth(self) -> int:
        return max(0, self.measure() - self._baseline)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.report(time.monotonic() - self._started, self.growth())
            except Exception:  # noqa: BLE001 - never break the download for a display problem
                pass

    def start(self) -> DiskWatcher:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()


def download_whisper_model(model: str, download_root: str | Path, report: DownloadReport,
                           snapshot_download: Callable[..., str] | None = None,
                           disk_report: DiskReport | None = None, disk_interval_s: float = 2.0) -> str:
    """Download (or complete) the model into ``download_root``; returns the snapshot folder."""
    if is_local_path(model):
        return model
    repo = repo_for(model)
    remove_stale_partials(download_root, repo)
    if snapshot_download is None:
        from huggingface_hub import snapshot_download as hf_snapshot_download

        snapshot_download = hf_snapshot_download
    agg = ProgressAggregator(report)
    watched = [repo_folder(download_root, repo)]
    xet = xet_cache_dir()
    if xet is not None:
        watched.append(xet)
    watcher = DiskWatcher(watched, disk_report, disk_interval_s).start() if disk_report else None
    try:
        path = snapshot_download(repo, cache_dir=str(download_root), allow_patterns=ALLOW_PATTERNS,
                                 tqdm_class=make_tqdm_class(agg))
    finally:
        if watcher is not None:
            watcher.stop()
    agg.finish()
    return str(path)


class ModelIncomplete(RuntimeError):
    pass


REQUIRED_FILES = ("config.json", "model.bin", "tokenizer.json")


def verify_snapshot(model: str, download_root: str | Path, resolver: Callable[..., str] | None = None) -> int:
    """Check offline that faster-whisper finds a complete model; returns its size in bytes.

    Uses faster-whisper's own ``download_model(..., local_files_only=True)`` - the same lookup
    ``WhisperModel(..., local_files_only=True)`` does - then checks the files it needs.
    """
    if is_local_path(model):
        folder = Path(model)
    else:
        if resolver is None:
            from faster_whisper.utils import download_model

            resolver = download_model
        try:
            folder = Path(resolver(model, local_files_only=True, cache_dir=str(download_root)))
        except Exception as exc:  # LocalEntryNotFoundError & co.
            raise ModelIncomplete(f"Modell {model} ist nach dem Download nicht vollständig im Cache ({exc.__class__.__name__}).") from exc
    missing = [f for f in REQUIRED_FILES if not (folder / f).is_file()]
    if not any(folder.glob("vocabulary.*")):
        missing.append("vocabulary.*")
    if missing:
        raise ModelIncomplete(f"Modell {model}: Dateien fehlen ({', '.join(missing)}) in {folder}")
    if (folder / "model.bin").stat().st_size < 1_000_000:
        raise ModelIncomplete(f"Modell {model}: model.bin ist unvollständig ({folder})")
    return sum(p.stat().st_size for p in folder.iterdir() if p.is_file())


def fmt_bytes(n: int) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.2f}".replace(".", ",") + " GB"
    return f"{n / 1e6:.0f} MB"


def fmt_elapsed(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d} min" if s >= 60 else f"{s} s"


def describe_progress(written: int, total: int, received: int) -> tuple[float | None, str]:
    """(fraction or None, German status text) for a DOWNLOAD report; ``None`` text = nothing known yet."""
    done = max(written, received)
    if total > 0 and done > 0:
        frac = min(0.999, done / total) if done < total else 1.0
        return frac, f"Modell wird heruntergeladen: {fmt_bytes(done)} von {fmt_bytes(total)} ({frac * 100:.0f} %)"
    if done > 0:
        return None, f"Modell wird heruntergeladen: {fmt_bytes(done)} empfangen"
    return None, ""


def describe_disk(elapsed_s: float, disk_bytes: int) -> str:
    """Honest indeterminate status when the byte callback reports nothing."""
    text = f"Modell wird heruntergeladen … {fmt_elapsed(elapsed_s)}"
    if disk_bytes > 0:
        text += f", bisher {fmt_bytes(disk_bytes)} auf der Festplatte (genauer Fortschritt nicht verfügbar)"
    else:
        text += " (genauer Fortschritt nicht verfügbar)"
    return text


class DownloadDisplay:
    """Combines DOWNLOAD (callback bytes) and DOWNLOADDISK (disk growth) worker lines into one text."""

    def __init__(self) -> None:
        self.callback_text = ""
        self.frac: float | None = None
        self.final = ""

    def feed(self, parts: list[str]) -> str | None:
        kind = parts[0] if parts else ""
        try:
            if kind == "DOWNLOAD" and len(parts) >= 4:
                frac, text = describe_progress(int(parts[1]), int(parts[2]), int(parts[3]))
                if text:
                    self.callback_text, self.frac = text, frac
                    return text
                return None
            if kind == "DOWNLOADDISK" and len(parts) >= 3:
                if self.callback_text:  # real byte progress is shown already
                    return None
                return describe_disk(float(parts[1]), int(parts[2]))
            if kind == "DOWNLOADED" and len(parts) >= 2:
                self.final = f"Modell vollständig heruntergeladen und geprüft ({fmt_bytes(int(parts[1]))})."
                return self.final
        except ValueError:
            return None
        return None
