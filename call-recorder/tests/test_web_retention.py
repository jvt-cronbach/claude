"""Web UI: deleting (scopes, multi-select), "Behalten" and the pre-notice through the bridge (0.3.1)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from callrec import retention
from callrec.recording import Recording
from callrec.ui.apploop import AppLoop
from tests.test_mainwindow_logic import processed
from tests.test_web_bridge import Core


@pytest.fixture
def loop():
    lp = AppLoop()
    lp.start()
    yield lp
    lp.stop()


def core_with_actions(tmp_path: Path, loop: AppLoop) -> Any:
    core = Core(tmp_path, loop)
    root = core.cfg.recordings_path()

    def remove(rec: Recording, scope: str, permanent: bool = True) -> str:
        return retention.remove(rec, root, scope, "permanent" if permanent else "recycle_bin")

    core.actions = SimpleNamespace(
        delete=lambda rec, permanent=False: retention.remove(rec, root, "recording", "permanent"),
        remove=remove, set_pinned=lambda rec, on: retention.set_pinned(rec, on),
        retention_plan=lambda: retention.plan(root, core.cfg))
    return core


def test_bridge_delete_scopes_multi_and_pin(loop: AppLoop, tmp_path: Path):
    from callrec.ui.bridge import Bridge

    root = tmp_path / "recs"
    a = processed(root, "2026-11-19_1400_A", "A")
    b = processed(root, "2026-11-19_1500_B", "B")
    (a.folder / "audio").mkdir()
    (a.folder / "audio" / "mic.flac").write_bytes(b"x")
    br = Bridge(core_with_actions(tmp_path, loop))
    info = br.delete_info("2026-11-19_1400_A", "audio_transcript")
    assert info["permanent"] is True and "ENDGÜLTIG" in info["message"] and "Zusammenfassung bleibt" in info["title"]
    assert "error" in br.delete_info("2026-11-19_1400_A", "everything")
    assert br.delete("2026-11-19_1400_A", True, "audio_transcript")["done"] == 1
    assert not a.transcript_path.exists() and a.summary_path.exists() and not (a.folder / "audio").exists()
    det = br.get_recording("2026-11-19_1400_A")
    assert det["reduced"] and det["actions"]["handoff"] and not det["actions"]["resummarize"]
    assert "gelöscht" in det["disabled_why"]["resummarize"]
    assert br.pin(["2026-11-19_1500_B"], True)["ok"] and retention.is_pinned(b.load_meta())
    lst = br.list_recordings()
    pinned = {r["id"]: r["pinned"] for r in lst["items"]}
    assert pinned == {"2026-11-19_1400_A": False, "2026-11-19_1500_B": True}
    multi = br.delete_info(["2026-11-19_1400_A", "2026-11-19_1500_B"], "recording")
    assert "2 Aufnahmen" in multi["message"] and "Behalten" in multi["message"]
    assert br.delete(["2026-11-19_1400_A", "2026-11-19_1500_B"], True, "recording")["done"] == 2
    assert not a.folder.exists() and not b.folder.exists()
    assert "error" in br.delete(["../x"], True, "recording")


def test_bridge_retention_overview_marks_shown(loop: AppLoop, tmp_path: Path):
    from datetime import datetime, timedelta

    from callrec.ui.bridge import Bridge

    rec = processed(tmp_path / "recs", "2026-11-19_1400_A", "A")
    rec.update_meta(["recording", "started_at"], (datetime.now().astimezone() - timedelta(days=30)).isoformat())
    br = Bridge(core_with_actions(tmp_path, loop))
    ov = br.retention_overview()
    assert ov["enabled"] and ov["items"] and ov["blocked"] == "first" and ov["permanent"]
    assert "2026-11-19_1400_A" in retention.load_state()["surfaced"]


def test_clip_unpublish_answers_404(tmp_path: Path):
    from callrec.ui.webserver import UiServer
    from tests.test_web_bridge import _get

    s = UiServer()
    s.start()
    try:
        clip = tmp_path / "c.wav"
        clip.write_bytes(b"RIFF")
        url = s.publish(clip)
        assert _get(url)[0] == 200
        s.unpublish(clip)
        assert _get(url)[0] == 404
    finally:
        s.stop()


def test_hidden_window_does_not_count_as_shown_and_retention_now_takes_only_announced(loop: AppLoop, tmp_path: Path):
    """Start in the tray: the page loads in the hidden window - that is no "shown as wird gelöscht".
    "Jetzt löschen" deletes only what is still announced (not a recording pinned meanwhile)."""
    from datetime import datetime, timedelta

    from callrec.ui.bridge import Bridge

    old = datetime.now().astimezone() - timedelta(days=30)
    a = processed(tmp_path / "recs", "2026-11-19_1400_A", "A")
    b = processed(tmp_path / "recs", "2026-11-19_1500_B", "B")
    for r in (a, b):
        r.update_meta(["recording", "started_at"], old.isoformat())
    core = core_with_actions(tmp_path, loop)
    core.windows = SimpleNamespace(main_visible=False)
    br = Bridge(core)
    assert br.retention_overview()["items"]
    assert "surfaced" not in retention.load_state() or not retention.load_state()["surfaced"]
    core.windows.main_visible = True
    br.retention_overview()
    assert "2026-11-19_1400_A" in retention.load_state()["surfaced"]
    retention.set_pinned(b, True)  # pinned in the other window after the notice was loaded
    res = br.retention_now(["2026-11-19_1400_A", "2026-11-19_1500_B"])
    assert res["done"] == 1 and b.transcript_path.exists() and not a.transcript_path.exists()


def test_retention_now_skips_what_is_processed_or_open_in_a_dialog(loop: AppLoop, tmp_path: Path):
    from datetime import datetime, timedelta

    from callrec.ui.bridge import Bridge

    old = datetime.now().astimezone() - timedelta(days=30)
    a = processed(tmp_path / "recs", "2026-11-19_1400_A", "A")
    b = processed(tmp_path / "recs", "2026-11-19_1500_B", "B")
    for r in (a, b):
        r.update_meta(["recording", "started_at"], old.isoformat())
    b.update_meta(["processing", "chain"], {"stage": "review"})  # the speaker dialog of the chain is open
    core = core_with_actions(tmp_path, loop)
    ov = Bridge(core).retention_overview()  # the confirmation counts only what "Jetzt löschen" takes
    assert set(ov["next24"]) == {"2026-11-19_1400_A", "2026-11-19_1500_B"} and ov["next24_now"] == ["2026-11-19_1400_A"]
    res = Bridge(core).retention_now(["2026-11-19_1400_A", "2026-11-19_1500_B"])
    assert res["done"] == 1 and not a.transcript_path.exists() and b.transcript_path.exists()
