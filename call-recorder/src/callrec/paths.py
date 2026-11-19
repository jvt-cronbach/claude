"""Well-known locations (config in %APPDATA%, data in %LOCALAPPDATA%).

Setting the environment variable ``CALLREC_HOME`` relocates everything below one
directory; the test-suite uses that, and it is handy for a portable setup.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from callrec import APP_NAME

IS_WINDOWS = sys.platform == "win32"


def _home_override() -> Path | None:
    value = os.environ.get("CALLREC_HOME")
    return Path(value) if value else None


def config_dir() -> Path:
    override = _home_override()
    if override:
        return override / "config"
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME


def data_dir() -> Path:
    """Machine-local data: token cache, voice profiles, models, logs."""
    override = _home_override()
    if override:
        return override / "data"
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME


def config_file() -> Path:
    return config_dir() / "config.toml"


def log_dir() -> Path:
    return data_dir() / "logs"


def models_dir() -> Path:
    return data_dir() / "models"


def token_cache_file() -> Path:
    return data_dir() / "msal_token_cache.bin"


def voice_profiles_file() -> Path:
    return data_dir() / "voiceprints.bin"


def ics_dir() -> Path:
    return data_dir() / "ics"


def default_recordings_dir() -> Path:
    # Deliberately NOT inside "Documents": on many Windows installs Documents is
    # redirected into OneDrive, which would upload call audio to the cloud.
    override = _home_override()
    if override:
        return override / "recordings"
    return Path.home() / "Anrufaufnahmen"


def cloud_sync_root_for(path: Path, environ: dict[str, str] | None = None) -> Path | None:
    """Return the OneDrive root that contains ``path`` (if any).

    Used to warn the user when recordings would be synced to the cloud.
    """
    env = os.environ if environ is None else environ
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        resolved = path
    for key in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        root = env.get(key)
        if not root:
            continue
        root_path = Path(root)
        try:
            root_path = root_path.resolve()
        except OSError:
            pass
        if resolved == root_path or root_path in resolved.parents:
            return root_path
    # Heuristic for setups without the environment variables.
    if any(part.lower().startswith("onedrive") for part in resolved.parts):
        return resolved
    return None
