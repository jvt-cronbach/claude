"""Meeting minutes via a swappable local LLM provider (see :mod:`callrec.llm`)."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from callrec.config import SummaryConfig
from callrec.llm import (
    LlmProvider,
    LlmUnavailable,
    check_endpoint,
    make_provider,
    model_available,
    summary_model,
)
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


ProgressCb = Callable[..., None]  # progress(fraction 0..1, message="")
HEARTBEAT_S = 2.0


class LlmSummarizer:
    def __init__(
        self,
        cfg: SummaryConfig,
        provider: LlmProvider | None = None,
        prompts: PromptTemplates = DEFAULT_PROMPTS,
        progress: ProgressCb | None = None,
        heartbeat_s: float = HEARTBEAT_S,
    ) -> None:
        self.cfg = cfg
        self.provider = provider or make_provider(cfg)
        self.prompts = prompts
        self.model_name = summary_model(cfg)
        self.progress: ProgressCb = progress or (lambda f, m="": None)
        self.heartbeat_s = heartbeat_s
        self._frac = 0.0

    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "provider": self.provider.name,
            "endpoint": self.provider.base_url,
            "model": self.model_name,
            "prompt_fingerprint": self.prompts.fingerprint(),
        }
        extra = getattr(self.provider, "describe", None)
        if callable(extra):  # cloud provider: served model, token usage, what was sent
            info.update(extra())
        else:
            info.update({"temperature": self.cfg.temperature, "num_ctx": self.cfg.num_ctx})
        return info

    def _report(self, msg: str) -> None:
        try:
            self.progress(self._frac, msg)
        except TypeError:  # old-style progress(fraction) callback
            self.progress(self._frac)

    def _ask(self, prompt: str, label: str) -> str:
        """One model request with a heartbeat (elapsed time) and streamed progress if available."""
        options = {"temperature": self.cfg.temperature, "num_ctx": self.cfg.num_ctx}
        messages = [{"role": "system", "content": self.prompts.system}, {"role": "user", "content": prompt}]
        started = time.monotonic()
        state = {"detail": f"warte auf {self.provider.name} ({self.model_name})"}
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(self.heartbeat_s):
                self._report(f"{label}: {state['detail']} – {int(time.monotonic() - started)} s")

        ticker = threading.Thread(target=beat, name="summary-heartbeat", daemon=True)
        ticker.start()
        try:
            if getattr(self.provider, "streams_progress", False):
                def streamed(detail: str) -> None:
                    state["detail"] = detail

                text = self.provider.chat(self.model_name, messages, options, progress=streamed)  # type: ignore[call-arg]
            else:
                text = self.provider.chat(self.model_name, messages, options)
        finally:
            stop.set()
        log.info("%s: Antwort von %s nach %.1f s", label, self.provider.name, time.monotonic() - started)
        return clean_output(text)

    def check(self) -> None:
        if self.provider.name == "anthropic":
            return  # model/key problems are reported by the request itself (no extra round trip)
        installed = self.provider.list_models()
        if self.provider.name == "ollama" and not model_available(self.cfg.model, installed):
            raise LlmUnavailable(
                f"Modell {self.cfg.model!r} ist in Ollama nicht installiert. Bitte ausführen: ollama pull {self.cfg.model}"
            )
        if self.provider.name == "openai" and installed and self.cfg.model not in installed:
            raise LlmUnavailable(f"Modell {self.cfg.model!r} ist auf dem Server nicht geladen (verfügbar: {', '.join(installed)})")

    def max_chunk_chars(self) -> int:
        return int(getattr(self.provider, "max_chunk_chars", 0) or self.cfg.max_chunk_chars)

    def summarize(self, title: str, participants: list[str], transcript_lines: list[str], date: str = "") -> str:
        from callrec.llm_anthropic import ContextTooLong

        self.check()
        limit = self.max_chunk_chars()
        for _attempt in range(4):
            try:
                return self._summarize(title, participants, transcript_lines, date, limit)
            except ContextTooLong as exc:
                if limit <= 10000:
                    raise
                limit //= 2
                log.warning("%s - teile das Transkript feiner auf (max. %d Zeichen je Teil)", exc, limit)
        raise LlmUnavailable("Das Transkript ist zu lang für das Sprachmodell.")

    def _summarize(self, title: str, participants: list[str], transcript_lines: list[str], date: str, limit: int) -> str:
        chunks = chunk_lines(transcript_lines, limit)
        if len(chunks) <= 1:
            material_label, material = "Transkript", "\n".join(chunks[0] if chunks else [])
        else:
            notes = []
            for i, ch in enumerate(chunks, 1):
                self._frac = (i - 1) / (len(chunks) + 1)
                self._report(f"Teil {i}/{len(chunks)}")
                prompt = render_template(self.prompts.chunk, {"i": str(i), "n": str(len(chunks)), "chunk": "\n".join(ch)}, "chunk")
                notes.append(f"### Teil {i}\n" + self._ask(prompt, f"Teil {i}/{len(chunks)}"))
            material_label, material = "Notizen aus den Gesprächsteilen", "\n\n".join(notes)
            self._frac = len(chunks) / (len(chunks) + 1)
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
        result = self._ask(prompt, "Protokoll")
        self._frac = 1.0
        self.progress(1.0)
        if not result:
            raise LlmUnavailable("Das Sprachmodell hat eine leere Antwort geliefert.")
        return result


# Kept for compatibility with earlier code/tests
OllamaSummarizer = LlmSummarizer


def render_summary_md(title: str, body: str, model: str, created: str, local: bool = True, provider: str = "") -> str:
    if provider == "anthropic":
        note = (f"_Automatisch erstellt mit Claude ({model}) über die Anthropic-API am {created} – das Transkript "
                "wurde dafür an Anthropic übertragen. Bitte vor Weitergabe prüfen._")
    else:
        where = "lokal" if local else "auf einem entfernten, vom Nutzer freigegebenen Server"
        via = f" (Anbieter: {provider})" if provider else ""
        note = f"_Automatisch {where} erstellt mit {model}{via} am {created}. Bitte vor Weitergabe prüfen._"
    return f"# Zusammenfassung: {title}\n\n{note}\n\n{body}\n"


def missing_sections(body: str) -> list[str]:
    """Expected headings absent from a generated summary (tolerant: level, case, trailing text).

    Used to warn in the log and meta.json - a summary with a different structure is still saved.
    """
    from callrec.userfiles import SUMMARY_SECTIONS

    headings = [
        re.sub(r"[*_:]+", "", m.group(1)).strip().lower()
        for m in re.finditer(r"^\s{0,3}#{1,4}\s*(.+?)\s*#*\s*$", body, re.MULTILINE)
    ]
    missing = [s for s in SUMMARY_SECTIONS if not any(h.startswith(s.lower()) for h in headings)]
    if "Aufgaben" not in missing:
        m = re.search(r"^\s{0,3}#{1,4}\s*\**Aufgaben.*?$(.*?)(?=^\s{0,3}#{1,2}\s|\Z)", body, re.MULTILINE | re.DOTALL | re.IGNORECASE)
        part = m.group(1) if m else ""
        if not re.search(r"keine", part, re.IGNORECASE) and not (
            re.search(r"verantwortlich", part, re.IGNORECASE) and re.search(r"frist", part, re.IGNORECASE)
        ):
            missing.append("Aufgaben (Verantwortlich/Frist)")
    return missing
