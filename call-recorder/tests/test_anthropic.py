"""Claude (Anthropic API) summary provider - all requests go to a fake transport (no network)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

httpx2 = pytest.importorskip("httpx2")
pytest.importorskip("anthropic")

from callrec import apikey  # noqa: E402
from callrec.config import Config, SummaryConfig, apply_preset, config_from_dict, load_config, save_preset  # noqa: E402
from callrec.consent import (  # noqa: E402
    CONSENT_STATEMENT,
    CONSENT_STATEMENT_ANTHROPIC,
    ConsentError,
    cli_confirm,
    cloud_summary_covered,
    confirm_consent,
    later_confirmation,
    record_later_confirmation,
    require_consent,
)
from callrec.llm import make_provider  # noqa: E402
from callrec.llm_anthropic import (  # noqa: E402
    API_HOST,
    AnthropicError,
    AnthropicProvider,
    CloudNotConfirmed,
    _guard_request,
)
from callrec.summary import LlmSummarizer, missing_sections, render_summary_md  # noqa: E402
from callrec.userfiles import DEFAULT_PROMPTS  # noqa: E402

KEY = "sk-ant-api03-TESTKEY-0123456789abcdefghijklmnop"
GOOD_SUMMARY = (
    "## Kurzfassung\nRelease verschoben.\n\n## Entscheidungen\n- Release am 1.12.\n\n## Aufgaben\n"
    "| Aufgabe | Verantwortlich | Frist |\n|---|---|---|\n| Tests | Anna Beispiel | offen |\n\n"
    "## Projektstand und Updates\n### Portal\n- Login fertig\n\n## Offene Fragen\n- Keine\n\n## Nächste Termine\n- Keine genannt\n"
)


def sse(text_parts: list[str], stop_reason: str = "end_turn", model: str = "claude-opus-5-5",
        extra_blocks: list[dict[str, Any]] | None = None, stop_details: dict[str, Any] | None = None) -> bytes:
    events: list[tuple[str, dict[str, Any]]] = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1200, "output_tokens": 1}}}),
    ]
    idx = 0
    for block in extra_blocks or []:
        events.append(("content_block_start", {"type": "content_block_start", "index": idx, "content_block": block}))
        events.append(("content_block_stop", {"type": "content_block_stop", "index": idx}))
        idx += 1
    events.append(("content_block_start", {"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}}))
    for part in text_parts:
        events.append(("content_block_delta", {"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": part}}))
    events.append(("content_block_stop", {"type": "content_block_stop", "index": idx}))
    delta: dict[str, Any] = {"stop_reason": stop_reason, "stop_sequence": None}
    if stop_details is not None:
        delta["stop_details"] = stop_details
    events.append(("message_delta", {"type": "message_delta", "delta": delta, "usage": {"output_tokens": 345}}))
    events.append(("message_stop", {"type": "message_stop"}))
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


def error(status: int, etype: str, message: str, error_code: str | None = None, headers: dict[str, str] | None = None) -> Any:
    body: dict[str, Any] = {"type": "error", "error": {"type": etype, "message": message}}
    if error_code:
        body["error"]["details"] = {"error_code": error_code}
    return httpx2.Response(status, json=body, headers={"request-id": "req_x", **(headers or {})})


class FakeApi:
    """Records requests; ``responses`` is a list of httpx2.Response objects or callables."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        nxt = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if callable(nxt) and not isinstance(nxt, httpx2.Response):
            return nxt(request)
        return nxt

    def body(self, i: int = -1) -> dict[str, Any]:
        return dict(json.loads(self.requests[i].content))


def ok_stream(*parts: str, **kw: Any) -> Any:
    return httpx2.Response(200, content=sse(list(parts) or [GOOD_SUMMARY], **kw),
                           headers={"content-type": "text/event-stream"})


def provider(api: FakeApi, **kw: Any) -> AnthropicProvider:
    sleeps: list[float] = []
    p = AnthropicProvider(KEY, transport=httpx2.MockTransport(api), sdk_max_retries=0, sleep=sleeps.append, **kw)
    p.sleeps = sleeps  # type: ignore[attr-defined]
    return p


