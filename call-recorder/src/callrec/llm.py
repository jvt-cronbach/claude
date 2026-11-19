"""Summary LLM providers behind a small interface.

- ``OllamaProvider``: Ollama native API (``/api/tags``, ``/api/chat``, ``/api/pull``, ``/api/version``)
- ``OpenAICompatProvider``: any OpenAI-compatible server (``/v1/models``, ``/v1/chat/completions``),
  e.g. LM Studio or llama.cpp ``llama-server``.

Endpoints must be on this computer unless the user set the explicitly named override
``summary.allow_remote_endpoint_data_leaves_this_computer``.
Field names were checked against the Ollama ``docs/api.md`` / ``docs/api/errors.mdx`` and the
llama.cpp ``tools/server/README.md`` (see README "Prüfstand").
"""

from __future__ import annotations

import ipaddress
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

PullProgress = Callable[[str, float | None], None]  # (status text, fraction 0..1 or None)


class LlmUnavailable(RuntimeError):
    """Server not reachable / model missing - the summary is skipped and can be re-run later."""


class RemoteEndpointRefused(ValueError):
    pass


def is_loopback_host(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# Characters that make URL parsers disagree about the host (userinfo, backslash as
# path separator, whitespace/control characters). urllib.parse and urllib3 (which
# requests uses to connect) parse e.g. "http://evil.example\\x@127.0.0.1" differently:
# the former sees 127.0.0.1, the latter connects to evil.example.
_AMBIGUOUS_URL_CHARS = set("@\\") | {chr(c) for c in range(0x21)} | {"\x7f"}


def _connect_host(url: str) -> str:
    """Host that ``requests`` would actually connect to for ``url``."""
    from urllib3.util import parse_url

    prepared = requests.Request("GET", url).prepare().url or ""
    return (parse_url(prepared).host or "").strip("[]").lower()


def check_endpoint(url: str, allow_remote: bool) -> bool:
    """Return True if the URL is local; raise unless remote endpoints were explicitly allowed."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
    except ValueError as exc:
        raise RemoteEndpointRefused(f"Ungültige Adresse {url!r}") from exc
    if parsed.scheme not in ("http", "https"):
        raise RemoteEndpointRefused(f"Ungültige Adresse {url!r}")
    ambiguous = any(ch in _AMBIGUOUS_URL_CHARS for ch in url)
    if not ambiguous and is_loopback_host(hostname):
        try:
            same = _connect_host(url) == hostname.strip("[]").lower()
        except Exception:  # noqa: BLE001 - unparsable for requests: not local
            same = False
        if same:
            return True
        ambiguous = True
    if ambiguous and not allow_remote:
        raise RemoteEndpointRefused(
            f"Die Adresse {url!r} ist mehrdeutig (Benutzerangabe '@', '\\' oder Leerzeichen) und wird abgelehnt."
        )
    if not allow_remote:
        raise RemoteEndpointRefused(
            f"Die Adresse {url!r} liegt nicht auf diesem Rechner. Gesprächsinhalte dürfen den Rechner nicht "
            "verlassen. (Nur wenn Sie den Server vollständig kontrollieren: "
            "summary.allow_remote_endpoint_data_leaves_this_computer = true)"
        )
    log.warning("ACHTUNG: Zusammenfassung über entfernten Server %s - Gesprächsinhalte verlassen diesen Rechner!", url)
    return False


class LlmProvider(Protocol):
    name: str
    base_url: str

    def list_models(self) -> list[str]: ...

    def chat(self, model: str, messages: list[dict[str, str]], options: dict[str, Any]) -> str: ...

    def version(self) -> str | None: ...

    @property
    def can_pull(self) -> bool: ...

    def pull(self, model: str, progress: PullProgress) -> None: ...


class _NoRedirectSession(requests.Session):
    """Never follows redirects: a loopback server must not be able to forward the
    transcript (307/308 keep the POST body) to another host."""

    def get_redirect_target(self, resp: Any) -> None:  # type: ignore[override]
        return None


def _refuse_redirect(r: Any) -> None:
    if 300 <= int(getattr(r, "status_code", 0) or 0) < 400:
        raise LlmUnavailable(
            f"KI-Server antwortet mit Weiterleitung ({r.status_code}) - Weiterleitungen werden nicht befolgt."
        )


def _session(base: Any = None) -> Any:
    if base is not None:
        return base
    s = _NoRedirectSession()
    s.trust_env = False  # never route through a system proxy
    return s


@dataclass
class OllamaProvider:
    base_url: str
    timeout: float = 900.0
    allow_remote: bool = False
    session: Any = None
    name: str = "ollama"

    def __post_init__(self) -> None:
        self.is_local = check_endpoint(self.base_url, self.allow_remote)
        self.base_url = self.base_url.rstrip("/")
        self.session = _session(self.session)

    def _unreachable(self, exc: Exception) -> LlmUnavailable:
        return LlmUnavailable(
            f"Ollama ist nicht erreichbar unter {self.base_url} ({exc.__class__.__name__}). "
            "Bitte Ollama starten und die Zusammenfassung später erneut erstellen."
        )

    @staticmethod
    def _error_text(r: Any) -> str:
        try:
            return str(r.json().get("error", r.text))
        except ValueError:
            return str(r.text)[:300]

    def list_models(self) -> list[str]:
        try:
            r = self.session.get(f"{self.base_url}/api/tags", timeout=10)
        except requests.RequestException as exc:
            raise self._unreachable(exc) from exc
        _refuse_redirect(r)
        if r.status_code >= 400:
            raise LlmUnavailable(f"Ollama-Fehler {r.status_code}: {self._error_text(r)}")
        return [str(m.get("name") or m.get("model") or "") for m in r.json().get("models", [])]

    def version(self) -> str | None:
        try:
            r = self.session.get(f"{self.base_url}/api/version", timeout=5)
            _refuse_redirect(r)
            r.raise_for_status()
            return str(r.json().get("version") or "") or None
        except (requests.RequestException, ValueError, LlmUnavailable):
            return None

    def chat(self, model: str, messages: list[dict[str, str]], options: dict[str, Any]) -> str:
        payload = {"model": model, "messages": messages, "stream": False, "options": options}
        try:
            r = self.session.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise self._unreachable(exc) from exc
        _refuse_redirect(r)
        if r.status_code == 404:
            raise LlmUnavailable(f"Modell {model!r} ist in Ollama nicht installiert (ollama pull {model}).")
        if r.status_code >= 400:
            raise LlmUnavailable(f"Ollama-Fehler {r.status_code}: {self._error_text(r)}")
        data = r.json()
        if "error" in data:
            raise LlmUnavailable(f"Ollama-Fehler: {data['error']}")
        return str((data.get("message") or {}).get("content", ""))

    @property
    def can_pull(self) -> bool:
        return True

    def pull(self, model: str, progress: PullProgress) -> None:
        """Download or update a model; streams NDJSON status objects."""
        try:
            r = self.session.post(
                f"{self.base_url}/api/pull", json={"model": model, "stream": True}, stream=True, timeout=(10, 3600)
            )
        except requests.RequestException as exc:
            raise self._unreachable(exc) from exc
        _refuse_redirect(r)
        if r.status_code >= 400:
            raise LlmUnavailable(f"Download fehlgeschlagen ({r.status_code}): {self._error_text(r)}")
        last = ""
        for raw in r.iter_lines():
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if "error" in obj:
                raise LlmUnavailable(f"Download fehlgeschlagen: {obj['error']}")
            last = str(obj.get("status", ""))
            total, completed = obj.get("total"), obj.get("completed")
            frac = (float(completed) / float(total)) if total and completed is not None else None
            progress(last, frac)
        if last != "success":
            raise LlmUnavailable(f"Download nicht abgeschlossen (letzter Status: {last or 'keiner'})")


@dataclass
class OpenAICompatProvider:
    base_url: str  # including /v1, e.g. http://127.0.0.1:1234/v1
    timeout: float = 900.0
    api_key: str = ""
    allow_remote: bool = False
    session: Any = None
    name: str = "openai"

    def __post_init__(self) -> None:
        self.is_local = check_endpoint(self.base_url, self.allow_remote)
        self.base_url = self.base_url.rstrip("/")
        self.session = _session(self.session)

    def _headers(self) -> dict[str, str]:
        # OpenAI-style auth header; local servers usually accept any value
        return {"Authorization": f"Bearer {self.api_key or 'no-key'}"}

    def list_models(self) -> list[str]:
        try:
            r = self.session.get(f"{self.base_url}/models", headers=self._headers(), timeout=10)
        except requests.RequestException as exc:
            raise LlmUnavailable(f"KI-Server nicht erreichbar unter {self.base_url} ({exc.__class__.__name__}).") from exc
        _refuse_redirect(r)
        if r.status_code >= 400:
            raise LlmUnavailable(f"KI-Server-Fehler {r.status_code}: {str(r.text)[:300]}")
        return [str(m.get("id", "")) for m in r.json().get("data", [])]

    def version(self) -> str | None:
        return None  # not part of the OpenAI API

    def chat(self, model: str, messages: list[dict[str, str]], options: dict[str, Any]) -> str:
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if "temperature" in options:
            payload["temperature"] = options["temperature"]
        if options.get("max_tokens"):
            payload["max_tokens"] = options["max_tokens"]
        try:
            r = self.session.post(f"{self.base_url}/chat/completions", json=payload, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            raise LlmUnavailable(f"KI-Server nicht erreichbar unter {self.base_url}: {exc}") from exc
        _refuse_redirect(r)
        if r.status_code >= 400:
            raise LlmUnavailable(f"KI-Server-Fehler {r.status_code}: {str(r.text)[:300]}")
        choices = r.json().get("choices") or []
        if not choices:
            raise LlmUnavailable("KI-Server hat keine Antwort geliefert.")
        return str((choices[0].get("message") or {}).get("content") or "")

    @property
    def can_pull(self) -> bool:
        return False

    def pull(self, model: str, progress: PullProgress) -> None:
        raise LlmUnavailable("Modelle für diesen Server bitte in dessen eigener Oberfläche laden (z.B. LM Studio).")


def make_provider(cfg: Any, session: Any = None) -> LlmProvider:
    """Build the provider from a SummaryConfig."""
    allow = bool(cfg.allow_remote_endpoint_data_leaves_this_computer)
    if cfg.provider == "openai":
        return OpenAICompatProvider(cfg.openai_url, cfg.timeout_s, cfg.openai_api_key, allow, session)
    return OllamaProvider(cfg.ollama_url, cfg.timeout_s, allow, session)


def model_available(wanted: str, installed: list[str]) -> bool:
    if wanted in installed:
        return True
    return ":" not in wanted and f"{wanted}:latest" in installed


OLLAMA_UPDATE_HINT = (
    "Ollama aktualisiert sich nicht automatisch über CallRecorder. Aktualisieren: "
    "'winget upgrade Ollama.Ollama' in PowerShell ausführen."
)
