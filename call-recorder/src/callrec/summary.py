"""Meeting minutes via a swappable local LLM provider (see :mod:`callrec.llm`)."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any, Protocol

from callrec.config import SummaryConfig
from callrec.llm import LlmProvider, LlmUnavailable, check_endpoint, make_provider, model_available
from callrec.userfiles import DEFAULT_PROMPTS, PromptTemplates, render_template

log = logging.getLogger(__name__)

# Backwards compatible name used throughout the pipeline
SummaryUnavailable = LlmUnavailable

__all__ = [
    "SummaryUnavailable", "Summarizer", "LlmSummarizer", "chunk_lines", "clean_output",
    "render_summary_md", "assert_local_url", "model_available",
]


class Summarizer(Protocol):
    model_name: str

    def summarize(self, title: str, participants: list[str], transcript_lines: list[str], date: str = "") -> str: ...


def assert_local_url(url: str) -> None:
    check_endpoint(url, allow_remote=False)


def chunk_lines(lines: list[str], max_chars: int) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if len(line) > max_chars:  # single huge line: hard split
            if current:
                chunks.append(current)
                current, size = [], 0
            for i in range(0, len(line), max_chars):
                chunks.append([line[i : i + max_chars]])
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


class LlmSummarizer:
    def __init__(
        self,
        cfg: SummaryConfig,
        provider: LlmProvider | None = None,
        prompts: PromptTemplates = DEFAULT_PROMPTS,
        progress: Callable[[float], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.provider = provider or make_provider(cfg)
        self.prompts = prompts
        self.model_name = cfg.model
        self.progress = progress or (lambda f: None)

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.provider.name,
            "endpoint": self.provider.base_url,
            "model": self.cfg.model,
            "prompt_fingerprint": self.prompts.fingerprint(),
            "temperature": self.cfg.temperature,
            "num_ctx": self.cfg.num_ctx,
        }

    def _ask(self, prompt: str) -> str:
        options = {"temperature": self.cfg.temperature, "num_ctx": self.cfg.num_ctx}
        messages = [{"role": "system", "content": self.prompts.system}, {"role": "user", "content": prompt}]
        return clean_output(self.provider.chat(self.cfg.model, messages, options))

    def check(self) -> None:
        installed = self.provider.list_models()
        if self.provider.name == "ollama" and not model_available(self.cfg.model, installed):
            raise LlmUnavailable(
                f"Modell {self.cfg.model!r} ist in Ollama nicht installiert. Bitte ausführen: ollama pull {self.cfg.model}"
            )
        if self.provider.name == "openai" and installed and self.cfg.model not in installed:
            raise LlmUnavailable(f"Modell {self.cfg.model!r} ist auf dem Server nicht geladen (verfügbar: {', '.join(installed)})")

    def summarize(self, title: str, participants: list[str], transcript_lines: list[str], date: str = "") -> str:
        self.check()
        chunks = chunk_lines(transcript_lines, self.cfg.max_chunk_chars)
        if len(chunks) <= 1:
            material_label, material = "Transkript", "\n".join(chunks[0] if chunks else [])
        else:
            notes = []
            for i, ch in enumerate(chunks, 1):
                self.progress((i - 1) / (len(chunks) + 1))
                prompt = render_template(self.prompts.chunk, {"i": str(i), "n": str(len(chunks)), "chunk": "\n".join(ch)}, "chunk")
                notes.append(f"### Teil {i}\n" + self._ask(prompt))
            material_label, material = "Notizen aus den Gesprächsteilen", "\n\n".join(notes)
        prompt = render_template(
            self.prompts.final,
            {
                "title": title,
                "date": date or "-",
                "participants": ", ".join(participants) or "unbekannt",
                "material_label": material_label,
                "material": material,
            },
            "material",
        )
        result = self._ask(prompt)
        self.progress(1.0)
        if not result:
            raise LlmUnavailable("Das Sprachmodell hat eine leere Antwort geliefert.")
        return result


# Kept for compatibility with earlier code/tests
OllamaSummarizer = LlmSummarizer


def render_summary_md(title: str, body: str, model: str, created: str) -> str:
    return (
        f"# Zusammenfassung: {title}\n\n"
        f"_Automatisch lokal erstellt mit {model} am {created}. Bitte vor Weitergabe prüfen._\n\n"
        f"{body}\n"
    )
