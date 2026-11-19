"""Summary provider: Claude via the official Anthropic API (cloud - the transcript leaves this computer).

Strictly opt-in (see :func:`callrec.llm.make_provider`): only usable after the explicit
confirmation in the settings dialog / ``callrec anthropic enable``; never via a preset.

Safety properties
- talks only to ``https://api.anthropic.com`` (fixed, not configurable; ``ANTHROPIC_BASE_URL``
  is ignored because the base URL is passed explicitly), every request is checked against
  that host, HTTP redirects are never followed;
- the API key is passed explicitly (stored DPAPI-protected or from ``ANTHROPIC_API_KEY``),
  so no other credential source (profiles, OAuth) is consulted;
- only the chat messages built by the summarizer (prompt templates, title, date, participant
  names, transcript text) are sent - never audio, voice profiles or e-mail addresses.

SDK usage follows the ``claude-api`` skill (anthropic Python SDK 2.x): ``client.beta.messages.stream``
with ``get_final_message()``, adaptive thinking (default on current models, so ``thinking`` is
omitted), ``output_config={"effort": ...}``, no ``temperature`` (rejected by current models),
optional server-side refusal fallback (beta ``server-side-fallback-2026-07-01``,
``fallbacks="default"``), typed exceptions for error handling.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from typing import Any

from callrec.llm import LlmUnavailable, PullProgress, RemoteEndpointRefused

log = logging.getLogger(__name__)

API_HOST = "api.anthropic.com"
API_BASE = f"https://{API_HOST}"
DEFAULT_MODEL = "claude-opus-5-5"
# Shown in the settings dialog (ids and prices as listed by the claude-api skill, 2026-11).
MODEL_CHOICES: dict[str, str] = {
    "claude-opus-5-5": "Claude Opus 5.5 – Standard, beste Qualität (4 $/20 $ je 1 Mio. Token Eingabe/Ausgabe)",
    "claude-sonnet-5-5": "Claude Sonnet 5.5 – schneller, günstiger (2 $/10 $)",
    "claude-haiku-5-5": "Claude Haiku 5.5 – am günstigsten (0,10 $/0,50 $)",
}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models for which the skill documents server-side refusal fallbacks.
_FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1")
_NON_RETRYABLE_429 = ("spend_limit", "ramp_limit")

StreamProgress = Callable[[str], None]  # status text (heartbeat / streamed characters)


class AnthropicError(LlmUnavailable):
    """Summary via Anthropic failed; the message is meant for the user (German)."""


class ContextTooLong(AnthropicError):
    """The prompt exceeds the model's context window - the caller splits the transcript further."""


class CloudNotConfirmed(LlmUnavailable):
    pass


NO_KEY_MESSAGE = (
    "Kein Anthropic-API-Schlüssel hinterlegt. Bitte unter Einstellungen > Zusammenfassung eintragen "
    "oder 'callrec anthropic set-key' ausführen (alternativ Umgebungsvariable ANTHROPIC_API_KEY)."
)


def _sdk() -> Any:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - dependency is pinned in requirements.txt
        raise AnthropicError("Das Paket 'anthropic' ist nicht installiert (setup.ps1 erneut ausführen).") from exc
    return anthropic


def _guard_request(request: Any) -> None:
    url = request.url
    if url.scheme != "https" or str(url.host).lower() != API_HOST:
        raise RemoteEndpointRefused(f"Anfrage an {url.host!s} blockiert - erlaubt ist nur {API_HOST}.")


def _refuse_redirect(response: Any) -> None:
    if 300 <= int(response.status_code) < 400:
        raise AnthropicError(
            f"Die Anthropic-API antwortet mit einer Weiterleitung ({response.status_code}) - Weiterleitungen werden nicht befolgt."
        )


def build_client(api_key: str, timeout_s: float, sdk_max_retries: int = 2, transport: Any = None) -> Any:
    """Anthropic client pinned to the official host, without redirects and without other credential sources."""
    anthropic = _sdk()
    http_kwargs: dict[str, Any] = {
        "follow_redirects": False,
        "event_hooks": {"request": [_guard_request], "response": [_refuse_redirect]},
    }
    if transport is not None:
        http_kwargs["transport"] = transport
    http_client = anthropic.DefaultHttpxClient(**http_kwargs)
    return anthropic.Anthropic(
        api_key=api_key,
        base_url=API_BASE,
        max_retries=sdk_max_retries,
        timeout=anthropic.Timeout(timeout_s, connect=15.0),
        http_client=http_client,
    )