def test_summary_request_shape_and_only_transcript_text_is_sent():
    api = FakeApi(ok_stream("## Kurzfassung\n", "Release ", "verschoben."))
    p = provider(api)
    s = LlmSummarizer(SummaryConfig(provider="anthropic"), p, heartbeat_s=60)
    out = s.summarize("Jour fixe", ["Ich Selbst", "Anna Beispiel"],
                      ["[00:00:01] Anna Beispiel: Wir verschieben das Release."], date="19.11.2026 14:00")
    assert out.startswith("## Kurzfassung\nRelease verschoben.")
    req = api.requests[0]
    assert req.url.host == API_HOST and req.url.scheme == "https" and req.url.path == "/v1/messages"
    assert req.headers["authorization"] == f"Bearer {KEY}"  # anthropic 2.10 sends API keys as Bearer (SDK source)
    assert "server-side-fallback-2026-07-01" in req.headers.get("anthropic-beta", "")
    body = api.body()
    assert body["model"] == "claude-opus-5-5" and body["stream"] is True
    assert body["output_config"] == {"effort": "medium"} and body["fallbacks"] == "default"
    assert "temperature" not in body and "thinking" not in body and "top_p" not in body
    assert body["system"].startswith(DEFAULT_PROMPTS.system.rstrip()) and "Sprache des Protokolls: Deutsch" in body["system"]
    assert [m["role"] for m in body["messages"]] == ["user"]
    user = body["messages"][0]["content"]
    assert "Wir verschieben das Release." in user and "Teilnehmende: Ich Selbst, Anna Beispiel" in user
    # nothing else is sent: exactly these top-level fields
    assert set(body) <= {"model", "max_tokens", "messages", "system", "output_config", "fallbacks", "stream"}
    assert "@" not in json.dumps(body)  # no e-mail addresses
    info = s.describe()
    assert info["provider"] == "anthropic" and info["served_model"] == "claude-opus-5-5"
    assert info["usage"] == {"input_tokens": 1200, "output_tokens": 345, "requests": 1}


def test_streamed_progress_is_reported():
    api = FakeApi(ok_stream("a" * 10, "b" * 10))
    seen: list[str] = []
    text = provider(api).chat("claude-opus-5-5", [{"role": "user", "content": "x"}], {}, progress=seen.append)
    assert text == "a" * 10 + "b" * 10
    assert seen == ["Claude schreibt … 10 Zeichen empfangen", "Claude schreibt … 20 Zeichen empfangen"]


def test_heartbeat_while_waiting():
    import time

    class Slow:
        name, base_url, is_local = "ollama", "http://127.0.0.1:11434", True

        def list_models(self):
            return ["m"]

        def chat(self, model, messages, options):
            time.sleep(0.35)
            return GOOD_SUMMARY

    msgs: list[str] = []
    s = LlmSummarizer(SummaryConfig(model="m"), Slow(), heartbeat_s=0.1)  # type: ignore[arg-type]
    s.progress = lambda f, m="": msgs.append(m)
    s.summarize("t", ["A"], ["x"])
    assert any(m.startswith("Protokoll: warte auf ollama (m) – ") and m.endswith(" s") for m in msgs)


def test_haiku_has_no_fallback_and_effort_can_be_omitted():
    api = FakeApi(ok_stream("x"))
    provider(api, model="claude-haiku-5-5", effort="").chat("claude-haiku-5-5", [{"role": "user", "content": "x"}], {})
    body = api.body()
    assert "fallbacks" not in body and "output_config" not in body
    assert "anthropic-beta" not in api.requests[0].headers


@pytest.mark.parametrize("resp, match", [
    (lambda: error(401, "authentication_error", "invalid x-api-key"), "lehnt den API-Schlüssel ab"),
    (lambda: error(402, "billing_error", "payment required"), "Abrechnungsproblem"),
    (lambda: error(400, "invalid_request_error", "Your credit balance is too low to access the Anthropic API."), "Guthaben"),
    (lambda: error(404, "not_found_error", "model: claude-x"), "unbekannt oder für Ihr Konto nicht verfügbar"),
    (lambda: error(403, "permission_error", "region"), "darf diese Anfrage nicht"),
    (lambda: error(429, "rate_limit_error", "spend cap", "enforced_spend_limit_reached"), "Ausgabenlimit"),
])
def test_clear_german_errors_without_retry(resp, match):
    api = FakeApi(resp())
    p = provider(api)
    with pytest.raises(AnthropicError, match=match):
        p.chat("claude-opus-5-5", [{"role": "user", "content": "x"}], {})
    assert len(api.requests) == 1 and p.sleeps == []  # type: ignore[attr-defined]


