"""Meeting minutes via a local LLM (Ollama on localhost only)."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import requests

from callrec.config import SummaryConfig

log = logging.getLogger(__name__)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

SYSTEM_PROMPT = """Du bist ein sorgfältiger Protokollführer. Du erstellst aus dem Transkript eines \
Gesprächs ein kurzes, sachliches Ergebnisprotokoll auf Deutsch.
Regeln:
- Verwende ausschließlich Informationen aus dem Transkript. Erfinde nichts.
- Nenne Personen mit den Namen aus dem Transkript. "Sprecher N" bleibt "Sprecher N".
- Wenn für eine Aufgabe niemand verantwortlich ist, schreibe "offen". Wenn keine Frist genannt wurde, schreibe "-".
- Das Transkript wurde automatisch erstellt und kann Erkennungsfehler enthalten.
- Antworte nur mit dem Protokoll in Markdown, ohne Vorrede."""

FINAL_TEMPLATE = """Erstelle das Ergebnisprotokoll mit genau diesen Abschnitten:

## Kurzfassung
(3-6 Sätze)

## Entscheidungen
(Aufzählung; "Keine" wenn es keine gab)

## Aufgaben
| Aufgabe | Verantwortlich | Frist |
|---|---|---|

## Offene Punkte
(Aufzählung; "Keine" wenn es keine gab)

Gespräch: {title}
Teilnehmende: {participants}

{material_label}:
{material}"""

CHUNK_TEMPLATE = """Das folgende ist Teil {i} von {n} eines Gesprächstranskripts.
Fasse diesen Teil in Stichpunkten zusammen: besprochene Themen, Entscheidungen, Aufgaben \
(mit verantwortlicher Person und Frist, falls genannt) und offene Fragen. Nur Inhalte aus diesem Teil.

{chunk}"""


class SummaryUnavailable(RuntimeError):
    """Ollama not reachable or model missing - summary is skipped, can be re-run later."""


class Summarizer(Protocol):
    model_name: str

    def summarize(self, title: str, participants: list[str], transcript_lines: list[str]) -> str: ...


def assert_local_url(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if host not in _LOCAL_HOSTS:
        raise ValueError(
            f"summary.ollama_url muss auf diesen Rechner zeigen (localhost/127.0.0.1), nicht {host!r}: "
            "Gesprächsinhalte dürfen den Rechner nicht verlassen."
        )


def chunk_lines(lines: list[str], max_chars: int) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if len(line) > max_chars:  # single huge line: hard split
            for i in range(0, len(line), max_chars):
                piece = line[i : i + max_chars]
                if current:
                    chunks.append(current)
                    current, size = [], 0
                chunks.append([piece])
            continue
        if size + len(line) + 1 > max_chars and current:
            chunks.append(current)
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append(current)
    return chunks


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def clean_output(text: str) -> str:
    text = _THINK_RE.sub("", text)
    text = re.sub(r"^```(?:markdown)?\s*\n", "", text.strip())
    text = re.sub(r"\n```\s*$", "", text)
    return text.strip()


@dataclass
class OllamaClient:
    base_url: str
    timeout: float
    session: Any = None

    def __post_init__(self) -> None:
        assert_local_url(self.base_url)
        if self.session is None:
            self.session = requests.Session()
            # never route localhost traffic through a configured system proxy
            self.session.trust_env = False
        self.base_url = self.base_url.rstrip("/")

    def list_models(self) -> list[str]:
        try:
            r = self.session.get(f"{self.base_url}/api/tags", timeout=5)
            r.raise_for_status()
        except requests.RequestException as exc:
            raise SummaryUnavailable(
                f"Ollama ist nicht erreichbar unter {self.base_url} ({exc.__class__.__name__}). "
                "Bitte Ollama starten und die Zusammenfassung später erneut erstellen."
            ) from exc
        return [str(m.get("name", "")) for m in r.json().get("models", [])]

    def chat(self, model: str, messages: list[dict[str, str]], options: dict[str, Any]) -> str:
        payload = {"model": model, "messages": messages, "stream": False, "options": options}
        try:
            r = self.session.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SummaryUnavailable(f"Ollama-Anfrage fehlgeschlagen: {exc}") from exc
        if r.status_code == 404:
            raise SummaryUnavailable(f"Modell {model!r} ist in Ollama nicht installiert (ollama pull {model}).")
        if r.status_code >= 400:
            raise SummaryUnavailable(f"Ollama-Fehler {r.status_code}: {r.text[:300]}")
        data = r.json()
        return str((data.get("message") or {}).get("content", ""))


def model_available(wanted: str, installed: list[str]) -> bool:
    if wanted in installed:
        return True
    # "gemma3" == "gemma3:latest"
    return ":" not in wanted and f"{wanted}:latest" in installed


class OllamaSummarizer:
    def __init__(self, cfg: SummaryConfig, client: OllamaClient | None = None, progress: Callable[[float], None] | None = None) -> None:
        self.cfg = cfg
        self.client = client or OllamaClient(cfg.ollama_url, cfg.timeout_s)
        self.model_name = cfg.model
        self.progress = progress or (lambda f: None)

    def _ask(self, prompt: str) -> str:
        options = {"temperature": self.cfg.temperature, "num_ctx": self.cfg.num_ctx}
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
        return clean_output(self.client.chat(self.cfg.model, messages, options))

    def check(self) -> None:
        installed = self.client.list_models()
        if not model_available(self.cfg.model, installed):
            raise SummaryUnavailable(
                f"Modell {self.cfg.model!r} ist in Ollama nicht installiert. Bitte ausführen: ollama pull {self.cfg.model}"
            )

    def summarize(self, title: str, participants: list[str], transcript_lines: list[str]) -> str:
        self.check()
        chunks = chunk_lines(transcript_lines, self.cfg.max_chunk_chars)
        if len(chunks) <= 1:
            material_label, material = "Transkript", "\n".join(chunks[0] if chunks else [])
        else:
            notes = []
            for i, ch in enumerate(chunks, 1):
                self.progress((i - 1) / (len(chunks) + 1))
                notes.append(f"### Teil {i}\n" + self._ask(CHUNK_TEMPLATE.format(i=i, n=len(chunks), chunk="\n".join(ch))))
            material_label, material = "Notizen aus den Gesprächsteilen", "\n\n".join(notes)
        prompt = FINAL_TEMPLATE.format(
            title=title,
            participants=", ".join(participants) or "unbekannt",
            material_label=material_label,
            material=material,
        )
        result = self._ask(prompt)
        self.progress(1.0)
        if not result:
            raise SummaryUnavailable("Ollama hat eine leere Antwort geliefert.")
        return result


def render_summary_md(title: str, body: str, model: str, created: str) -> str:
    return (
        f"# Zusammenfassung: {title}\n\n"
        f"_Automatisch lokal erstellt mit {model} am {created}. Bitte vor Weitergabe prüfen._\n\n"
        f"{body}\n"
    )
