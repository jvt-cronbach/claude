"""Storage of the Anthropic API key.

The key is never written to config.toml, presets or the log. It is kept in one file under
%LOCALAPPDATA%\\CallRecorder, encrypted with Windows DPAPI (same atomic protected-file
mechanism as the voice profiles), so only this Windows user on this computer can read it.
Alternatively the environment variable ``ANTHROPIC_API_KEY`` is used.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from callrec import paths

log = logging.getLogger(__name__)

ENV_VAR = "ANTHROPIC_API_KEY"
SOURCE_STORED = "gespeichert (DPAPI-verschlüsselt)"
SOURCE_ENV = f"Umgebungsvariable {ENV_VAR}"

_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{20,400}$")
# Pattern used to scrub keys from log records (see logging_setup.RedactingFilter)
KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}")


class ApiKeyError(ValueError):
    pass


def key_file() -> Path:
    return paths.data_dir() / "anthropic_api_key.bin"


def _persistence(path: Path | None = None) -> Any:
    from callrec.speakers.profiles import AtomicProtectedFile, PlainFilePersistence

    path = path or key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        from msal_extensions.windows import WindowsDataProtectionAgent

        return AtomicProtectedFile(path, WindowsDataProtectionAgent(entropy="callrec-anthropic-api-key"))
    log.warning("Nicht-Windows-System: API-Schlüssel wird UNVERSCHLÜSSELT gespeichert (%s)", path)
    return PlainFilePersistence(path)


def validate_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        raise ApiKeyError("Kein API-Schlüssel eingegeben.")
    if not _KEY_RE.match(key):
        raise ApiKeyError("Der API-Schlüssel enthält ungültige Zeichen oder ist zu kurz (bitte vollständig kopieren).")
    if key.startswith("sk-ant-admin"):
        raise ApiKeyError("Das ist ein Admin-Schlüssel; für Zusammenfassungen wird ein normaler API-Schlüssel benötigt.")
    if not key.startswith("sk-ant-"):
        log.warning("API-Schlüssel beginnt nicht mit 'sk-ant-' - bitte prüfen")
    return key


def store_key(key: str, persistence: Any = None) -> None:
    key = validate_key(key)
    (persistence or _persistence()).save(key)
    if sys.platform != "win32":
        try:
            os.chmod(key_file(), 0o600)
        except OSError:
            pass
    log.info("Anthropic-API-Schlüssel gespeichert (%s)", mask(key))


def delete_key(path: Path | None = None) -> bool:
    path = path or key_file()
    if path.exists():
        path.unlink()
        log.info("Anthropic-API-Schlüssel gelöscht")
        return True
    return False


def stored_key(persistence: Any = None) -> str:
    if not key_file().exists() and persistence is None:
        return ""
    try:
        return str((persistence or _persistence()).load()).strip()
    except FileNotFoundError:
        return ""
    except Exception as exc:  # undecryptable (other Windows user / machine)
        log.warning("Gespeicherter API-Schlüssel nicht lesbar: %s", exc.__class__.__name__)
        return ""


def load_key(environ: dict[str, str] | None = None, persistence: Any = None) -> tuple[str, str]:
    """Return ``(key, source)``; the stored key wins over the environment variable."""
    key = stored_key(persistence)
    if key:
        return key, SOURCE_STORED
    env = (environ if environ is not None else os.environ).get(ENV_VAR, "").strip()
    if env:
        return env, SOURCE_ENV
    return "", ""


def mask(key: str) -> str:
    key = key or ""
    if len(key) < 12:
        return "***"
    return f"{key[:7]}…{key[-4:]}"


def redact(text: str) -> str:
    return KEY_PATTERN.sub("sk-ant-***", text)
