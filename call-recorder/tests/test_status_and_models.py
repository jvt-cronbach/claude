"""Console status, retry status/time budget, robust summary failure, transcript header, models list/remove,
download progress (incl. the real huggingface_hub xet code path with a fake Xet session)."""

from __future__ import annotations

import io
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from callrec.config import Config, SummaryConfig
from callrec.console import ConsoleStatus
from callrec.models import SpeakerInfo, Utterance
from callrec.transcript import render_markdown
from tests.test_pipeline import FakeSummarizer, make_pipeline, profiles, recording_with_voices  # noqa: F401


# -- console status ------------------------------------------------------------------
def test_tty_updates_one_line_in_place_and_cuts_to_width():
    out = io.StringIO()
    st = ConsoleStatus(out, is_tty=True, width=lambda: 40)
    for i in range(5):
        st.update(f"Zusammenfassung: Protokoll: warte auf anthropic – {i * 2} s")
    st.update("Zusammenfassung: " + "x" * 100)
    st.finish()
    text = out.getvalue()
    assert text.count("\n") == 1  # one line, finished once
    assert all(len(part) <= 39 for part in text.replace("\n", "").split("\r"))
    st.update("Transkription 10 %")
    st.update("Zusammenfassung 0 %")  # stage change: the previous stage stays visible
    st.finish()
    assert out.getvalue().count("\n") == 3


def test_non_tty_prints_only_stage_or_percent_changes_at_most_every_30_s():
    out = io.StringIO()
    now = {"t": 0.0}
    st = ConsoleStatus(out, is_tty=False, clock=lambda: now["t"])
    st.update("Transkription 10 % (Spur mic)")
    for i in range(300):  # 10 minutes of heartbeats every 2 s
        now["t"] += 2
        st.update(f"Zusammenfassung: Protokoll: warte auf anthropic – {i * 2} s")
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("Transkription") and lines[1].startswith("Zusammenfassung")
    assert 15 <= len(lines) <= 22  # ~ one line per 30 s instead of 300


# -- summary heartbeat / Anthropic retries ------------------------------------------------
def test_heartbeat_shows_current_state():
    from callrec.summary import heartbeat_text

    state = {"detail": "Versuch 1/4 fehlgeschlagen (Zeitüberschreitung), neuer Versuch 2/4 in 20 s", "since": 100.0,
             "until": 120.0}
    assert heartbeat_text(state, 105.0, 50.0).endswith("– noch 15 s (insgesamt 55 s)")
    state = {"detail": "Versuch 2/4 läuft (vorher: Zeitüberschreitung)", "since": 120.0, "until": None}
    assert heartbeat_text(state, 150.0, 50.0) == "Versuch 2/4 läuft (vorher: Zeitüberschreitung) – 30 s (insgesamt 100 s)"


def _timeout_provider(**kw: Any):
    httpx2 = pytest.importorskip("httpx2")
    from callrec.llm_anthropic import AnthropicProvider

    clock = {"t": 0.0}
    requests: list[Any] = []

    def handler(request):
        requests.append(request)
        clock["t"] += 15.0  # each attempt hangs until the connect timeout
        raise httpx2.ConnectTimeout("timed out", request=request)

    def sleep(s: float) -> None:
        clock["t"] += s

    p = AnthropicProvider("sk-ant-api03-TESTKEY-0123456789abcdefghij", transport=httpx2.MockTransport(handler),
                          sleep=sleep, clock=lambda: clock["t"], **kw)
    return p, requests, clock


