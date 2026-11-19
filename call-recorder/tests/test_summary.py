from __future__ import annotations

from typing import Any

import pytest
import requests

from callrec.config import SummaryConfig
from callrec.summary import (
    FINAL_TEMPLATE,
    SYSTEM_PROMPT,
    OllamaClient,
    OllamaSummarizer,
    SummaryUnavailable,
    assert_local_url,
    chunk_lines,
    clean_output,
    model_available,
    render_summary_md,
)


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434", "http://localhost:11434/", "http://[::1]:11434"])
def test_local_urls_ok(url):
    assert_local_url(url)
    OllamaClient(url, 5)


@pytest.mark.parametrize("url", ["http://192.168.1.10:11434", "https://ollama.example.com", "http://ollama:11434"])
def test_remote_urls_rejected(url):
    with pytest.raises(ValueError):
        OllamaClient(url, 5)


def test_session_ignores_system_proxy():
    c = OllamaClient("http://127.0.0.1:11434", 5)
    assert c.session.trust_env is False


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
    def __init__(self, status: int, data: Any) -> None:
        self.status_code, self._data, self.text = status, data, str(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeSession:
    def __init__(self, models=("gemma3:12b",), down=False):
        self.models = models
        self.down = down
        self.posts: list[dict[str, Any]] = []
        self.trust_env = False

    def get(self, url, timeout=None):
        if self.down:
            raise requests.ConnectionError("refused")
        assert url.endswith("/api/tags")
        return FakeResp(200, {"models": [{"name": m} for m in self.models]})

    def post(self, url, json=None, timeout=None):
        assert url == "http://127.0.0.1:11434/api/chat"
        self.posts.append(json)
        content = json["messages"][1]["content"]
        if content.startswith("Das folgende ist Teil"):
            return FakeResp(200, {"message": {"role": "assistant", "content": "- Notiz"}})
        return FakeResp(200, {"message": {"role": "assistant", "content": "## Kurzfassung\nAlles gut."}})


def _summarizer(session, **cfg) -> OllamaSummarizer:
    c = SummaryConfig(**cfg)
    return OllamaSummarizer(c, OllamaClient(c.ollama_url, 5, session=session))


def test_summarize_single_chunk_prompt():
    sess = FakeSession()
    out = _summarizer(sess).summarize("Jour fixe", ["Ich", "Anna"], ["[00:00:01] Anna: Wir machen das bis Freitag."])
    assert out == "## Kurzfassung\nAlles gut."
    payload = sess.posts[0]
    assert payload["model"] == "gemma3:12b" and payload["stream"] is False
    assert payload["options"]["num_ctx"] == 16384
    assert payload["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    user = payload["messages"][1]["content"]
    for section in ("## Kurzfassung", "## Entscheidungen", "## Aufgaben", "| Aufgabe | Verantwortlich | Frist |", "## Offene Punkte"):
        assert section in user
    assert "Wir machen das bis Freitag." in user and "Teilnehmende: Ich, Anna" in user


def test_summarize_map_reduce():
    sess = FakeSession()
    lines = [f"[00:{i:02d}:00] Anna: " + "Satz " * 20 for i in range(30)]
    _summarizer(sess, max_chunk_chars=500).summarize("Lang", ["Anna"], lines)
    assert len(sess.posts) > 2
    assert "Notizen aus den Gesprächsteilen" in sess.posts[-1]["messages"][1]["content"]


def test_skip_when_ollama_down_or_model_missing():
    with pytest.raises(SummaryUnavailable, match="nicht erreichbar"):
        _summarizer(FakeSession(down=True)).summarize("t", [], ["x"])
    with pytest.raises(SummaryUnavailable, match="ollama pull gemma3:12b"):
        _summarizer(FakeSession(models=("llama3:8b",))).summarize("t", [], ["x"])


def test_render():
    md = render_summary_md("Jour fixe", "## Kurzfassung\nX", "gemma3:12b", "19.11.2026 15:00")
    assert md.startswith("# Zusammenfassung: Jour fixe") and "gemma3:12b" in md
    assert "{material}" in FINAL_TEMPLATE
