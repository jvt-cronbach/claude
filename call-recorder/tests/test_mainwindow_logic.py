"""Logic behind the main window that does not need Tk: library index, filters, rename/delete,
queue snapshot/cancel, controller operations, setup checklist, error hints, view helpers."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from callrec import hints, setupcheck
from callrec.config import Config
from callrec.library import (
    FAILED,
    MISSING,
    OK,
    OPEN,
    LibraryError,
    RecordingIndex,
    delete_recording,
    filter_infos,
    fmt_duration,
    handoff_description,
    read_info,
    rename_recording,
    stage_cells,
    status_description,
)
from callrec.pipeline import ProcessingCancelled, ProcessResult
from callrec.recording import Recording
from callrec.ui import viewmodel as vm
from tests.conftest import make_event, make_recording


def processed(root: Path, name: str = "2026-11-19_1400_Projekt-Jour-fixe", title: str = "Projekt Jour fixe",
              summary: bool = True, unassigned: int = 0) -> Recording:
    tmp = root / "_tmp"
    rec = make_recording(tmp, title=title, with_parts=False)
    folder = root / name
    folder.parent.mkdir(parents=True, exist_ok=True)
    rec.folder.rename(folder)
    tmp.rmdir()
    rec = Recording(folder)
    transcript = "# Transkript: x\n\n**[00:00:01] Anna:** Hallo\n"
    rec.transcript_path.write_text(transcript, encoding="utf-8")
    rec.update_meta(["versions", "transcript"], [{"version": 1, "file": "transcript.v1.md", "created": "2026-11-19T15:00:00+01:00"}])
    (folder / "transcript.v1.md").write_text(transcript, encoding="utf-8")
    rec.update_meta(["processing", "transcription"], {"mic": {"language": "de"}, "remote": {"language": "de"}})
    rec.update_meta(["processing", "speakers"], {"spoken": 2, "unassigned": unassigned})
    rec.update_meta(["processing", "status"], "done")
    if summary:
        rec.summary_path.write_text("# Zusammenfassung: x\n\n## Kurzfassung\nAlles gut.\n", encoding="utf-8")
        (folder / "summary.v1.md").write_text(rec.summary_path.read_text(encoding="utf-8"), encoding="utf-8")
        rec.update_meta(["versions", "summary"], [{"version": 1, "file": "summary.v1.md"}])
    return rec


# ---------------------------------------------------------------------------
# library
# ---------------------------------------------------------------------------
def test_read_info_stages_and_handoff_change_detection(tmp_path: Path):
    from callrec.handoff import FolderTarget, handoff_summary

    rec = processed(tmp_path / "recs", unassigned=1)
    info = read_info(rec.folder)
    assert info.title == "Projekt Jour fixe" and info.language == "de" and info.duration_s == 6.0
    assert (info.transcript_state(), info.speakers_state(), info.summary_state(), info.handoff_state()) == \
        (OK, OPEN, OK, MISSING)
    assert stage_cells(info) == ("✓", "1 offen", "✓", "–")
    assert "Noch nicht übergeben" in handoff_description(info)
    assert "1 Stimme(n)" in status_description(info)

    out = tmp_path / "sp"
    out.mkdir()
    handoff_summary(rec, FolderTarget(out))
    info = read_info(rec.folder)
    assert info.handoff_state() == OK and info.handoff_current
    assert stage_cells(info)[3].startswith("✓ ")
    assert "unverändert" in handoff_description(info) and str(out) in handoff_description(info)

    rec.summary_path.write_text("# Zusammenfassung: x\n\n## Kurzfassung\nNeu.\n", encoding="utf-8")
    info = read_info(rec.folder)
    assert info.handoff_state() == OPEN and not info.handoff_current
    assert "geändert" in handoff_description(info) and stage_cells(info)[3] == "geändert"


def test_read_info_failed_summary_and_broken_meta(tmp_path: Path):
    rec = processed(tmp_path / "recs", summary=False)
    rec.update_meta(["processing", "summary"], {"skipped": "Zeitüberschreitung bei der Verbindung zu api.anthropic.com."})
    info = read_info(rec.folder)
    assert info.summary_state() == FAILED and info.has_problem()
    assert "Zeitüberschreitung" in status_description(info)
    bad = tmp_path / "recs" / "2026-01-01_0900_kaputt"
    bad.mkdir()
    (bad / "meta.json").write_text("{kaputt", encoding="utf-8")
    assert "nicht lesbar" in read_info(bad).error


def test_old_recording_speakers_from_speakers_json(tmp_path: Path):
    from callrec.models import SpeakerInfo

    rec = make_recording(tmp_path / "r", with_parts=False)
    rec.save_speakers({"ME": SpeakerInfo("ME", "Ich", name="Ich"), "S1": SpeakerInfo("S1", "Sprecher 1"),
                       "S2": SpeakerInfo("S2", "Sprecher 2", name="Anna")})
    info = read_info(rec.folder)
    assert info.speakers_unassigned == 1 and info.speakers_state() == OPEN


def test_index_caches_and_rereads_changed_folders(tmp_path: Path, monkeypatch):
    root = tmp_path / "recs"
    for i in range(5):
        processed(root, name=f"2026-11-1{i}_1000_Call-{i}", title=f"Call {i}")
    calls: list[Path] = []
    import callrec.library as lib

    real = lib.read_info
    monkeypatch.setattr(lib, "read_info", lambda f: calls.append(f) or real(f))
    idx = RecordingIndex(root)
    batches: list[int] = []
    infos = idx.scan(batch=2, on_batch=lambda part: batches.append(len(part)))
    assert [i.title for i in infos] == ["Call 4", "Call 3", "Call 2", "Call 1", "Call 0"]  # newest first
    assert batches == [2, 4] and len(calls) == 5
    calls.clear()
    idx.scan()
    assert calls == []  # nothing changed: served from the cache
    time.sleep(0.01)
    Recording(infos[2].folder).update_meta(["title"], "Geändert")
    again = idx.scan()
    assert len(calls) == 1 and again[2].title == "Geändert"


def test_index_scales_to_hundreds(tmp_path: Path):
    root = tmp_path / "recs"
    proto = processed(root, name="2026-01-01_0000_proto")
    import shutil

    for i in range(300):
        shutil.copytree(proto.folder, root / f"2025-{i // 28 + 1:02d}-{i % 28 + 1:02d}_1000_Call-{i}")
    idx = RecordingIndex(root)
    t0 = time.perf_counter()
    assert len(idx.scan()) == 301
    first = time.perf_counter() - t0
    t0 = time.perf_counter()
    idx.scan()
    cached = time.perf_counter() - t0
    assert cached < first and cached < 1.0, (first, cached)


def test_filters(tmp_path: Path):
    root = tmp_path / "recs"
    a = read_info(processed(root, name="2026-11-19_1400_A", title="Jour fixe Vertrieb").folder)
    b = read_info(processed(root, name="2026-11-18_1400_B", title="Kunde Müller", summary=False, unassigned=2).folder)
    infos = [a, b]
    assert [i.title for i in filter_infos(infos, "müller")] == ["Kunde Müller"]
    assert [i.title for i in filter_infos(infos, "anna jour")] == ["Jour fixe Vertrieb"]  # participant + title
    assert [i.title for i in filter_infos(infos, flt="no_summary")] == ["Kunde Müller"]
    assert [i.title for i in filter_infos(infos, flt="speakers_open")] == ["Kunde Müller"]
    assert [i.title for i in filter_infos(infos, flt="not_handed_over")] == ["Jour fixe Vertrieb"]
    assert filter_infos(infos, flt="changed") == []


def test_fmt_duration():
    assert [fmt_duration(x) for x in (0, 42, 60, 2820, 3600 + 25 * 60)] == ["–", "42 s", "1 min", "47 min", "1:25 h"]


def test_rename_keeps_folder_and_logs_history(tmp_path: Path):
    rec = processed(tmp_path / "recs")
    assert rename_recording(rec, "  Neuer   Titel ") == "Neuer Titel"
    assert rec.folder.name == "2026-11-19_1400_Projekt-Jour-fixe" and rec.title() == "Neuer Titel"
    assert rec.load_meta()["title_history"][0]["from"] == "Projekt Jour fixe"
    with pytest.raises(LibraryError):
        rename_recording(rec, "   ")


def test_delete_guards_and_modes(tmp_path: Path):
    root = tmp_path / "recs"
    rec = processed(root)
    outside = make_recording(tmp_path / "elsewhere", with_parts=False)
    with pytest.raises(LibraryError, match="nicht im Aufnahmeordner"):
        delete_recording(outside, root, permanent=True)
    recycled: list[Path] = []

    def recycler(p: Path) -> None:
        recycled.append(p)
        import shutil

        shutil.rmtree(p)

    assert delete_recording(rec, root, recycler=recycler) == "recycled" and recycled == [rec.folder.resolve()]
    rec2 = processed(root)
    with pytest.raises(LibraryError, match="nicht ausgeführt"):
        delete_recording(rec2, root, recycler=lambda p: None)  # recycle bin "did nothing"
    (rec2.folder / "audio").mkdir()
    ro = rec2.folder / "audio" / "mic.flac"
    ro.write_bytes(b"x")
    ro.chmod(0o400)
    assert delete_recording(rec2, root, permanent=True) == "deleted" and not rec2.folder.exists()


# ---------------------------------------------------------------------------
# queue + controller
# ---------------------------------------------------------------------------
class BlockingPipeline:
    def __init__(self, gate: threading.Event, seen: list) -> None:
        self.gate, self.seen = gate, seen

    def run(self, rec, steps, progress, language=None, should_cancel=None):
        self.seen.append((rec.folder.name, frozenset(steps), language))
        progress("transcribe", 0.25, "Spur mic")
        self.gate.wait(5)
        if should_cancel is not None and should_cancel():
            raise ProcessingCancelled()
        return ProcessResult(steps_done=list(steps))


def test_queue_snapshot_cancel_pending_and_running(tmp_path: Path):
    from callrec.jobs import CANCELLED, DONE, RUNNING, ProcessingQueue

    gate = threading.Event()
    seen: list = []
    presets: list = []

    def factory(preset=None):
        presets.append(preset)
        return BlockingPipeline(gate, seen)

    q = ProcessingQueue(factory)
    finished: list = []
    q.add_listener(lambda j: finished.append((j.id, j.state)))
    a = Recording(tmp_path / "A")
    b = Recording(tmp_path / "B")
    c = Recording(tmp_path / "C")
    ja = q.submit(a, {"transcribe"}, language="en", preset="Genau")
    jb = q.submit(b, {"summarize"})
    jc = q.submit(c, {"summarize"})
    for _ in range(100):
        snap = q.snapshot()
        if snap.current is not None and snap.current.step:
            break
        time.sleep(0.02)
    assert snap.current is not None and snap.current.id == ja.id and snap.current.state == RUNNING
    assert snap.current.status_line() == "Transkription 25 % (Spur mic) – A"
    assert [j.id for j in snap.pending] == [jb.id, jc.id]
    assert q.is_busy_with(b.folder) and q.is_busy_with(a.folder)
    assert "entfernt" in q.cancel(jb.id)
    assert not q.is_busy_with(b.folder)
    assert "Abbruch angefordert" in q.cancel(ja.id)  # no Whisper worker running in this test
    gate.set()
    for _ in range(200):
        if len(finished) == 3:
            break
        time.sleep(0.02)
    states = dict(finished)
    assert states == {jb.id: CANCELLED, ja.id: CANCELLED, jc.id: DONE}
    assert [s[0] for s in seen] == ["A", "C"] and seen[0][2] == "en"
    assert presets == ["Genau", None]
    recent = q.snapshot().recent
    assert recent[0].id == jc.id and q.cancel() == "Gerade läuft keine Verarbeitung."
    q.stop()


def test_pipeline_should_cancel_marks_meta_cancelled(tmp_path: Path, profiles):  # noqa: F811
    from tests.test_pipeline import make_pipeline

    rec = make_recording(tmp_path / "r")
    pipeline = make_pipeline(profiles)[0]
    with pytest.raises(ProcessingCancelled):
        pipeline.run(rec, {"finalize", "transcribe"}, should_cancel=lambda: True)
    assert rec.load_meta()["processing"]["status"] == "cancelled"
    from callrec.recording import find_unprocessed

    assert find_unprocessed(tmp_path / "r") == []  # a cancelled job is not resumed at the next start


def test_render_persists_speaker_counts(tmp_path: Path, profiles):  # noqa: F811
    from tests.test_pipeline import make_pipeline

    rec = make_recording(tmp_path / "r")
    make_pipeline(profiles)[0].run(rec)
    sp = rec.load_meta()["processing"]["speakers"]
    assert set(sp) >= {"spoken", "unassigned"}
    assert read_info(rec.folder).speakers_unassigned == sp["unassigned"]


@pytest.fixture
def profiles(tmp_path: Path):
    from callrec.speakers.profiles import PlainFilePersistence, ProfileStore

    return ProfileStore(PlainFilePersistence(tmp_path / "vp.json"))


def test_controller_operations_and_busy_guard(tmp_path: Path):
    from callrec.calendar.service import CalendarService
    from callrec.controller import Controller
    from callrec.jobs import ProcessingQueue
    from callrec.models import Person
    from tests.test_controller import FakeBackend, FakeUi, _decision

    gate = threading.Event()
    seen: list = []
    cfg = Config()
    cfg.general.recordings_dir = str(tmp_path / "recs")
    q = ProcessingQueue(lambda preset=None: BlockingPipeline(gate, seen))
    ctl = Controller(cfg, FakeUi(_decision), FakeBackend, CalendarService(None, tmp_path / "c.json", timedelta(0), timedelta(0)),
                     q, lambda: Person("Ich"))
    rec = processed(tmp_path / "recs")
    rec.save_utterances([])
    ctl.retranscribe(rec, language="de", preset="Genau")
    for _ in range(100):
        if seen:
            break
        time.sleep(0.02)
    assert ctl.is_busy(rec)
    with pytest.raises(LibraryError, match="verarbeitet"):
        ctl.rename(rec, "X")
    with pytest.raises(LibraryError):
        ctl.delete(rec, permanent=True)
    gate.set()
    for _ in range(200):
        if not ctl.is_busy(rec):
            break
        time.sleep(0.02)
    assert ctl.rename(rec, "Neuer Name") == "Neuer Name"  # queues a re-render for the transcript header
    for _ in range(200):
        if len(seen) == 2 and not ctl.is_busy(rec):
            break
        time.sleep(0.02)
    assert seen[0] == (rec.folder.name, frozenset({"transcribe", "identify", "render", "summarize"}), "de")
    assert seen[1][1] == frozenset({"render"})
    assert ctl.delete(rec, permanent=True) == "deleted"
    q.stop()


def test_controller_live_state_while_recording(tmp_path: Path):
    from callrec.calendar.service import CalendarService
    from callrec.controller import Controller
    from callrec.models import Person
    from tests.test_controller import FakeBackend, FakeQueue, FakeUi, _decision

    cfg = Config()
    cfg.general.recordings_dir = str(tmp_path / "recs")
    ctl = Controller(cfg, FakeUi(_decision), FakeBackend, CalendarService(None, tmp_path / "c.json", timedelta(0), timedelta(0)),
                     FakeQueue(), lambda: Person("Ich"))  # type: ignore[arg-type]
    assert ctl.levels() == {"mic": 0.0, "remote": 0.0} and ctl.elapsed() == 0.0 and ctl.current_title() == ""
    assert ctl.start()
    assert ctl.current_title() == "Spontan" and ctl.elapsed() >= 0.0
    ctl.recorder._writers["mic"].level = 0.5  # type: ignore[union-attr]
    ctl.recorder._writers["mic"].last_data_t = time.monotonic()  # type: ignore[union-attr]
    assert ctl.levels()["mic"] == 0.5
    ctl.stop()


def test_peak_level():
    from callrec.audio.capture import peak_level

    assert peak_level(np.array([], dtype=np.int16)) == 0.0
    assert peak_level(np.array([0, -16384, 100], dtype=np.int16)) == 0.5
    assert peak_level(np.array([-32768], dtype=np.int16)) == 1.0


def test_calendar_candidates(tmp_path: Path):
    from callrec.calendar.service import CalendarService

    now = datetime(2026, 11, 19, 13, 5, tzinfo=UTC)
    a = make_event("Projekt Jour fixe", start=now - timedelta(minutes=5), is_online=True)
    b = make_event("Kunde", id="b", start=now - timedelta(minutes=30))
    c = make_event("Später", id="c", start=now + timedelta(hours=3))

    class Src:
        fail = False

        def calendar_view(self, s, e):
            if self.fail:
                raise RuntimeError("offline")
            return [b, a, c, a]

    src = Src()
    svc = CalendarService(src, tmp_path / "cache.json", timedelta(minutes=10), timedelta(minutes=10), clock=lambda: now)
    events, msg, source = svc.candidates()
    assert [e.subject for e in events] == ["Projekt Jour fixe", "Kunde"] and source == "graph" and msg == ""
    src.fail = True
    events, msg, source = svc.candidates()
    assert source == "cache" and len(events) == 2 and "offline" in msg
    none = CalendarService(None, tmp_path / "x.json", timedelta(0), timedelta(0))
    assert none.candidates()[0] == [] and "optional" in none.candidates()[1]


# ---------------------------------------------------------------------------
# setup checklist + hints
# ---------------------------------------------------------------------------
def test_setup_checks_fresh_install_is_usable_without_microsoft_and_sharepoint():
    cfg = Config()
    dev = SimpleNamespace(name="Headset")
    items = setupcheck.run_all(cfg, None, resolve=lambda m, lb: (dev, dev), selected=lambda: ("large-v3-turbo", ""),
                               find=lambda m: SimpleNamespace(complete=True), key_present=lambda: False,
                               signed_in=lambda: None)
    states = {i.key: i.state for i in items}
    assert states == {"audio": "ok", "whisper": "ok", "summary": "unknown", "microsoft": "optional",
                      "sharepoint": "optional", "autostop": "ok"}
    ready, text = setupcheck.summary_line(items)
    assert ready and "Optional noch offen" in text and "Microsoft" in text
    ms = next(i for i in items if i.key == "microsoft")
    assert "funktioniert auch ohne" in ms.detail and (setupcheck.A_SETTINGS_MICROSOFT in dict(ms.actions).values())


def test_setup_checks_errors_and_fix_actions(tmp_path: Path):
    cfg = Config()
    cfg.summary.provider = "anthropic"
    cfg.calendar.client_id = "abc"
    cfg.handoff.folder = str(tmp_path / "fehlt")

    def broken(m, lb):
        raise RuntimeError("kein Mikrofon")

    items = setupcheck.run_all(cfg, None, resolve=broken, selected=lambda: ("large-v3-turbo", "CPU"),
                               find=lambda m: None, key_present=lambda: True, signed_in=lambda: None)
    by = {i.key: i for i in items}
    assert by["audio"].state == "error" and "kein Mikrofon" in by["audio"].detail
    assert by["whisper"].state == "warn" and ("Jetzt herunterladen", setupcheck.A_DOWNLOAD_WHISPER) in by["whisper"].actions
    assert by["summary"].state == "error" and "nicht bestätigt" in by["summary"].detail
    assert by["microsoft"].state == "warn" and ("Bei Microsoft anmelden …", setupcheck.A_LOGIN) in by["microsoft"].actions
    assert by["sharepoint"].state == "error" and "nicht erreichbar" in by["sharepoint"].detail
    ready, text = setupcheck.summary_line(items)
    assert not ready and "Audiogeräte" in text
    cfg.summary.anthropic_transcripts_leave_this_computer_confirmed = True
    ok = setupcheck.check_summary(cfg, lambda: True, "Verbindung zu Anthropic OK", True)
    assert ok.state == "ok"
    # sharepoint folder that exists but is not synced -> warning, synced -> ok
    cfg.handoff.folder = str(tmp_path)
    assert setupcheck.check_sharepoint(cfg, synced=lambda p: None).state == "warn"
    assert setupcheck.check_sharepoint(cfg, synced=lambda p: p).state == "ok"


def test_probe_summary_provider_local_and_anthropic():
    cfg = Config()

    class Local:
        def list_models(self):
            return ["gemma3:12b"]

    assert setupcheck.probe_summary_provider(cfg, lambda s: Local())[1] is True
    cfg.summary.model = "llama3"
    text, ok = setupcheck.probe_summary_provider(cfg, lambda s: Local())
    assert not ok and "fehlt" in text
    cfg.summary.provider = "anthropic"

    class Claude:
        def test_connection(self):
            raise RuntimeError("Zeitüberschreitung bei der Verbindung zu api.anthropic.com. VPN …")

    text, ok = setupcheck.probe_summary_provider(cfg, lambda s: Claude())
    assert not ok and "Zeitüberschreitung" in text


@pytest.mark.parametrize("text,action", [
    ("Zeitüberschreitung bei der Verbindung zu api.anthropic.com. Ein aktives VPN …", hints.RESUMMARIZE),
    ("Keine Verbindung zu api.anthropic.com (Internet, Proxy oder Firewall prüfen).", hints.RESUMMARIZE),
    ("Der API-Schlüssel ist ungültig (401).", hints.SETTINGS_SUMMARY),
    ("KI-Server nicht erreichbar unter http://127.0.0.1:11434 (ConnectError).", hints.RESUMMARIZE),
    ("Übergabeordner existiert nicht: C:\\x", hints.SETTINGS_SHAREPOINT),
    ("Keine fertigen Audiodateien in X (Schritt 'finalize' ausführen)", hints.REPROCESS),
    ("Audiogeräte nicht verfügbar: kein Gerät", hints.SETTINGS_GENERAL),
    ("irgendwas Unerwartetes", hints.OPEN_LOG),
])
def test_hints(text: str, action: str):
    h = hints.explain(text)
    assert h.action == action and h.next_step and h.message == text


def test_vpn_hint_added_only_when_missing():
    h = hints.explain("Anthropic: Zeitüberschreitung (aufgegeben nach 4 Versuch(en))")
    assert "VPN" in h.next_step and "Zusammenfassung neu erstellen" in h.next_step
    h2 = hints.explain("Zeitüberschreitung bei api.anthropic.com. Ein aktives VPN (z. B. NordVPN) …")
    assert "VPN" not in h2.next_step


# ---------------------------------------------------------------------------
# view helpers
# ---------------------------------------------------------------------------
def test_markdown_spans_summary_structure():
    md = ("# Zusammenfassung: Jour fixe\n\n_Automatisch lokal erstellt._\n\n## Kurzfassung\nRelease am **1.12.**\n\n"
          "## Aufgaben\n| Aufgabe | Verantwortlich | Frist |\n|---|---|---|\n| Angebot schicken | Anna | 1.12. |\n\n"
          "- Punkt mit `code`\n  - Unterpunkt\n\n---\n> Zitat\n")
    spans = vm.markdown_spans(md)
    text = vm.plain_text(spans)
    assert "#" not in text and "**" not in text and "|---" not in text
    assert ("Zusammenfassung: Jour fixe", ("h1",)) in spans and ("Kurzfassung", ("h2",)) in spans
    assert ("1.12.", ("bold",)) in spans and ("Automatisch lokal erstellt.", ("italic",)) in spans
    table = [t for t, tags in spans if "table" in tags]
    assert table[0].startswith("Aufgabe") and "Anna" in table[1] and len(table) == 2
    assert ("code", ("bullet", "code")) in spans and ("• ", ("bullet2",)) in spans
    assert any("─" in t for t, _ in spans) and ("Zitat", ("quote",)) in spans
    assert "\n\n\n" not in text


def test_markdown_spans_transcript_line_and_snake_case():
    spans = vm.markdown_spans("**[00:00:01] Anna Beispiel:** Wir nutzen file_name_x und 2*3*4.\n")
    assert spans[0] == ("[00:00:01] Anna Beispiel:", ("bold",))
    assert "file_name_x" in vm.plain_text(spans) and "2*3*4" in vm.plain_text(spans)


def test_geometry_clamp_and_defaults():
    assert vm.parse_geometry("1100x700+10+20") == (1100, 700, 10, 20)
    assert vm.parse_geometry("1100x700+-5+20") == (1100, 700, -5, 20)
    assert vm.parse_geometry("kaputt") is None
    # a window remembered on a big monitor, now on a 1366x768 laptop
    assert vm.clamp_geometry((1900, 1100, 2400, 900), 1366, 768, 760, 500) == (1366, 608, 0, 0)
    assert vm.clamp_geometry((900, 600, -300, -50), 1366, 768, 760, 500) == (900, 600, 0, 0)
    w, h, x, y = vm.default_geometry(1366, 768)
    # client area + title bar (31) + menu bar (20) must end above a 48-px taskbar
    assert w <= 1326 and y + 31 + 20 + h <= 768 - 48 and x >= 0 and y >= 0
    w, h, x, y = vm.default_geometry(1920, 1080, scale=1.5)  # 150 %: 1120*1.5 > 1880 -> capped
    assert w == 1680 and h == 840 and y + int((31 + 20) * 1.5) + h <= 1080 - 72


def test_window_state_roundtrip(tmp_path: Path):
    p = tmp_path / "w.json"
    assert vm.WindowState.load(p) == vm.WindowState()
    vm.WindowState("800x600+1+2", True, 420, "Einrichtung", "problems").save(p)
    assert vm.WindowState.load(p) == vm.WindowState("800x600+1+2", True, 420, "Einrichtung", "problems")
    p.write_text("{nope", encoding="utf-8")
    assert vm.WindowState.load(p) == vm.WindowState()


def test_version_choices(tmp_path: Path):
    for n in (1, 2):
        (tmp_path / f"summary.v{n}.md").write_text("x", encoding="utf-8")
    versions = [{"version": 1, "file": "summary.v1.md", "created": "2026-11-19T15:00:00+01:00",
                 "settings": {"llm": {"model": "gemma3:12b"}}},
                {"version": 2, "file": "summary.v2.md", "created": "2026-11-20T09:30:00+01:00",
                 "settings": {"llm": {"provider": "anthropic", "model": "claude-opus-5-5"}}},
                {"version": 3, "file": "summary.v3.md"}]  # file missing -> skipped
    ch = vm.version_choices(tmp_path, "summary", versions)
    assert [c.label for c in ch] == ["Version 2 (neueste) – 20.11.2026 09:30, claude-opus-5-5",
                                     "Version 1 – 19.11.2026 15:00, gemma3:12b"]
    (tmp_path / "transcript.md").write_text("x", encoding="utf-8")
    assert [c.label for c in vm.version_choices(tmp_path, "transcript", [])] == ["aktuelle Fassung"]
    assert vm.version_choices(tmp_path / "leer", "transcript", []) == []


def test_cli_list_uses_library(tmp_path: Path, capsys, monkeypatch):
    from callrec import cli
    from callrec.config import save_config

    cfg = Config()
    cfg.general.recordings_dir = str(tmp_path / "recs")
    save_config(cfg)
    processed(tmp_path / "recs", unassigned=1)
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "2026-11-19_1400_Projekt-Jour-fixe" in out and "Sprecher 1 offen" in out and "Zusammenfassung ✓" in out