def test_timeouts_are_bounded_by_the_retry_budget_and_status_is_current():
    from callrec.llm_anthropic import AnthropicError

    p, requests, clock = _timeout_provider(max_attempts=10, retry_budget_s=60.0)
    msgs: list[tuple[str, float | None]] = []
    with pytest.raises(AnthropicError, match=r"Zeitüberschreitung.*aufgegeben nach \d Versuch\(en\)"):
        p.chat("claude-opus-5-5", [{"role": "user", "content": "x"}], {},
               progress=lambda text, wait_until=None: msgs.append((text, wait_until)))
    assert clock["t"] <= 60.0 + 15.0  # budget + one connect timeout, not 10 attempts x backoff
    assert len(requests) < 10
    texts = [m[0] for m in msgs]
    assert texts[0].startswith("Versuch 1/10 fehlgeschlagen (Zeitüberschreitung), neuer Versuch 2/10 in")
    assert msgs[0][1] is not None  # waiting has an end time -> heartbeat counts down
    assert texts[1] == "Versuch 2/10 läuft (vorher: Zeitüberschreitung)"  # not the stale "in 21 s"


def test_sdk_retries_are_off_so_every_attempt_is_reported():
    p, requests, _clock = _timeout_provider(max_attempts=2, retry_budget_s=600.0)
    with pytest.raises(Exception):  # noqa: B017
        p.chat("claude-opus-5-5", [{"role": "user", "content": "x"}], {})
    assert len(requests) == 2  # without SDK retries: exactly our attempts
    assert p.client.max_retries == 0
    assert p.client.timeout.read == 180.0 and p.client.timeout.connect == 15.0


def test_make_provider_uses_the_anthropic_timeouts():
    from callrec.llm import make_provider

    cfg = SummaryConfig(provider="anthropic", anthropic_transcripts_leave_this_computer_confirmed=True,
                        anthropic_read_timeout_s=90.0, anthropic_retry_budget_s=120.0)
    p = make_provider(cfg, anthropic_key=("sk-ant-x", "test"))
    assert p.timeout_s == 90.0 and p.retry_budget_s == 120.0  # type: ignore[attr-defined]
    assert p.worst_case_wait_s() == 120.0 + 15.0 + 90.0  # type: ignore[attr-defined]


# -- summary failure never fails the recording ---------------------------------------------
class BrokenSummarizer(FakeSummarizer):
    def summarize(self, title, participants, lines, date=""):
        raise RuntimeError("kaputt")


def test_failed_summary_keeps_recording_done(tmp_path: Path, profiles):  # noqa: F811
    from callrec.llm_anthropic import AnthropicError

    rec = recording_with_voices(tmp_path / "r", 330, 550)
    pipeline, _, _ = make_pipeline(profiles, BrokenSummarizer())
    result = pipeline.run(rec)
    meta = rec.load_meta()["processing"]
    assert meta["status"] == "done" and meta["summary"]["status"] == "failed"
    assert result.summary_failed and "kaputt" in result.summary_skipped
    assert rec.transcript_path.exists()
    hint = [m for m in result.messages if m.startswith("Zusammenfassung nicht erstellt")][0]
    assert "Das Transkript ist fertig" in hint and "callrec summarize" in hint

    class Offline(FakeSummarizer):
        def summarize(self, title, participants, lines, date=""):
            raise AnthropicError("Zeitüberschreitung … (aufgegeben nach 3 Versuch(en) in 4:10 min)")

    pipeline2, _, _ = make_pipeline(profiles, Offline())
    r2 = pipeline2.run(rec, {"summarize"})
    assert r2.summary_failed and rec.load_meta()["processing"]["status"] == "done"


def test_summary_language_instruction_for_english_call(tmp_path: Path, profiles):  # noqa: F811
    from callrec.summary import language_instruction

    text = language_instruction("de", "en")
    assert "Sprache des Protokolls: Deutsch" in text and "auf Englisch" in text and "vollständig auf Deutsch" in text
    assert "Englisch" in language_instruction("en", "de")

    seen: dict[str, Any] = {}

    class LangSummarizer(FakeSummarizer):
        def summarize(self, title, participants, lines, date="", transcript_language=""):
            seen["lang"] = transcript_language
            return super().summarize(title, participants, lines, date)

    rec = recording_with_voices(tmp_path / "r", 330, 550)
    pipeline, _, _ = make_pipeline(profiles, LangSummarizer())
    pipeline.run(rec)
    assert seen["lang"] == "de"  # FakeTranscriber reports German


