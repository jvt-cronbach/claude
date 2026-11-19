from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from callrec.config import SummaryConfig
from callrec.llm import (
    LlmUnavailable,
    OllamaProvider,
    OpenAICompatProvider,
    RemoteEndpointRefused,
    check_endpoint,
    make_provider,
    model_available,
)
from callrec.summary import LlmSummarizer, chunk_lines, clean_output, render_summary_md
from callrec.userfiles import DEFAULT_PROMPTS, PromptTemplates


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434", "http://localhost:11434/", "http://[::1]:11434", "http://127.0.0.2:8080/v1"])
def test_local_urls_ok(url):
    assert check_endpoint(url, allow_remote=False) is True
    OllamaProvider(url)
    OpenAICompatProvider(url)


@pytest.mark.parametrize("url", ["http://192.168.1.10:11434", "https://ollama.example.com", "http://ollama:11434", "ftp://127.0.0.1"])
def test_remote_urls_rejected_without_override(url):
    with pytest.raises(RemoteEndpointRefused):
        OllamaProvider(url)
    with pytest.raises(RemoteEndpointRefused):
        OpenAICompatProvider(url)


def test_remote_override_is_explicit_and_logged(caplog):
    p = OpenAICompatProvider("http://192.168.1.10:1234/v1", allow_remote=True)
    assert p.is_local is False
    assert "verlassen diesen Rechner" in caplog.text
    cfg = SummaryConfig(provider="openai", openai_url="http://10.0.0.5:9931/v1")
    with pytest.raises(RemoteEndpointRefused):
        make_provider(cfg)
    cfg.allow_remote_endpoint_data_leaves_this_computer = True
    assert make_provider(cfg).name == "openai"


def test_sessions_ignore_system_proxy():
    assert OllamaProvider("http://127.0.0.1:11434").session.trust_env is False


def test_chunking():
    lines = [f"[00:00:{i:02d}] Anna: " + "x" * 50 for i in range(10)]
    chunks = chunk_lines(lines, 200)
    assert all(sum(len(line) + 1 for line in ch) <= 200 for ch in chunks)
    assert [line for ch in chunks for line in ch] == lines
    assert chunk_lines(["y" * 450], 200) == [["y" * 200], ["y" * 200], ["y" * 50]]
    assert chunk_lines([], 100) == []


def test_clean_output():
    assert clean_output("<think>hmm</think>\n```markdown\n## Kurzfassung\nText\n```") == "## Kurzfassung\nText"


def test_model_available():
    assert model_available("gemma3:12b", ["gemma3:12b", "x"])
    assert model_available("gemma3", ["gemma3:latest"])
    assert not model_available("gemma3:27b", ["gemma3:12b"])


class FakeResp:
    def __init__(self, status: int, data: Any = None, lines: list[dict[str, Any]] | None = None) -> None:
        self.status_code, self._data, self.text = status, data, json.dumps(data)
        self._lines = lines or []

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def iter_lines(self):
        for obj in self._lines:
            yield json.dumps(obj).encode()


class FakeOllama:
    """Mimics the documented Ollama API (docs/api.md)."""

    def __init__(self, models=("gemma3:12b",), down=False, pull_lines=None):
        self.models, self.down = list(models), down
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.pull_lines = pull_lines
        self.trust_env = False

    def get(self, url, timeout=None, headers=None):
        if self.down:
            raise requests.ConnectionError("refused")
        if url.endswith("/api/tags"):
            return FakeResp(200, {"models": [{"name": m, "model": m} for m in self.models]})
        if url.endswith("/api/version"):
            return FakeResp(200, {"version": "0.13.1"})
        raise AssertionError(url)

    def post(self, url, json=None, timeout=None, stream=False, headers=None):
        self.posts.append((url, json))
        if url.endswith("/api/pull"):
            assert json == {"model": "gemma3:4b", "stream": True} and stream
            return FakeResp(200, lines=self.pull_lines)
        assert url == "http://127.0.0.1:11434/api/chat"
        if json["model"] not in self.models:
            return FakeResp(404, {"error": f"model '{json['model']}' not found"})
        content = json["messages"][1]["content"]
        text = "- Notiz" if content.startswith("Das folgende ist Teil") else "## Kurzfassung\nAlles gut."
        return FakeResp(200, {"model": json["model"], "message": {"role": "assistant", "content": text}, "done": True})


def _summarizer(session, prompts: PromptTemplates = DEFAULT_PROMPTS, **cfg) -> LlmSummarizer:
    c = SummaryConfig(**cfg)
    return LlmSummarizer(c, OllamaProvider(c.ollama_url, 5, session=session), prompts=prompts)


def test_summarize_single_chunk_prompt():
    sess = FakeOllama()
    out = _summarizer(sess).summarize("Jour fixe", ["Ich", "Anna"], ["[00:00:01] Anna: Wir machen das bis Freitag."], date="19.11.2026 14:00")
    assert out == "## Kurzfassung\nAlles gut."
    _, payload = sess.posts[0]
    assert payload["model"] == "gemma3:12b" and payload["stream"] is False
    assert payload["options"] == {"temperature": 0.2, "num_ctx": 16384}
    assert payload["messages"][0] == {"role": "system", "content": DEFAULT_PROMPTS.system}
    user = payload["messages"][1]["content"]
    for section in ("## Kurzfassung", "## Entscheidungen", "## Aufgaben", "| Aufgabe | Verantwortlich | Frist |", "## Offene Punkte"):
        assert section in user
    assert "Wir machen das bis Freitag." in user and "Teilnehmende: Ich, Anna" in user and "Datum: 19.11.2026 14:00" in user