def test_overload_and_rate_limit_are_retried_with_backoff():
    api = FakeApi(error(529, "overloaded_error", "Overloaded"),
                  error(429, "rate_limit_error", "slow down", headers={"retry-after": "7"}),
                  ok_stream("fertig"))
    p = provider(api)
    msgs: list[str] = []
    assert p.chat("claude-opus-5-5", [{"role": "user", "content": "x"}], {}, progress=msgs.append) == "fertig"
    assert len(api.requests) == 3
    assert p.sleeps[1] == 7.0 and 5.0 <= p.sleeps[0] <= 6.0  # type: ignore[attr-defined]
    assert any("neuer Versuch 2/4" in m for m in msgs)


def test_gives_up_after_max_attempts():
    api = FakeApi(error(529, "overloaded_error", "Overloaded"))
    with pytest.raises(AnthropicError, match="überlastet"):
        provider(api, max_attempts=3).chat("claude-opus-5-5", [{"role": "user", "content": "x"}], {})
    assert len(api.requests) == 3


def test_network_error_message():
    def boom(request):
        raise httpx2.ConnectError("no route", request=request)

    with pytest.raises(AnthropicError, match="Keine Verbindung zu api.anthropic.com"):
        provider(FakeApi(boom), max_attempts=2).chat("claude-opus-5-5", [{"role": "user", "content": "x"}], {})


def _stream_with_error(etype: str) -> Any:
    head = sse(["Teil"]).split(b"event: message_delta")[0]
    err = json.dumps({"type": "error", "error": {"type": etype, "message": "Overloaded"}})
    return httpx2.Response(200, content=head + f"event: error\ndata: {err}\n\n".encode(),
                           headers={"content-type": "text/event-stream"})


def test_mid_stream_overload_is_retried():
    """The API reports overload inside a 200 stream; the SDK raises a plain APIStatusError(200) for it."""
    api = FakeApi(_stream_with_error("overloaded_error"), ok_stream("fertig"))
    p = provider(api)
    assert p.chat("claude-opus-5-5", [{"role": "user", "content": "x"}], {}) == "fertig"
    assert len(api.requests) == 2 and len(p.sleeps) == 1  # type: ignore[attr-defined]
    with pytest.raises(AnthropicError, match="überlastet"):
        provider(FakeApi(_stream_with_error("overloaded_error")), max_attempts=2).chat(
            "claude-opus-5-5", [{"role": "user", "content": "x"}], {})
    api3 = FakeApi(_stream_with_error("invalid_request_error"))
    with pytest.raises(AnthropicError):
        provider(api3).chat("claude-opus-5-5", [{"role": "user", "content": "x"}], {})
    assert len(api3.requests) == 1