# -- transcript header -----------------------------------------------------------------
def test_header_lists_speakers_consistently():
    speakers = {sid: SpeakerInfo(sid, f"Sprecher {sid[1:]}") for sid in ("S1", "S2", "S3", "S4", "S5", "S6")}
    speakers["ME"] = SpeakerInfo("ME", "Ich", "Ich Selbst", source="self")
    speakers["S?"] = SpeakerInfo("S?", "Unbekannt")
    utts = [Utterance(i * 2.0, i * 2.0 + 1, f"S{i}", f"Text {i}", "remote") for i in range(1, 6)]
    utts.append(Utterance(20, 21, "ME", "Hallo", "mic"))
    md = render_markdown("T", None, 30, [], utts, speakers)
    spoken = [line for line in md.splitlines() if line.startswith("- **Sprechende:**")][0]
    hint = [line for line in md.splitlines() if line.startswith("- **Hinweis:**")][0]
    import re

    assert len(re.findall(r"Sprecher \d", spoken)) == 5 and len(re.findall(r"Sprecher \d", hint)) == 5  # both 5
    assert "Sprecher 6" not in hint and "Unbekannt" not in hint
    assert "- **Ohne Text:** Sprecher 6 (nur sehr kurze Sprechabschnitte" in md


# -- installed models ------------------------------------------------------------------------
def _fake_model(root: Path, repo: str, blob_mb: int = 3, partial_mb: int = 0, complete: bool = True) -> Path:
    folder = root / ("models--" + repo.replace("/", "--"))
    snap = folder / "snapshots" / "abc"
    snap.mkdir(parents=True)
    blobs = folder / "blobs"
    blobs.mkdir()
    (blobs / "b1").write_bytes(b"x" * blob_mb * 1_000_000)
    if partial_mb:
        (blobs / "b1.1a2b3c4d.incomplete").write_bytes(b"y" * partial_mb * 1_000_000)
    if complete:
        for f in ("config.json", "tokenizer.json", "vocabulary.json"):
            (snap / f).write_text("{}")
        try:
            (snap / "model.bin").symlink_to(blobs / "b1")
        except OSError:
            (snap / "model.bin").write_bytes(b"x")
    return folder


def test_models_list_and_remove(tmp_path: Path):
    from callrec.asr.modelcache import find_model, installed_models, remove_model

    root = tmp_path / "whisper"
    _fake_model(root, "Systran/faster-whisper-large-v3", blob_mb=3, partial_mb=2)
    _fake_model(root, "mobiuslabsgmbh/faster-whisper-large-v3-turbo", blob_mb=1)
    models = {m.name: m for m in installed_models(root)}
    assert set(models) == {"large-v3", "large-v3-turbo"}  # canonical names, not the aliases "large"/"turbo"
    old = models["large-v3"]
    assert old.complete and old.partial_size == 2_000_000 and old.size >= 5_000_000
    assert "Reste abgebrochener Downloads" in old.describe()
    assert find_model(root, "large-v3") is not None and find_model(root, "Systran/faster-whisper-large-v3") is not None
    assert remove_model(old, root) >= 5_000_000
    assert [m.name for m in installed_models(root)] == ["large-v3-turbo"]
    outside = installed_models(root)[0]
    outside.folder = tmp_path  # never delete anything outside the model folder
    with pytest.raises(ValueError):
        remove_model(outside, root)


def test_cli_models_list_and_remove(tmp_path: Path, capsys, monkeypatch):
    from callrec import cli, paths

    root = paths.models_dir() / "whisper"
    _fake_model(root, "Systran/faster-whisper-large-v3", partial_mb=1)
    monkeypatch.setattr(cli, "_selected_whisper_model", lambda cfg: ("large-v3-turbo", ""))
    assert cli.main(["models", "list"]) == 0
    out = capsys.readouterr().out
    assert "large-v3 " in out and "Gesamt:" in out and "callrec models remove" in out
    assert cli.main(["models", "remove", "medium", "--yes"]) == 1
    assert cli.main(["models", "remove", "large-v3", "--yes"]) == 0
    assert "Gelöscht: large-v3" in capsys.readouterr().out and not any(root.iterdir())