def test_custom_template_and_missing_placeholder():
    sess = FakeOllama()
    prompts = PromptTemplates("SYS {title}", "Nur Stichpunkte für {title} ({participants}). {unbekannt}", "{chunk}")
    _summarizer(sess, prompts).summarize("Daily", ["Anna"], ["[00:00:01] Anna: Hallo"])
    user = sess.posts[0][1]["messages"][1]["content"]
    assert user.startswith("Nur Stichpunkte für Daily (Anna). {unbekannt}")  # unknown placeholders stay
    assert user.rstrip().endswith("[00:00:01] Anna: Hallo")  # material appended because {material} missing
    assert sess.posts[0][1]["messages"][0]["content"] == "SYS {title}"  # system prompt is not templated


def test_summarize_map_reduce():
    sess = FakeOllama()
    lines = [f"[00:{i:02d}:00] Anna: " + "Satz " * 20 for i in range(30)]
    _summarizer(sess, max_chunk_chars=500).summarize("Lang", ["Anna"], lines)
    assert len(sess.posts) > 2
    assert "Notizen aus den Gesprächsteilen" in sess.posts[-1][1]["messages"][1]["content"]


def test_skip_when_ollama_down_or_model_missing():
    with pytest.raises(LlmUnavailable, match="nicht erreichbar"):
        _summarizer(FakeOllama(down=True)).summarize("t", [], ["x"])
    with pytest.raises(LlmUnavailable, match="ollama pull gemma3:12b"):
        _summarizer(FakeOllama(models=("llama3:8b",))).summarize("t", [], ["x"])
    p = OllamaProvider("http://127.0.0.1:11434", session=FakeOllama(models=()))
    with pytest.raises(LlmUnavailable, match="nicht installiert"):
        p.chat("gemma3:12b", [{"role": "user", "content": "x"}], {})


def test_ollama_version_and_pull_progress():
    lines = [
        {"status": "pulling manifest"},
        {"status": "pulling abc", "digest": "abc", "total": 200},
        {"status": "pulling abc", "digest": "abc", "total": 200, "completed": 100},
        {"status": "verifying sha256 digest"},
        {"status": "success"},
    ]
    p = OllamaProvider("http://127.0.0.1:11434", session=FakeOllama(pull_lines=lines))
    assert p.version() == "0.13.1" and p.can_pull
    seen: list[tuple[str, float | None]] = []
    p.pull("gemma3:4b", lambda s, f: seen.append((s, f)))
    assert seen[0] == ("pulling manifest", None) and seen[1] == ("pulling abc", None)
    assert ("pulling abc", 0.5) in seen and seen[-1][0] == "success"
    bad = OllamaProvider("http://127.0.0.1:11434", session=FakeOllama(pull_lines=[{"status": "pulling manifest"}, {"error": "pull model manifest: file does not exist"}]))
    with pytest.raises(LlmUnavailable, match="file does not exist"):
        bad.pull("gemma3:4b", lambda s, f: None)
    incomplete = OllamaProvider("http://127.0.0.1:11434", session=FakeOllama(pull_lines=[{"status": "pulling manifest"}]))
    with pytest.raises(LlmUnavailable, match="nicht abgeschlossen"):
        incomplete.pull("gemma3:4b", lambda s, f: None)


class FakeOpenAI:
    """Mimics /v1/models and /v1/chat/completions (OpenAI-compatible, llama.cpp/LM Studio)."""

    def __init__(self):
        self.posts = []
        self.trust_env = False

    def get(self, url, headers=None, timeout=None):
        assert url == "http://127.0.0.1:1234/v1/models" and headers["Authorization"].startswith("Bearer ")
        return FakeResp(200, {"object": "list", "data": [{"id": "qwen2.5-7b-instruct", "object": "model"}]})

    def post(self, url, json=None, headers=None, timeout=None):
        assert url == "http://127.0.0.1:1234/v1/chat/completions"
        self.posts.append(json)
        return FakeResp(200, {"choices": [{"index": 0, "message": {"role": "assistant", "content": "## Kurzfassung\nOK"}, "finish_reason": "stop"}]})


def test_openai_compatible_provider():
    sess = FakeOpenAI()
    cfg = SummaryConfig(provider="openai", model="qwen2.5-7b-instruct")
    s = LlmSummarizer(cfg, make_provider(cfg, session=sess))
    assert s.provider.list_models() == ["qwen2.5-7b-instruct"]
    assert s.provider.version() is None and not s.provider.can_pull
    assert s.summarize("t", ["a"], ["[00:00:00] a: x"]) == "## Kurzfassung\nOK"
    assert sess.posts[0]["temperature"] == 0.2 and sess.posts[0]["stream"] is False and "options" not in sess.posts[0]
    assert s.describe()["provider"] == "openai"
    cfg2 = SummaryConfig(provider="openai", model="anderes-modell")
    with pytest.raises(LlmUnavailable, match="nicht geladen"):
        LlmSummarizer(cfg2, make_provider(cfg2, session=FakeOpenAI())).summarize("t", [], ["x"])


def test_render():
    md = render_summary_md("Jour fixe", "## Kurzfassung\nX", "gemma3:12b", "19.11.2026 15:00")
    assert md.startswith("# Zusammenfassung: Jour fixe") and "gemma3:12b" in md
