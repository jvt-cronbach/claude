"""The web UI's Python side without a window: app loop, dialog broker, UiPort (consent in both modes,
call-detected prompt), bridge (validation, exposure), settings model, loopback server, Markdown blocks,
plus the whole flow start -> record -> stop -> question -> chain with a simulated page."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from callrec.calendar.service import CalendarService, Suggestion
from callrec.config import Config, ConfigError, load_config, save_config
from callrec.consent import CONSENT_STATEMENT, ConsentError, reminder_statement, require_consent
from callrec.detect import Prompt
from callrec.jobs import ProcessingQueue
from callrec.models import Person
from callrec.pipeline import ProcessResult
from callrec.ui.apploop import AppLoop
from callrec.ui.webdialogs import DialogBroker
from callrec.ui.webui import Events, WebUi
from tests.conftest import make_event
from tests.test_controller import FakeBackend
from tests.test_mainwindow_logic import processed

ME = Person("Ich Selbst", "ich@firma.de", "self")


# ---------------------------------------------------------------------------
# app loop + broker
# ---------------------------------------------------------------------------
@pytest.fixture
def loop():
    lp = AppLoop("test-app")
    lp.start()
    yield lp
    lp.stop()
    lp.join(2)


def test_apploop_post_call_after_order_and_errors(loop: AppLoop):
    out: list[Any] = []
    loop.post(lambda: out.append(1))
    loop.after(80, lambda: out.append(3))
    loop.post(lambda: out.append(2))
    loop.post(lambda: 1 / 0)  # logged, loop keeps running
    assert loop.call(lambda: threading.current_thread().name) == "test-app"
    time.sleep(0.2)
    assert out == [1, 2, 3]
    with pytest.raises(ZeroDivisionError):
        loop.call(lambda: 1 / 0)


def test_apploop_wait_pumps_other_calls_on_the_app_thread(loop: AppLoop):
    fut: Future[str] = Future()
    seen: list[str] = []

    def modal() -> str:  # like a dialog: waits on the app thread ...
        loop.post(lambda: seen.append("ran while waiting"))  # ... while other posts keep running
        threading.Timer(0.1, lambda: fut.set_result("answer")).start()
        return loop.wait(fut)

    assert loop.call(modal) == "answer"
    assert seen == ["ran while waiting"]
    with pytest.raises(TimeoutError):
        loop.call(lambda: loop.wait(Future(), timeout=0.1))


def test_broker_ask_answer_reload_and_cancel(loop: AppLoop):
    events = Events()
    events.keep_sent = True
    broker = DialogBroker(loop, events.emit)

    def page() -> None:  # the page answers from a js_api thread
        for _ in range(100):
            sent = [s for s in events.sent if s[1] == "dialog"]
            if sent:
                did = sent[-1][2]["id"]
                assert broker.open_dialogs()[0]["kind"] == "confirm"  # shown again after a reload
                assert broker.answer(did, {"ok": True})
                return
            time.sleep(0.02)

    threading.Thread(target=page).start()
    assert loop.call(lambda: broker.ask("confirm", {"title": "x"})) == {"ok": True}
    assert broker.open_dialogs() == [] and any(s[1] == "dialog_close" for s in events.sent)
    assert broker.answer("unknown", {}) is False
    # app exit: open questions end without an answer (None), nothing hangs
    threading.Timer(0.1, broker.cancel_all).start()
    assert loop.call(lambda: broker.ask("info", {})) is None


# ---------------------------------------------------------------------------
# UiPort: start question in both consent modes, call-detected prompt
# ---------------------------------------------------------------------------
class Page:
    """Answers dialogs like the page would (thread, via broker.answer)."""

    def __init__(self, ui: WebUi, answers: dict[str, Any]) -> None:
        self.ui, self.answers, self.seen = ui, answers, []
        self.stop = threading.Event()
        threading.Thread(target=self.run, daemon=True).start()

    def run(self) -> None:
        done: set[str] = set()
        while not self.stop.is_set():
            for _target, name, payload in list(self.ui.events.sent):
                if name == "dialog" and payload["id"] not in done:
                    done.add(payload["id"])
                    self.seen.append(payload)
                    ans = self.answers.get(payload["kind"])
                    if isinstance(ans, list):
                        ans = ans.pop(0) if ans else None
                    res = ans(payload) if callable(ans) else ans
                    self.ui.broker.answer(payload["id"], res)
            time.sleep(0.01)


def make_ui(loop: AppLoop, tmp_path: Path, mode: str = "ask", provider: str = "ollama") -> WebUi:
    events = Events()
    events.keep_sent = True
    cal = CalendarService(None, tmp_path / "cal.json", timedelta(minutes=10), timedelta(minutes=10))
    ui = WebUi(loop, events, cal)
    ui.consent_mode = lambda: mode
    ui.summary_provider = lambda: provider
    ui.me_provider = lambda: ME
    return ui


@pytest.mark.parametrize("provider", ["ollama", "anthropic"])
def test_ask_mode_one_click_starts_with_reminder_record(loop: AppLoop, tmp_path: Path, provider: str):
    ui = make_ui(loop, tmp_path, "ask", provider)
    ev = make_event("Jour fixe")
    page = Page(ui, {"start": {"action": "start", "title": "Jour fixe", "participants_text": "Anna <a@b.de>"}})
    dec = loop.call(lambda: ui.ask_start(Suggestion(ev, ev.participants(), source="graph"), ME))
    page.stop.set()
    shown = page.seen[0]["data"]
    assert shown["mode"] == "ask" and shown["checkbox"] == "" and shown["start_label"] == "Aufnehmen"
    assert shown["statement"] == reminder_statement(provider)  # exactly what the record carries
    c = dec.consent
    assert c.mode == "ask" and c.confirmed is False and c.statement == shown["statement"]
    assert c.summary_provider == provider and c.method == "dialog" and dec.event is ev
    assert [p.name for p in dec.participants] == ["Anna"]
    require_consent(c, summary_provider=provider)  # the controller's gate accepts it


def test_ask_mode_dismiss_starts_nothing(loop: AppLoop, tmp_path: Path):
    ui = make_ui(loop, tmp_path, "ask")
    page = Page(ui, {"start": {"action": "cancel"}})
    assert loop.call(lambda: ui.ask_start(Suggestion(None, [], ""), ME)) is None
    page.stop.set()


def test_confirm_mode_needs_the_ticked_box(loop: AppLoop, tmp_path: Path):
    ui = make_ui(loop, tmp_path, "confirm")
    # first answer without the box (a tampered page): refused in Python, asked again with the error
    page = Page(ui, {"start": [{"action": "start", "title": "T", "participants_text": "", "checked": False},
                               {"action": "start", "title": "T", "participants_text": "", "checked": True}]})
    dec = loop.call(lambda: ui.ask_start(Suggestion(None, [], ""), ME))
    page.stop.set()
    assert page.seen[0]["data"]["checkbox"].startswith("Ja, alle Teilnehmenden")
    assert "Ohne Bestätigung" in page.seen[1]["data"]["error"]
    assert dec.consent.mode == "confirm" and dec.consent.confirmed is True
    assert dec.consent.statement == CONSENT_STATEMENT


def test_confirm_mode_cancel_after_refusal_starts_nothing(loop: AppLoop, tmp_path: Path):
    ui = make_ui(loop, tmp_path, "confirm")
    page = Page(ui, {"start": [{"action": "start", "checked": "yes"}, {"action": "cancel"}]})  # "yes" != True
    assert loop.call(lambda: ui.ask_start(Suggestion(None, [], ""), ME)) is None
    page.stop.set()


def test_call_prompt_in_ask_mode_is_the_one_question(loop: AppLoop, tmp_path: Path):
    ui = make_ui(loop, tmp_path, "ask", "anthropic")
    ev = make_event("Kundentermin")
    prompt = Prompt("calendar", ev)
    # no toast window in this test: the prompt comes as a dialog in the main window (without focus)
    page = Page(ui, {"record_prompt": {"choice": "record"}})
    assert loop.call(lambda: ui.ask_record_prompt(prompt)) is True
    page.stop.set()
    shown = page.seen[0]["data"]
    assert shown["question"] == "Call erkannt: Kundentermin – aufnehmen?"
    assert shown["reminder"] == reminder_statement("anthropic")
    dec = loop.call(lambda: ui.ask_start(Suggestion(None, [], ""), ME))  # no second question
    assert dec.title == "Kundentermin" and dec.event is ev
    assert dec.consent.mode == "ask" and dec.consent.statement == reminder_statement("anthropic")
    assert [p.display for p in dec.participants] == [p.display for p in ev.participants()]
    assert ui.prepared.take() is None  # one-shot


def test_call_prompt_dismiss_details_and_expiry(loop: AppLoop, tmp_path: Path):
    ui = make_ui(loop, tmp_path, "ask")
    page = Page(ui, {"record_prompt": [{"choice": "dismiss"}, {"choice": "details"}]})
    assert loop.call(lambda: ui.ask_record_prompt(Prompt("teams"))) is False
    assert loop.call(lambda: ui.ask_record_prompt(Prompt("teams"))) is True  # -> full start dialog
    assert ui.prepared.take() is None  # "Details" decides nothing
    page.stop.set()
    # nobody answers: expires without starting anything
    t0 = time.monotonic()
    ui2 = make_ui(loop, tmp_path, "ask")
    assert loop.call(lambda: ui2.ask_toast({"mode": "ask", "timeout_s": 0.3})) == ""
    assert time.monotonic() - t0 < 5 and ui2.prepared.take() is None


def test_prepared_start_expires(monkeypatch: pytest.MonkeyPatch):
    from callrec.ui import startflow

    p = startflow.PreparedStart()
    p.put("x")
    monkeypatch.setattr(startflow.PreparedStart, "VALID_S", -1.0)
    assert p.take() is None


# ---------------------------------------------------------------------------
# full flow with the real controller, queue, actions and chain
# ---------------------------------------------------------------------------
class Pipe:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, frozenset[str]]] = []

    def run(self, rec, steps, progress, language=None, should_cancel=None):
        self.jobs.append((rec.folder.name, frozenset(steps)))
        progress("transcribe", 0.5, "Spur mic")
        return ProcessResult(steps_done=list(steps))


def test_web_flow_start_record_stop_question_chain(loop: AppLoop, tmp_path: Path):
    from callrec.controller import Controller
    from callrec.ui.actions import AppActions
    from callrec.ui.webdialogs import WebDialogPort

    cfg = Config()
    cfg.general.recordings_dir = str(tmp_path / "recs")
    cfg.handoff.folder = str(tmp_path / "sp")
    (tmp_path / "sp").mkdir()
    ui = make_ui(loop, tmp_path, "ask")
    pipe = Pipe()
    queue = ProcessingQueue(lambda preset=None: pipe)
    controller = Controller(cfg, ui, FakeBackend, ui.calendar, queue, lambda: ME)
    actions = AppActions(cfg, ui, controller, queue, profiles=None, me_provider=lambda: ME)  # type: ignore[arg-type]
    actions.dialogs = WebDialogPort(ui.broker, lambda r, p: None, ui.notify)
    controller.after_stop = actions.after_recording
    page = Page(ui, {"start": {"action": "start", "title": "Web-Test", "participants_text": "Anna"},
                     "after_recording": lambda d: {"choice": "full", "plan": {**d["data"]["plan"], "speakers": False}}})
    loop.post(controller.toggle)  # hotkey / tray / button: the same path
    for _ in range(200):
        if controller.is_recording:
            break
        time.sleep(0.02)
    assert controller.is_recording
    meta = controller.current.load_meta()
    assert meta["consent"]["mode"] == "ask" and meta["consent"]["confirmed"] is False
    assert any(n == "recording" and p["on"] for _t, n, p in ui.events.sent)
    loop.post(actions.stop_recording)
    for _ in range(300):
        if pipe.jobs and any(n == "notify" and p["title"].startswith("Fertig") for _t, n, p in ui.events.sent):
            break
        time.sleep(0.02)
    page.stop.set()
    kinds = [d["kind"] for d in page.seen]
    assert kinds == ["start", "after_recording"]
    assert page.seen[1]["data"]["plan"]["handoff"] is True  # ticked by default
    assert pipe.jobs and "transcribe" in pipe.jobs[0][1]
    queue.stop()


# ---------------------------------------------------------------------------
# bridge: ids, exposure, links
# ---------------------------------------------------------------------------
class Core:
    def __init__(self, tmp_path: Path, loop: AppLoop) -> None:
        from callrec.library import RecordingIndex

        self.cfg = Config()
        self.cfg.general.recordings_dir = str(tmp_path / "recs")
        self.loop = loop
        self.index = RecordingIndex(self.cfg.recordings_path)
        self.queue = ProcessingQueue(lambda preset=None: Pipe())
        self.posted: list[Any] = []

        class Ctl:
            current = None
            is_recording = False
            calendar = type("C", (), {"source": None})()

            def is_busy(self, rec):
                return False

        self.controller = Ctl()


def test_bridge_rejects_paths_outside_the_recordings_folder(loop: AppLoop, tmp_path: Path):
    from callrec.ui.bridge import Bridge

    processed(tmp_path / "recs", "2026-11-19_1400_A", "A")
    (tmp_path / "secret").mkdir()
    b = Bridge(Core(tmp_path, loop))
    assert b.get_recording("2026-11-19_1400_A")["title"] == "A"
    for bad in ("..", "../secret", "..\\secret", "/etc", "C:\\Windows", "2026-11-19_1400_A/../../secret", "", " A",
                "nope", 5, None, "a\x00b"):
        r = b.get_recording(bad)
        assert "error" in r, bad
    assert "error" in b.get_text("2026-11-19_1400_A", "summary", "../../meta.json")
    assert "error" in b.get_text("2026-11-19_1400_A", "meta", "")
    assert "error" in b.recording_action("2026-11-19_1400_A", "rm -rf")
    assert "error" in b.open_external("http://example.com") and "error" in b.open_external("file:///c:/x")


def test_only_methods_are_exposed_to_the_page(loop: AppLoop, tmp_path: Path):
    """pywebview exposes public attributes recursively (webview/util.py get_functions)."""
    import inspect

    from callrec.ui.bridge import Bridge, CompanionApi

    for obj in (Bridge(Core(tmp_path, loop)), CompanionApi(Core(tmp_path, loop), "toast")):
        public = [n for n in dir(obj) if not n.startswith("_")]
        assert public and all(inspect.ismethod(getattr(obj, n)) for n in public), public
        for n in public:  # pywebview: new Function(<params>, body) - params must not shadow the body's globals
            params = set(inspect.signature(getattr(obj, n)).parameters)
            assert not params & {"window", "Array", "Math", "Promise", "arguments"}, (n, params)
    assert set(n for n in dir(CompanionApi) if not n.startswith("_")) == {
        "ready", "state", "stop", "autostop", "toast_answer", "show_main", "log_js_error"}


def test_bridge_errors_are_german_text_not_tracebacks(loop: AppLoop, tmp_path: Path):
    from callrec.ui.bridge import Bridge

    b = Bridge(Core(tmp_path, loop))
    r = b.get_recording("../x")
    assert r == {"error": "Ungültige Aufnahme."}


# ---------------------------------------------------------------------------
# DTOs, Markdown blocks, settings
# ---------------------------------------------------------------------------
def test_markdown_blocks_keep_hostile_text_as_text():
    from callrec.ui.viewmodel import markdown_blocks

    md = "# T <script>x</script>\n\n**[00:00:01] <img src=x onerror=1>:** hi <b>x</b>\n\n| a | <i>b</i> |\n|---|---|\n| 1 | 2 |"
    blocks = markdown_blocks(md, transcript=True)
    assert blocks[0] == {"t": "h", "level": 1, "inl": [["T <script>x</script>", ""]]}
    assert blocks[1]["t"] == "utt" and blocks[1]["who"] == "<img src=x onerror=1>"
    assert blocks[1]["inl"] == [["hi <b>x</b>", ""]]
    assert blocks[2]["head"][1] == [["<i>b</i>", ""]]
    json.dumps(blocks)  # plain data only


def test_large_transcript_blocks_are_fast():
    from callrec.ui.viewmodel import markdown_blocks

    md = "\n\n".join(f"**[{i // 3600:02d}:{i % 3600 // 60:02d}:{i % 60:02d}] Person {i % 4}:** Satz {i} " * 1
                     for i in range(0, 7200, 3))
    t0 = time.monotonic()
    blocks = markdown_blocks(md, transcript=True)
    assert len(blocks) == 2400 and time.monotonic() - t0 < 2.0


def test_settings_schema_patch_and_confirmations(tmp_path: Path):
    from callrec.ui import settingsmodel as m

    ids = {f.id for f in m.all_fields()}
    assert {"ui.frontend", "ui.theme", "consent.mode", "summary.provider", "processing.default_handoff"} <= ids
    cfg = m.apply_patch(Config(), {"ui.frontend": "web", "autostop.silence_dbfs": "−40,5", "consent.mode": "confirm"})
    assert cfg.ui.frontend == "web" and cfg.autostop.silence_dbfs == -40.5 and cfg.consent.mode == "confirm"
    for bad in ({"nope.x": 1}, {"ui.frontend": "qt"}, {"autostop.countdown_s": "abc"}, {"ui.show_overlay": "ja"},
                {"hotkey.toggle": "kaputt+"}):
        with pytest.raises(Exception):  # noqa: B017 - ConfigError or HotkeyError
            m.apply_patch(Config(), bad)
    written: list[Config] = []
    assert m.save(Config(), {"summary.provider": "anthropic"}, [], written.append)["confirm"]["kind"] == "cloud"
    assert not written
    assert m.save(Config(), {"summary.provider": "anthropic"}, ["cloud"], written.append) == {"ok": True}
    assert written[-1].summary.anthropic_transcripts_leave_this_computer_confirmed is True
    # switching away revokes the confirmation
    m.save(written[-1], {"summary.provider": "ollama"}, [], written.append)
    assert written[-1].summary.anthropic_transcripts_leave_this_computer_confirmed is False
    r = m.save(Config(), {"processing.after_recording": "full", "processing.handoff_without_asking": True}, [],
               written.append)
    assert r["confirm"]["kind"] == "auto_handoff"
    assert "error" in m.save(Config(), {"transcription.beam_size": "x"}, [], written.append)


def test_presets_still_cannot_set_cloud_or_scope(tmp_path: Path):
    from callrec.config import apply_preset

    cfg = apply_preset(Config(), {"summary": {"provider": "anthropic"}, "speakers": {"match_scope": "all_profiles"},
                                  "ui": {"frontend": "web"}, "consent": {"mode": "confirm"}}, "p")
    assert cfg.summary.provider != "anthropic" and cfg.speakers.match_scope == "participants"
    assert cfg.ui.frontend == "tk" and cfg.consent.mode == "ask"


def test_default_handoff_migration(tmp_path: Path):
    p = tmp_path / "config.toml"
    p.write_text('[processing]\ndefault_handoff = false\n[ui]\nshow_overlay = true\n', encoding="utf-8")
    assert load_config(p).processing.default_handoff is True  # 0.2 template default, not a choice
    cfg = load_config(p)
    cfg.processing.default_handoff = False
    save_config(cfg, p)  # written by 0.3 (with [ui] frontend): an explicit false stays false
    assert load_config(p).processing.default_handoff is False
    with pytest.raises(ConfigError):
        p.write_text('[ui]\nfrontend = "qt"\n', encoding="utf-8")
        load_config(p)


def test_cli_tray_ui_switch_is_remembered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from callrec import cli, paths

    seen: list[Any] = []
    monkeypatch.setattr("callrec.ui.app.run_tray_app", lambda **kw: seen.append(kw) or 0)
    assert cli.main(["tray", "--ui", "web"]) == 0
    assert load_config(paths.config_file()).ui.frontend == "web" and seen[-1]["frontend"] == "web"
    assert cli.main(["tray", "--ui", "classic", "--once"]) == 0
    assert load_config(paths.config_file()).ui.frontend == "web" and seen[-1]["frontend"] == "tk"
    assert cli.main(["tray", "--ui", "classic"]) == 0
    assert load_config(paths.config_file()).ui.frontend == "tk"


def test_cli_record_uses_the_consent_mode(monkeypatch: pytest.MonkeyPatch):
    from callrec import consent

    asked: list[str] = []
    monkeypatch.setattr(consent, "cli_start", lambda *a, **k: asked.append("ask") or (_ for _ in ()).throw(ConsentError("x")))
    monkeypatch.setattr(consent, "cli_confirm", lambda *a, **k: asked.append("confirm") or (_ for _ in ()).throw(ConsentError("x")))
    from callrec.ui.startflow import build_record, consent_mode

    assert consent_mode(Config()) == "ask"
    rec = build_record("ask", user_action=True, me=ME, participants=[], provider="")
    assert rec.mode == "ask" and rec.confirmed is False
    with pytest.raises(ConsentError):
        build_record("ask", user_action=False, me=ME, participants=[], provider="")
    with pytest.raises(ConsentError):
        build_record("confirm", user_action=False, me=ME, participants=[], provider="")


# ---------------------------------------------------------------------------
# loopback server
# ---------------------------------------------------------------------------
def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310 - loopback test
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), b""


def test_ui_server_token_whitelist_host_and_csp(tmp_path: Path):
    from callrec.ui.webserver import UiServer

    s = UiServer()
    s.start()
    try:
        code, headers, body = _get(s.url("index.html"))
        assert code == 200 and b"<title>CallRecorder</title>" in body
        assert "default-src 'none'" in headers["Content-Security-Policy"] and headers["X-Content-Type-Options"] == "nosniff"
        assert s.host == "127.0.0.1"
        bad = [f"{s.origin}/index.html", f"{s.origin}/wrongtoken/index.html", s.url("../webserver.py"),
               s.url("%2e%2e/webserver.py"), s.url("missing.js"), f"{s.origin}/{s.token}/clip/xyz.wav",
               f"{s.origin}/{s.token}/sub/app.js"]
        for u in bad:
            assert _get(u)[0] == 404, u
        assert _get(s.url("app.js"), {"Host": "evil.example"})[0] == 403  # DNS rebinding
        clip = tmp_path / "c.wav"
        clip.write_bytes(b"RIFF" + b"x" * 100)
        url = s.publish(clip)
        code, headers, body = _get(url, {"Range": "bytes=0-3"})
        assert code == 206 and body == b"RIFF" and headers["Content-Range"] == "bytes 0-3/104"
        assert s.is_own_url(s.url("index.html") + "?theme=dark") and not s.is_own_url("https://example.com/")
    finally:
        s.stop()


def test_web_files_have_no_inline_scripts_styles_or_remote_urls():
    import re

    from callrec.ui.webserver import WEB_DIR

    for p in WEB_DIR.iterdir():
        text = p.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text), p.name
        assert 'style="' not in text and "style='" not in text, p.name
        assert not re.search(r"(src|href)=[\"']?(https?:)?//", text), p.name
        assert "innerHTML = `" not in text or p.name == "companion.js", p.name
    assert {"index.html", "app.js", "app.css", "indicator.html", "toast.html", "companion.js", "theme.js"} <= {
        p.name for p in WEB_DIR.iterdir()}


def test_frontend_choice_falls_back_to_tk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from callrec.ui import app, webapp

    monkeypatch.setattr(webapp, "preflight", lambda: "Die Microsoft-Edge-WebView2-Laufzeit wurde nicht gefunden.")
    ran: list[str] = []
    monkeypatch.setattr(app, "acquire_single_instance", lambda: True)
    monkeypatch.setattr(app, "_run_tk", lambda cfg, log_file, start_minimized, notice="": ran.append(notice) or 0)
    cfg = Config()
    cfg.ui.frontend = "web"
    assert app.run_tray_app(cfg) == 0
    assert ran and "WebView2" in ran[0] and "callrec tray --ui" in ran[0]


def test_dto_home_and_list(tmp_path: Path):
    from callrec.library import RecordingIndex
    from callrec.ui import dto

    root = tmp_path / "recs"
    processed(root, "2026-11-19_1400_A", "A", unassigned=2)
    processed(root, "2026-11-18_1000_B", "B", summary=False)
    infos = RecordingIndex(root).scan()
    L = dto.recordings_list(infos, "speakers_open", "", set(), "")
    assert [i["title"] for i in L["items"]] == ["A"] and L["items"][0]["action"]["key"] == "review"
    assert dto.recordings_list(infos, "unknown", "b", set(), "")["filter"] == "all"
    h = dto.home(infos, set(), "", None)
    assert {a["title"] for a in h["attention"]} == {"A", "B"}
    json.dumps(h)
    now = datetime.now(UTC)
    ev = make_event("X", start=now - timedelta(minutes=5))
    cal = dto.calendar_events([ev], "graph", "", lambda e: True, now)
    assert cal["events"][0]["current"] is True and cal["events"][0]["people"]


def test_token_contrast():
    """WCAG AA for the colour tokens of app.css in both themes (text 4.5:1, marks/boundaries 3:1)."""
    import re

    from callrec.ui.webserver import WEB_DIR

    css = (WEB_DIR / "app.css").read_text(encoding="utf-8")

    def block(sel: str) -> dict[str, str]:
        m = re.search(re.escape(sel) + r"\s*\{(.*?)\n\}", css, re.S)
        assert m, sel
        return dict(re.findall(r"--([\w-]+):\s*(#[0-9A-Fa-f]{6})", m.group(1)))

    def lum(h: str) -> float:
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    def ratio(a: str, b: str) -> float:
        la, lb = sorted((lum(a), lum(b)), reverse=True)
        return (la + 0.05) / (lb + 0.05)

    light = block(":root")
    dark = {**light, **block(':root[data-theme="dark"]')}
    text = [("text", "bg"), ("text", "surface"), ("text-2", "surface"), ("text-2", "frame"), ("text-3", "surface"),
            ("text-3", "bg"), ("text-3", "frame"), ("text-3", "surface-2"), ("text-on-accent", "accent-fill"),
            ("accent-text", "surface"), ("accent-text", "frame"), ("accent-text", "accent-subtle"),
            ("accent-subtle-text", "accent-subtle"), ("rec-text", "rec-subtle"), ("ok-text", "ok-subtle"),
            ("warn-text", "warn-subtle"), ("err-text", "err-subtle"), ("info-text", "info-subtle")]
    text += [(f"sp-{i}", "surface") for i in range(1, 7)]
    marks = [("border-control", "surface"), ("accent", "surface"), ("accent", "bg"), ("warn-mark", "surface")]
    for name, t in (("hell", light), ("dunkel", dark)):
        for a, b in text:
            assert ratio(t[a], t[b]) >= 4.5, (name, a, b, round(ratio(t[a], t[b]), 2))
        for a, b in marks:
            assert ratio(t[a], t[b]) >= 3.0, (name, a, b, round(ratio(t[a], t[b]), 2))
    assert ratio("#FFFFFF", light["rec"]) >= 4.5