# -- download progress -------------------------------------------------------------------
def test_download_display_falls_back_to_honest_disk_growth():
    from callrec.asr.download import DownloadDisplay

    d = DownloadDisplay()
    assert d.feed(["DOWNLOAD", "0", "0", "0"]) is None  # never "0 MB empfangen"
    text = d.feed(["DOWNLOADDISK", "83", "350000000"])
    assert text == ("Modell wird heruntergeladen … 1:23 min, bisher 350 MB auf der Festplatte "
                    "(genauer Fortschritt nicht verfügbar)")
    assert "von 1,60 GB" in (d.feed(["DOWNLOAD", "0", "1600000000", "400000000"]) or "")
    assert d.feed(["DOWNLOADDISK", "90", "500000000"]) is None  # real bytes known -> no disk text
    assert "geprüft (1,62 GB)" in (d.feed(["DOWNLOADED", "1620000000"]) or "")


def test_real_snapshot_download_xet_path_reaches_our_progress(tmp_path: Path, monkeypatch):
    """huggingface_hub 2.4.0: snapshot_download -> xet_get -> XetDownloadProgressReporter -> _AggregatedTqdm.

    Only hf_xet's session is faked (no network); everything above it is the installed library code.
    """
    sd = pytest.importorskip("huggingface_hub._snapshot_download")
    import huggingface_hub.file_download as fd
    import huggingface_hub.utils._xet as ux

    from callrec.asr.download import download_whisper_model

    size = 1_000_000

    class Group:
        def __init__(self, cb):
            self.cb = cb

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def start_download_file(self, info, path):
            for i in range(1, 5):
                self.cb(types.SimpleNamespace(
                    total_bytes=size, total_bytes_completed=size * i // 4, total_bytes_completion_rate=1.0,
                    total_transfer_bytes=size, total_transfer_bytes_completed=size * i // 5,
                    total_transfer_bytes_completion_rate=1.0), {})
            Path(path).write_bytes(b"x")

    monkeypatch.setattr(ux, "get_xet_session", lambda: types.SimpleNamespace(
        new_file_download_group=lambda **kw: Group(kw["progress_callback"])))
    monkeypatch.setattr(ux, "refresh_xet_connection_info",
                        lambda **kw: types.SimpleNamespace(endpoint="e", access_token="t", expiration_unix_epoch=0))

    def fake_hf_hub_download(repo_id, filename, tqdm_class=None, **kw):
        tmp = tmp_path / (filename + ".incomplete")
        fd.xet_get(incomplete_path=tmp, xet_file_data=types.SimpleNamespace(file_hash="h", refresh_route="r"),
                   headers={}, expected_size=size, displayed_filename=filename, tqdm_class=tqdm_class)
        return str(tmp)

    entry = sd.TreeCacheEntry(size=size, blob_id="b", lfs_sha256=None, lfs_size=None, xet_hash="h")
    monkeypatch.setattr(sd, "hf_hub_download", fake_hf_hub_download)
    monkeypatch.setattr(sd, "read_tree_cache", lambda folder, commit: {"model.bin": entry})
    real = sd.snapshot_download
    reports: list[tuple[int, int, int]] = []
    download_whisper_model("x/y", tmp_path / "cache", lambda *r: reports.append(r),
                           snapshot_download=lambda repo, **kw: real(repo, revision="a" * 40, **kw))
    assert reports[-1][:2] == (size, size) and max(r[2] for r in reports) > 0


def test_disk_watcher_reports_growth(tmp_path: Path):
    import time

    from callrec.asr.download import DiskWatcher

    seen: list[tuple[float, int]] = []
    w = DiskWatcher([tmp_path], lambda e, b: seen.append((e, b)), interval_s=0.05).start()
    (tmp_path / "x.incomplete").write_bytes(b"z" * 5000)
    time.sleep(0.3)
    w.stop()
    assert seen and seen[-1][1] == 5000