def _retry_after(exc: Any) -> float | None:
    try:
        value = exc.response.headers.get("retry-after")
        return float(value) if value else None
    except (AttributeError, TypeError, ValueError):
        return None


class AnthropicProvider:
    """Implements the :class:`callrec.llm.LlmProvider` interface for the Claude API."""

    name = "anthropic"
    streams_progress = True
    base_url = API_BASE
    is_local = False

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        timeout_s: float = 900.0,
        effort: str = "medium",
        max_tokens: int = 32000,
        max_chunk_chars: int = 300000,
        max_attempts: int = 4,
        refusal_fallback: bool = True,
        key_source: str = "",
        transport: Any = None,
        sdk_max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_key = api_key.strip()
        self.model = model.strip() or DEFAULT_MODEL
        self.timeout_s = timeout_s
        self.effort = effort
        self.max_tokens = max_tokens
        self.max_chunk_chars = max_chunk_chars
        self.max_attempts = max(1, max_attempts)
        self.refusal_fallback = refusal_fallback
        self.key_source = key_source
        self._transport = transport
        self._sdk_max_retries = sdk_max_retries
        self._sleep = sleep
        self._client: Any = None
        self._lock = threading.Lock()
        self.last_served_model = ""
        self.usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
        self.used_fallback = False

    # -- helpers ---------------------------------------------------------
    @property
    def client(self) -> Any:
        if not self.api_key:
            raise AnthropicError(NO_KEY_MESSAGE)
        with self._lock:
            if self._client is None:
                self._client = build_client(self.api_key, self.timeout_s, self._sdk_max_retries, self._transport)
            return self._client

    def _fallback_enabled(self, model: str) -> bool:
        return self.refusal_fallback and model in _FALLBACK_MODELS

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "endpoint": API_BASE,
            "model": self.model,
            "served_model": self.last_served_model or self.model,
            "effort": self.effort,
            "refusal_fallback": self.refusal_fallback,
            "fallback_used": self.used_fallback,
            "usage": dict(self.usage),
            "data_sent_to": "Anthropic (api.anthropic.com): Transkripttext, Titel, Datum, Teilnehmernamen",
        }

    def translate_error(self, exc: BaseException, model: str) -> AnthropicError:
        """Map SDK exceptions to clear German messages (most specific first)."""
        anthropic = _sdk()
        if isinstance(exc, AnthropicError):
            return exc
        if isinstance(exc, anthropic.AuthenticationError):
            return AnthropicError(
                "Anthropic lehnt den API-Schlüssel ab (ungültig, widerrufen oder falsch kopiert). "
                "Bitte unter Einstellungen > Zusammenfassung neu eintragen."
            )
        if isinstance(exc, anthropic.PaymentRequiredError) or getattr(exc, "type", None) == "billing_error":
            return AnthropicError(
                "Anthropic meldet ein Abrechnungsproblem (kein Guthaben / Zahlungsmittel). Bitte unter "
                "console.anthropic.com > Billing Guthaben aufladen."
            )
        if isinstance(exc, anthropic.PermissionDeniedError):
            return AnthropicError(f"Der API-Schlüssel darf diese Anfrage nicht ausführen (403): {getattr(exc, 'api_message', '') or exc}")
        if isinstance(exc, anthropic.NotFoundError):
            return AnthropicError(f"Das Modell {model!r} ist unbekannt oder für Ihr Konto nicht verfügbar (Einstellungen > Modell prüfen).")
        if isinstance(exc, anthropic.RateLimitError):
            code = str(getattr(exc, "error_code", "") or "")
            if any(c in code for c in _NON_RETRYABLE_429):
                return AnthropicError(
                    "Das Ausgabenlimit Ihres Anthropic-Kontos ist erreicht. Limit in der Anthropic-Konsole erhöhen "
                    "oder bis zum Monatswechsel warten; danach die Zusammenfassung neu erstellen."
                )
            return AnthropicError("Anthropic-Ratenlimit erreicht - bitte später erneut versuchen (Zusammenfassung neu erstellen).")
        if isinstance(exc, anthropic.BadRequestError):
            msg = str(getattr(exc, "api_message", "") or exc)
            low = msg.lower()
            if "prompt is too long" in low or "too many tokens" in low:
                return ContextTooLong(f"Das Transkript ist zu lang für ein Modell-Kontextfenster ({msg}).")
            if "credit balance" in low:  # wording of the API's low-credit error (not covered by the skill)
                return AnthropicError(
                    "Das Guthaben Ihres Anthropic-Kontos reicht nicht aus. Bitte unter console.anthropic.com > "
                    "Billing aufladen und die Zusammenfassung neu erstellen."
                )
            return AnthropicError(f"Anthropic hat die Anfrage abgelehnt (400): {msg}")
        if isinstance(exc, anthropic.ContentTooLargeError):
            return ContextTooLong("Die Anfrage ist zu groß für die Anthropic-API (413).")
        if isinstance(exc, anthropic.InternalServerError):
            return AnthropicError(
                f"Anthropic ist gerade überlastet oder gestört ({getattr(exc, 'status_code', '?')}). "
                "Bitte später erneut versuchen (Zusammenfassung neu erstellen)."
            )
        if isinstance(exc, anthropic.APITimeoutError):
            return AnthropicError("Zeitüberschreitung bei der Verbindung zu api.anthropic.com. Bitte später erneut versuchen.")
        if isinstance(exc, anthropic.APIConnectionError):
            return AnthropicError(
                "Keine Verbindung zu api.anthropic.com (Internet, Proxy oder Firewall prüfen). "
                "Die Zusammenfassung kann später neu erstellt werden."
            )
        if isinstance(exc, anthropic.APIStatusError):
            return AnthropicError(f"Fehler der Anthropic-API ({exc.status_code}): {getattr(exc, 'api_message', '') or exc}")
        if isinstance(exc, RemoteEndpointRefused):
            return AnthropicError(str(exc))
        return AnthropicError(f"Unerwarteter Fehler bei der Anthropic-API: {exc.__class__.__name__}: {exc}")

    def _retryable(self, exc: BaseException) -> bool:
        anthropic = _sdk()
        if isinstance(exc, anthropic.RateLimitError):
            code = str(getattr(exc, "error_code", "") or "")
            return not any(c in code for c in _NON_RETRYABLE_429)
        return isinstance(exc, (anthropic.InternalServerError, anthropic.APIConnectionError))

    # -- LlmProvider -------------------------------------------------------
    def list_models(self) -> list[str]:
        try:
            return [str(m.id) for m in self.client.models.list()]
        except Exception as exc:
            raise self.translate_error(exc, self.model) from exc

    def version(self) -> str | None:
        return None

    @property
    def can_pull(self) -> bool:
        return False

    def pull(self, model: str, progress: PullProgress) -> None:
        raise AnthropicError("Claude-Modelle laufen bei Anthropic und müssen nicht geladen werden.")

    def _request(self, model: str, messages: list[dict[str, str]], use_fallback: bool,
                 on_text: Callable[[int], None]) -> Any:
        system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
        conv = [{"role": m["role"], "content": m["content"]} for m in messages if m.get("role") in ("user", "assistant")]
        kwargs: dict[str, Any] = {"model": model, "max_tokens": self.max_tokens, "messages": conv}
        if system:
            kwargs["system"] = system
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        if use_fallback:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        chars = 0
        with self.client.beta.messages.stream(**kwargs) as stream:
            for event in stream:
                if getattr(event, "type", "") == "text":
                    chars += len(event.text)
                    on_text(chars)
            return stream.get_final_message()

    def chat(self, model: str, messages: list[dict[str, str]], options: dict[str, Any],
             progress: StreamProgress | None = None) -> str:
        model = model or self.model
        report = progress or (lambda s: None)
        use_fallback = self._fallback_enabled(model)
        anthropic = _sdk()
        attempt = 0
        while True:
            attempt += 1
            try:
                final = self._request(model, messages, use_fallback,
                                      lambda n: report(f"Claude schreibt … {n} Zeichen empfangen"))
                break
            except anthropic.BadRequestError as exc:
                msg = str(getattr(exc, "api_message", "") or exc)
                if use_fallback and "anthropic-beta" in msg:
                    log.info("Fallback-Beta nicht verfügbar - wiederhole ohne Ersatzmodell")
                    use_fallback = False
                    continue
                raise self.translate_error(exc, model) from exc
            except Exception as exc:
                if not self._retryable(exc) or attempt >= self.max_attempts:
                    raise self.translate_error(exc, model) from exc
                delay = _retry_after(exc) or min(60.0, 5.0 * 2 ** (attempt - 1)) + random.uniform(0, 1)
                why = self.translate_error(exc, model)
                log.warning("Anthropic-Anfrage fehlgeschlagen (Versuch %d/%d): %s - neuer Versuch in %.0f s",
                            attempt, self.max_attempts, why, delay)
                report(f"Anthropic: {exc.__class__.__name__}, neuer Versuch {attempt + 1}/{self.max_attempts} in {delay:.0f} s")
                self._sleep(delay)
        self.usage["requests"] += 1
        usage = getattr(final, "usage", None)
        if usage is not None:
            self.usage["input_tokens"] += int(getattr(usage, "input_tokens", 0) or 0)
            self.usage["output_tokens"] += int(getattr(usage, "output_tokens", 0) or 0)
        self.last_served_model = str(getattr(final, "model", "") or model)
        if any(getattr(b, "type", "") == "fallback" for b in (final.content or [])):
            self.used_fallback = True
        stop = getattr(final, "stop_reason", None)
        if stop == "refusal":
            details = getattr(final, "stop_details", None)
            cat = getattr(details, "category", None) if details is not None else None
            raise AnthropicError(f"Claude hat die Zusammenfassung abgelehnt (Kategorie: {cat or 'unbekannt'}).")
        text = "".join(str(getattr(b, "text", "")) for b in (final.content or []) if getattr(b, "type", "") == "text")
        if stop == "max_tokens":
            log.warning("Antwort von Claude wurde bei max_tokens=%d abgeschnitten", self.max_tokens)
            text += "\n\n_(Hinweis: Die Antwort wurde wegen der Längenbegrenzung abgeschnitten.)_"
        return text

    def test_connection(self) -> str:
        """Check key, network, model access and credit with a minimal request (costs a fraction of a cent)."""
        anthropic = _sdk()
        try:
            info = self.client.models.retrieve(self.model)
            name = str(getattr(info, "display_name", "") or self.model)
            msg = self.client.messages.create(
                model=self.model, max_tokens=256,
                messages=[{"role": "user", "content": "Antworte nur mit: OK"}],
                **({"output_config": {"effort": "low"}} if self.effort else {}),
            )
        except anthropic.APIError as exc:
            raise self.translate_error(exc, self.model) from exc
        except RemoteEndpointRefused as exc:
            raise AnthropicError(str(exc)) from exc
        tokens = getattr(getattr(msg, "usage", None), "output_tokens", "?")
        return f"Verbindung zu Anthropic OK: Modell {name} ({self.model}) antwortet ({tokens} Ausgabe-Token)."


ANTHROPIC_SETTINGS_WARNING = (
    "Mit dem Anbieter „Claude (Anthropic)“ wird für jede Zusammenfassung das vollständige TRANSKRIPT "
    "(Text mit Sprechernamen und Zeitstempeln) sowie Titel, Datum und Teilnehmernamen an Anthropic "
    "(api.anthropic.com) übertragen und dort verarbeitet. Audio, Stimmprofile und E-Mail-Adressen werden "
    "nicht übertragen. Die Kosten werden pro Token über Ihr Anthropic-Konto abgerechnet.\n\n"
    "Vor jeder Aufnahme müssen Sie dann zusätzlich bestätigen, dass alle Teilnehmenden auch dieser "
    "Übertragung zugestimmt haben.\n\n"
    "Sollen Transkripte künftig an Anthropic gesendet werden?"
)