def test_connection_drop_during_stream_is_retried():
    class Broken(httpx2.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            yield sse(["Teil"]).split(b"event: content_block_stop")[0]
            raise httpx2.ReadError("connection reset")

    def broken(request: Any) -> Any:
        return httpx2.Response(200, stream=Broken(), headers={"content-type": "text/event-stream"})

    api = FakeApi(broken, ok_stream("fertig"))
    assert provider(api).chat("claude-opus-5-5", [{"role": "user", "content": "x"}], {}) == "fertig"
    assert len(api.requests) == 2
    with pytest.raises(AnthropicError, match="unterbrochen"):
        provider(FakeApi(broken), max_attempts=1).chat("claude-opus-5-5", [{"role": "user", "content": "x"}], {})


def test_sdk_debug_logging_cannot_put_transcripts_into_the_log():
    from callrec.llm_anthropic import _sdk

    logging.getLogger("anthropic").setLevel(logging.DEBUG)  # what ANTHROPIC_LOG=debug does on SDK import
    logging.getLogger("httpx2").setLevel(logging.DEBUG)
    _sdk()
    assert logging.getLogger("anthropic").level >= logging.INFO
    assert logging.getLogger("httpx2").level >= logging.INFO


def test_fallback_detected_from_usage_iterations():
    body = sse(["ok"]).replace(b'"usage": {"output_tokens": 345}',
                               b'"usage": {"output_tokens": 345, "iterations": [{"type": "fallback_message", '
                               b'"input_tokens": 1, "output_tokens": 2}]}')
    api = FakeApi(httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"}))
    p = provider(api)
    assert p.chat("claude-opus-5-5", [{"role": "user", "content": "x"}], {}) == "ok"
    assert p.used_fallback


def test_missing_key():
    with pytest.raises(AnthropicError, match="Kein Anthropic-API-Schlüssel"):
        AnthropicProvider("").chat("claude-opus-5-5", [{"role": "user", "content": "x"}], {})


def test_fallback_beta_unavailable_retries_without_it():
    api = FakeApi(error(400, "invalid_request_error", "Unexpected value(s) `server-side-fallback-2026-07-01` for the `anthropic-beta` header."),
                  ok_stream("ok"))
    assert provider(api).chat("claude-opus-5-5", [{"role": "user", "content": "x"}], {}) == "ok"
    assert "fallbacks" in api.body(0) and "fallbacks" not in api.body(1)


def test_refusal_and_truncation():
    api = FakeApi(ok_stream("", stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "x"}))
    with pytest.raises(AnthropicError, match="abgelehnt.*cyber"):
        provider(api).chat("claude-opus-5-5", [{"role": "user", "content": "x"}], {})
    api2 = FakeApi(ok_stream("Teil", stop_reason="max_tokens"))
    assert "abgeschnitten" in provider(api2).chat("claude-opus-5-5", [{"role": "user", "content": "x"}], {})


def test_redirects_are_not_followed():
    api = FakeApi(httpx2.Response(307, headers={"location": "https://evil.example/v1/messages"}))
    with pytest.raises(AnthropicError):
        provider(api, max_attempts=1).chat("claude-opus-5-5", [{"role": "user", "content": "x"}], {})
    assert all(r.url.host == API_HOST for r in api.requests)


def test_only_official_host_and_env_base_url_ignored(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example")
    api = FakeApi(ok_stream("ok"))
    provider(api).chat("claude-opus-5-5", [{"role": "user", "content": "x"}], {})
    assert api.requests[0].url.host == API_HOST
    from callrec.llm import RemoteEndpointRefused

    with pytest.raises(RemoteEndpointRefused):
        _guard_request(httpx2.Request("POST", "https://evil.example/v1/messages"))
    with pytest.raises(RemoteEndpointRefused):
        _guard_request(httpx2.Request("POST", "http://api.anthropic.com/v1/messages"))


def test_long_transcript_is_split_and_context_errors_shrink_chunks():
    calls: list[int] = []

    def respond(request):
        n = len(json.loads(request.content)["messages"][0]["content"])
        calls.append(n)
        if n > 60000:
            return error(400, "invalid_request_error", "prompt is too long: 1200000 tokens > 1000000 maximum")
        return ok_stream("- Notiz" if "Teil" in json.loads(request.content)["messages"][0]["content"][:40] else GOOD_SUMMARY)

    p = provider(FakeApi(respond), max_chunk_chars=100000)
    lines = [f"[00:{i // 60:02d}:{i % 60:02d}] Anna: " + "Wort " * 30 for i in range(600)]  # ~100k chars
    out = LlmSummarizer(SummaryConfig(provider="anthropic"), p, heartbeat_s=60).summarize("Lang", ["Anna"], lines)
    assert out.startswith("## Kurzfassung") and calls[0] > 60000 and len(calls) > 2
    assert max(calls[1:]) <= 60000


def test_test_connection():
    def respond(request):
        if request.url.path.startswith("/v1/models/"):
            return httpx2.Response(200, json={"type": "model", "id": "claude-opus-5-5", "display_name": "Claude Opus 5.5",
                                              "created_at": "2026-01-01T00:00:00Z"})
        assert json.loads(request.content)["messages"][0]["content"] == "Antworte nur mit: OK"
        return httpx2.Response(200, json={"id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                                          "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn",
                                          "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 3}})

    assert "Verbindung zu Anthropic OK: Modell Claude Opus 5.5" in provider(FakeApi(respond)).test_connection()
    with pytest.raises(AnthropicError, match="API-Schlüssel ab"):
        provider(FakeApi(error(401, "authentication_error", "bad"))).test_connection()


# --- opt-in, presets, key storage -----------------------------------------------------------
def test_strict_opt_in():
    cfg = SummaryConfig(provider="anthropic")
    with pytest.raises(CloudNotConfirmed, match="nicht bestätigt"):
        make_provider(cfg, anthropic_key=(KEY, "test"))
    cfg.anthropic_transcripts_leave_this_computer_confirmed = True
    p = make_provider(cfg, anthropic_key=(KEY, "test"))
    assert p.name == "anthropic" and p.base_url == "https://api.anthropic.com" and not p.is_local  # type: ignore[attr-defined]
    # the local providers keep their loopback-only guarantee untouched
    from callrec.llm import RemoteEndpointRefused

    with pytest.raises(RemoteEndpointRefused):
        make_provider(SummaryConfig(provider="ollama", ollama_url="https://api.anthropic.com"))


def test_preset_can_never_enable_or_select_anthropic(isolated_home: Path):
    base = config_from_dict({"summary": {"provider": "ollama"}})
    out = apply_preset(base, {"summary": {"provider": "anthropic", "anthropic_transcripts_leave_this_computer_confirmed": True,
                                          "anthropic_model": "claude-sonnet-5-5"}}, "Cloud")
    assert out.summary.provider == "ollama" and not out.summary.anthropic_transcripts_leave_this_computer_confirmed
    assert out.summary.anthropic_model == "claude-sonnet-5-5"  # model tuning is fine
    assert any("summary.provider = 'anthropic'" in w for w in out.warnings)
    enabled = config_from_dict({"summary": {"provider": "anthropic", "anthropic_transcripts_leave_this_computer_confirmed": True}})
    path = save_preset(enabled, "Export")
    text = path.read_text(encoding="utf-8")
    assert "anthropic_transcripts_leave" not in text and 'provider = "anthropic"' not in text


def test_api_key_storage_and_env(isolated_home: Path, caplog):
    assert apikey.load_key(environ={}) == ("", "")
    assert apikey.load_key(environ={"ANTHROPIC_API_KEY": KEY}) == (KEY, apikey.SOURCE_ENV)
    with caplog.at_level(logging.INFO):
        apikey.store_key("  " + KEY + "\n")
    assert apikey.load_key(environ={"ANTHROPIC_API_KEY": "sk-ant-other-xxxxxxxxxxxxxxxxxxxx"}) == (KEY, apikey.SOURCE_STORED)
    assert KEY not in caplog.text and apikey.mask(KEY) in caplog.text
    cfg_text = (isolated_home / "config").exists() and "".join(p.read_text() for p in (isolated_home / "config").rglob("*.toml"))
    assert not cfg_text or KEY not in cfg_text
    with pytest.raises(apikey.ApiKeyError):
        apikey.store_key("kein schlüssel")
    with pytest.raises(apikey.ApiKeyError, match="Admin"):
        apikey.store_key("sk-ant-admin01-xxxxxxxxxxxxxxxxxxxxxxxx")
    assert apikey.delete_key() and apikey.load_key(environ={}) == ("", "")


def test_log_redacts_keys(tmp_path: Path):
    from callrec.logging_setup import RedactingFilter

    rec = logging.LogRecord("x", logging.ERROR, "f", 1, "Fehler mit %s", (KEY,), None)
    RedactingFilter().filter(rec)
    assert KEY not in rec.getMessage() and "sk-ant-***" in rec.getMessage()


def test_config_key_never_in_config_file(isolated_home: Path):
    cfg = load_config()
    assert not any("api_key" in k and "anthropic" in k for k in vars(cfg.summary))


# --- consent ---------------------------------------------------------------------------------
def test_consent_text_names_anthropic():
    rec = confirm_consent(user_checked_box=True, confirmed_by="Ich", participants=[], method="dialog", summary_provider="anthropic")
    assert rec.statement == CONSENT_STATEMENT_ANTHROPIC and "Anthropic" in rec.statement
    d = rec.to_dict()
    assert d["summary_provider"] == "anthropic" and "api.anthropic.com" in d["transcript_sent_to"]
    require_consent(rec, summary_provider="anthropic")
    local = confirm_consent(user_checked_box=True, confirmed_by="Ich", participants=[], method="dialog", summary_provider="ollama")
    assert local.statement == CONSENT_STATEMENT and local.to_dict()["summary_provider"] == "ollama"
    with pytest.raises(ConsentError, match="Anthropic"):
        require_consent(local, summary_provider="anthropic")
    out: list[str] = []
    cli_rec = cli_confirm(lambda p: "ja", out.append, "Ich", [], summary_provider="anthropic")
    assert CONSENT_STATEMENT_ANTHROPIC in out and any("verlässt diesen Rechner" in line for line in out)
    assert cli_rec.summary_provider == "anthropic"


def test_controller_refuses_local_consent_when_anthropic_active(tmp_path: Path):
    from callrec.controller import StartDecision
    from tests.test_controller import FakeUi, _controller

    def decision(provider):
        def make(sugg):
            c = confirm_consent(user_checked_box=True, confirmed_by="Ich", participants=[], method="dialog",
                                summary_provider=provider)
            return StartDecision("Test", [], None, c)
        return make

    for provider_name, expected in (("ollama", False), ("anthropic", True)):
        ui = FakeUi(decision_factory=decision(provider_name))
        ctl, _q = _controller(ui, tmp_path / provider_name)
        ctl.cfg.summary.provider = "anthropic"
        ctl.cfg.summary.anthropic_transcripts_leave_this_computer_confirmed = True
        assert ctl.start() is expected
        if expected:
            assert ctl.current.load_meta()["consent"]["summary_provider"] == "anthropic"  # type: ignore[union-attr]
            ctl.stop()
        else:
            assert any("Anthropic" in m for _t, m in ui.notes)


def test_older_recordings_need_a_separate_confirmation(tmp_path: Path):
    from tests.conftest import make_recording

    rec = make_recording(tmp_path)
    assert cloud_summary_covered(rec.load_meta(), "ollama")
    assert not cloud_summary_covered(rec.load_meta(), "anthropic")
    with pytest.raises(ConsentError):
        later_confirmation("Ich", "dialog", False)
    record_later_confirmation(rec, later_confirmation("Ich", "cli", True))
    meta = rec.load_meta()
    assert cloud_summary_covered(meta, "anthropic")
    assert meta["consent"]["statement"] == CONSENT_STATEMENT  # original record untouched
    assert meta["consent"]["additional_confirmations"][0]["provider"] == "anthropic"


def test_summary_md_and_section_check():
    md = render_summary_md("Jour fixe", GOOD_SUMMARY, "claude-opus-5-5", "19.11.2026 15:00", local=False, provider="anthropic")
    assert "Claude (claude-opus-5-5) über die Anthropic-API" in md and "an Anthropic übertragen" in md
    assert "(Anbieter: ollama)" in render_summary_md("t", "b", "gemma3:12b", "x", provider="ollama")
    assert missing_sections(GOOD_SUMMARY) == []
    assert missing_sections("# Kurzfassung\nx\n### **Entscheidungen:**\n- y") == [
        "Aufgaben", "Projektstand und Updates", "Offene Fragen", "Nächste Termine"]
    bad_tasks = GOOD_SUMMARY.replace("| Aufgabe | Verantwortlich | Frist |\n|---|---|---|\n| Tests | Anna Beispiel | offen |", "- Tests machen")
    assert missing_sections(bad_tasks) == ["Aufgaben (Verantwortlich/Frist)"]
    assert missing_sections(GOOD_SUMMARY.replace("| Tests | Anna Beispiel | offen |", "")) == []


def test_pipeline_records_provider_and_skips_uncovered_recordings(tmp_path: Path):
    from callrec.audio.finalize import load_mono
    from callrec.pipeline import Components, Pipeline
    from callrec.speakers.profiles import PlainFilePersistence, ProfileStore
    from tests.test_pipeline import FakeDiarizer, FakeEmbedder, FakeTranscriber, recording_with_voices

    cfg = Config()
    cfg.summary.provider = "anthropic"
    cfg.summary.anthropic_transcripts_leave_this_computer_confirmed = True
    api = FakeApi(ok_stream(GOOD_SUMMARY.replace("## Nächste Termine\n- Keine genannt\n", "")))
    comps = Components(
        transcriber_factory=FakeTranscriber, diarizer_factory=FakeDiarizer, embedder_factory=FakeEmbedder,
        profiles=ProfileStore(PlainFilePersistence(tmp_path / "vp.json")),
        summarizer_factory=lambda: LlmSummarizer(cfg.summary, provider(api), heartbeat_s=60),
        audio_loader=load_mono,
    )
    pipeline = Pipeline(cfg, comps)
    rec = recording_with_voices(tmp_path / "r", 330, 550)
    res = pipeline.run(rec)
    assert "Einwilligung dieser Aufnahme erwähnt die Übertragung an Anthropic nicht" in res.summary_skipped
    assert api.requests == []  # nothing was sent
    record_later_confirmation(rec, later_confirmation("Ich", "dialog", True))
    res = pipeline.run(rec, {"summarize"})
    assert not res.summary_skipped and len(api.requests) == 1
    meta = rec.load_meta()["processing"]["summary"]
    assert meta["provider"] == "anthropic" and meta["model"] == "claude-opus-5-5" and meta["served_model"] == "claude-opus-5-5"
    assert meta["usage"]["output_tokens"] == 345 and meta["missing_sections"] == ["Nächste Termine"]
    assert any("ohne erwartete Abschnitte: Nächste Termine" in m for m in res.messages)
    assert "über die Anthropic-API" in rec.summary_path.read_text(encoding="utf-8")
    body = json.dumps(api.body(), ensure_ascii=False)
    assert "olga@firma.de" not in body and "anna@firma.de" not in body and "Hallo zusammen." in body


def test_cli_anthropic_commands(isolated_home: Path, capsys, monkeypatch):
    import io

    from callrec.cli import main

    monkeypatch.setattr("sys.stdin", io.StringIO(KEY + "\n"))
    assert main(["anthropic", "set-key"]) == 0
    assert apikey.load_key(environ={})[0] == KEY
    assert main(["anthropic", "status"]) == 0
    out = capsys.readouterr().out
    assert apikey.mask(KEY) in out and KEY not in out and "bestätigt: nein" in out
    monkeypatch.setattr("sys.stdin", io.StringIO("ja\n"))  # not a terminal: refused
    assert main(["anthropic", "enable"]) == 1
    assert load_config(preset="").summary.provider == "ollama"

    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", Tty("ja\n"))
    monkeypatch.setattr("builtins.input", lambda prompt="": "ja")
    assert main(["anthropic", "enable", "--model", "claude-sonnet-5-5"]) == 0
    cfg = load_config(preset="")
    assert cfg.summary.provider == "anthropic" and cfg.summary.anthropic_transcripts_leave_this_computer_confirmed
    assert cfg.summary.anthropic_model == "claude-sonnet-5-5"
    from callrec import paths

    assert KEY not in paths.config_file().read_text(encoding="utf-8")
    assert main(["anthropic", "disable"]) == 0
    cfg = load_config(preset="")
    assert cfg.summary.provider == "ollama" and not cfg.summary.anthropic_transcripts_leave_this_computer_confirmed
    assert main(["anthropic", "delete-key"]) == 0 and apikey.load_key(environ={}) == ("", "")


def test_cli_summarize_old_recording_requires_confirmation(isolated_home: Path, tmp_path: Path, capsys, monkeypatch):
    from callrec.cli import _cloud_consent_ok
    from tests.conftest import make_recording

    rec = make_recording(tmp_path)
    cfg = Config()
    cfg.summary.provider = "anthropic"
    assert _cloud_consent_ok(cfg, rec, confirm=False) is False
    assert "--confirm-anthropic" in capsys.readouterr().out

    class Tty:
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", Tty())
    monkeypatch.setattr("builtins.input", lambda prompt="": "nein")
    assert _cloud_consent_ok(cfg, rec, confirm=True) is False
    monkeypatch.setattr("builtins.input", lambda prompt="": "ja")
    assert _cloud_consent_ok(cfg, rec, confirm=True) is True
    assert cloud_summary_covered(rec.load_meta(), "anthropic")
    cfg.summary.provider = "ollama"
    assert _cloud_consent_ok(cfg, rec, confirm=False) is True