def test_verify_snapshot_and_failed_download_is_not_fertig(tmp_path: Path, capsys):
    from callrec import cli
    from callrec.asr.download import ModelIncomplete, verify_snapshot

    with pytest.raises(ModelIncomplete):
        verify_snapshot("large-v3-turbo", tmp_path / "empty")  # real faster-whisper lookup, offline
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "config.json").write_text("{}")
    with pytest.raises(ModelIncomplete, match="model.bin"):
        verify_snapshot("large-v3-turbo", tmp_path, resolver=lambda *a, **k: str(snap))
    for f in ("tokenizer.json", "vocabulary.txt"):
        (snap / f).write_text("x")
    (snap / "model.bin").write_bytes(b"m" * 1_500_000)
    assert verify_snapshot("large-v3-turbo", tmp_path, resolver=lambda *a, **k: str(snap)) > 1_500_000

    script = tmp_path / "dl.py"  # worker that "downloads" but cannot verify the model
    script.write_text("import sys\nprint('STAGE download', flush=True)\nprint('FAILED Modell fehlt', flush=True)\n"
                      "sys.exit(3)\n", encoding="utf-8")
    assert cli.run_whisper_download("large-v3-turbo", tmp_path, command_prefix=[sys.executable, str(script)]) == 3
    assert "FEHLER: Modell fehlt" in capsys.readouterr().out


# -- tray / queue ------------------------------------------------------------------------------
def test_tray_language_items_and_queue_passes_language(tmp_path: Path, profiles):  # noqa: F811
    import threading

    from callrec.jobs import ProcessingQueue
    from callrec.ui.app import retranscribe_language_items

    items = retranscribe_language_items(["de", "en"])
    assert items == [("Deutsch", "retranscribe_lang:de"), ("Englisch", "retranscribe_lang:en"),
                     ("Sprache automatisch erkennen", "retranscribe_lang:auto")]

    rec = recording_with_voices(tmp_path / "r", 330, 550)
    seen: dict[str, Any] = {}

    class Spy:
        def run(self, rec, steps, progress, language=None):
            seen["language"] = language
            from callrec.pipeline import ProcessResult

            return ProcessResult()

    done = threading.Event()
    q = ProcessingQueue(lambda: Spy(), status=lambda s: None)  # type: ignore[arg-type, return-value]
    q.submit(rec, {"transcribe"}, on_done=lambda *a: done.set(), language="en")
    assert done.wait(5) and seen["language"] == "en"
    q.stop()


def test_cli_retranscribe_accepts_language():
    from callrec.cli import build_parser

    args = build_parser().parse_args(["retranscribe", "X", "--language", "en"])
    assert args.language == "en" and "transcribe" in args.steps
    assert build_parser().parse_args(["summarize", "X"]).language is None


def test_config_template_documents_new_keys():
    import tomllib

    from callrec.config import DEFAULT_CONFIG_TEMPLATE

    t = tomllib.loads(DEFAULT_CONFIG_TEMPLATE)["transcription"]
    assert t["language"] == "auto" and t["language_candidates"] == ["de", "en"]
    assert Config().summary.language == "de"


def test_timeout_messages_mention_vpn_and_test_connection_fails_fast():
    httpx2 = pytest.importorskip("httpx2")
    from callrec.llm_anthropic import TEST_TIMEOUT_S, AnthropicError, AnthropicProvider

    seen: list[Any] = []

    def handler(request):
        seen.append(request.extensions.get("timeout"))
        raise httpx2.ConnectTimeout("timed out", request=request)

    p = AnthropicProvider("sk-ant-api03-TESTKEY-0123456789abcdefghij", transport=httpx2.MockTransport(handler))
    with pytest.raises(AnthropicError, match="VPN") as exc:
        p.test_connection()
    assert "Split-Tunneling" in str(exc.value) and len(seen) == 1  # no retries
    assert TEST_TIMEOUT_S == 20.0 and seen[0]["read"] == 20.0 and seen[0]["connect"] == 10.0

    p2, _r, _c = _timeout_provider(max_attempts=1)
    with pytest.raises(AnthropicError, match="VPN"):
        p2.chat("claude-opus-5-5", [{"role": "user", "content": "x"}], {})
