"""Deleting recordings and the automatic retention (0.3.1)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from callrec import retention
from callrec.config import Config
from callrec.library import LibraryError, read_info
from callrec.recording import Recording
from callrec.speakers.profiles import PlainFilePersistence, ProfileStore
from callrec.ui.viewmodel import action_states, delete_question, next_action
from tests.conftest import BERLIN, make_recording
from tests.test_pipeline import make_pipeline, recording_with_voices

START = datetime(2026, 11, 19, 14, 0, tzinfo=BERLIN)


def processed(root: Path, name: str, days_old: float, now: datetime) -> Recording:
    rec = recording_with_voices(root / name, 330, 550)
    make_pipeline(ProfileStore(PlainFilePersistence(root / f"{name}.vp.json")))[0].run(rec)
    rec.update_meta(["recording", "started_at"], (now - timedelta(days=days_old)).isoformat())
    return rec


def library(tmp_path: Path, now: datetime, ages: list[float]) -> tuple[Path, list[Recording]]:
    root = tmp_path / "lib"
    recs = []
    for i, age in enumerate(ages):
        rec = processed(tmp_path / f"src{i}", "r", age, now)
        dst = root / f"{i:02d}_{rec.folder.name}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        rec.folder.rename(dst)
        recs.append(Recording(dst))
    return root, recs


def remover(root: Path, log: list[tuple[str, str]]) -> Any:
    def go(rec: Recording, scope: str, mode: str) -> str:
        log.append((rec.folder.name, scope))
        return retention.remove(rec, root, scope, mode, by="retention")
    return go


def test_remove_audio_transcript_keeps_summary_and_strips_meta(tmp_path: Path):
    now = START + timedelta(days=30)
    root, (rec,) = library(tmp_path, now, [10])
    rec.update_meta(["handoffs"], [{"at": "x", "location": "SharePoint"}])
    retention.remove(rec, root, "audio_transcript", "permanent")
    names = {p.relative_to(rec.folder).as_posix() for p in rec.folder.rglob("*")}
    assert names == {"meta.json", "summary.md", "summary.v1.md"}
    meta = rec.load_meta()
    assert meta["retention"]["removed"] == "audio_transcript" and meta["retention"]["mode"] == "permanent"
    assert "transcription" not in meta["processing"] and "versions" in meta and "transcript" not in meta["versions"]
    assert meta["handoffs"] and meta["participants"] and meta["consent"] and meta["processing"]["summary"]["model"]
    assert "erste Stimme" not in rec.meta_path.read_text(encoding="utf-8")
    info = read_info(rec.folder)
    assert info.reduced and "Nur Zusammenfassung (Audio und Transkript gelöscht am" in info.removed_text
    st = action_states(info, False, False, True)
    assert st["handoff"] and not st["resummarize"] and not st["review"] and not st["retranscribe"] and st["delete"]
    assert next_action(info)[0].startswith("Nur Zusammenfassung")
    # no empty summary from a deleted transcript
    with pytest.raises(FileNotFoundError, match="gelöscht"):
        make_pipeline(ProfileStore(PlainFilePersistence(tmp_path / "x.json")))[0].run(rec, {"summarize"})
    assert rec.summary_path.exists()
    # "Löschen" then removes the rest
    retention.remove(rec, root, "recording", "permanent")
    assert not rec.folder.exists()
    entries = retention.read_log()
    assert [e["scope"] for e in entries] == ["audio_transcript", "recording"]
    assert all("Stimme" not in str(e) for e in entries)  # no content in the log


def test_remove_audio_only_and_recycle_bin_mode(tmp_path: Path):
    now = START + timedelta(days=30)
    root, (rec,) = library(tmp_path, now, [10])
    moved: list[Path] = []
    retention.remove(rec, root, "audio", "recycle_bin", recycler=lambda p: (moved.append(p), _rm(p)))
    assert [p.name for p in moved] == ["audio"] and rec.transcript_path.exists() and rec.summary_path.exists()
    info = read_info(rec.folder)
    assert not info.has_audio and info.removed_text.startswith("Audio gelöscht")
    st = action_states(info, False, False, True)
    assert not st["retranscribe"] and not st["play"] and st["review"] and st["resummarize"]
    with pytest.raises(FileNotFoundError, match="Audio"):
        make_pipeline(ProfileStore(PlainFilePersistence(tmp_path / "x.json")))[0].run(rec, {"transcribe"})


def _rm(p: Path) -> None:
    import shutil

    shutil.rmtree(p) if p.is_dir() else p.unlink()


def test_outside_the_library_is_never_deleted(tmp_path: Path):
    rec = make_recording(tmp_path / "elsewhere")
    with pytest.raises(LibraryError):
        retention.remove(rec, tmp_path / "lib", "audio_transcript", "permanent")


def test_age_boundary_pins_and_pre_notice(tmp_path: Path):
    now = START + timedelta(days=30)
    root, recs = library(tmp_path, now, [7.01, 6.99, 5.5, 1, 20])
    retention.acknowledge()
    retention.set_pinned(recs[4], True)
    cfg = Config()
    p = retention.plan(root, cfg, now)
    assert [i.rec.folder for i in p.removable] == [recs[0].folder]  # 20 days old but pinned
    assert {i.rec.folder for i in p.upcoming} == {recs[1].folder, recs[2].folder}
    texts = {i.rec.folder: i.when_text(now) for i in p.upcoming}
    assert texts[recs[1].folder] == "wird in den nächsten 24 h gelöscht" and texts[recs[2].folder] == "wird in 2 Tagen gelöscht"
    assert p.notice(now) == "2 Aufnahmen werden in den nächsten 24 h gelöscht"  # due now + within 24 h
    assert retention.notify_due_today(p, now) and retention.notify_due_today(p, now) == ""  # once per day
    log: list[tuple[str, str]] = []
    res = retention.run(root, cfg, remover(root, log), now=now)
    assert log == [(recs[0].folder.name, "audio_transcript")] and res.removed == [recs[0].folder.name]
    assert recs[4].transcript_path.exists() and recs[0].summary_path.exists() and not recs[0].transcript_path.exists()


def test_busy_and_unfinished_recordings_wait(tmp_path: Path):
    now = START + timedelta(days=30)
    root, recs = library(tmp_path, now, [10, 10, 10, 10])
    retention.acknowledge()
    recs[1].transcript_path.unlink()  # never fully processed
    recs[2].update_meta(["processing", "chain"], {"stage": "summary"})
    cfg = Config()
    cfg.retention.max_share_per_run = 1.0
    log: list[tuple[str, str]] = []
    retention.run(root, cfg, remover(root, log), now=now, busy=lambda r: r.folder == recs[3].folder)
    assert log == [(recs[0].folder.name, "audio_transcript")]
    # shown as "wird gelöscht" for 24 h: then the unprocessed one goes too
    p = retention.plan(root, cfg, now + timedelta(hours=12), busy=lambda r: r.folder == recs[3].folder)
    assert any(i.rec.folder == recs[1].folder and "24 h" in i.waiting for i in p.waiting)
    retention.run(root, cfg, remover(root, log), now=now + timedelta(hours=25),
                  busy=lambda r: r.folder == recs[3].folder)
    assert (recs[1].folder.name, "audio_transcript") in log and len(log) == 2


def test_first_activation_asks_instead_of_deleting(tmp_path: Path):
    now = START + timedelta(days=30)
    root, recs = library(tmp_path, now, [10, 1])
    cfg = Config()
    log: list[tuple[str, str]] = []
    res = retention.run(root, cfg, remover(root, log), now=now)
    assert res.blocked == "first" and log == [] and recs[0].transcript_path.exists()
    title, msg = retention.question_text(res.blocked, res.blocked_text, [("Projekt", "")], cfg.retention)
    assert "ENDGÜLTIG" in msg and "nicht rückgängig" in msg
    # "Diese behalten (anpinnen)"
    retention.pin_all(retention.plan(root, cfg, now).removable)
    retention.acknowledge()
    assert retention.run(root, cfg, remover(root, log), now=now).removed == [] and log == []
    assert read_info(recs[0].folder).pinned


def test_first_activation_without_old_recordings_needs_no_question(tmp_path: Path):
    now = START + timedelta(days=30)
    root, recs = library(tmp_path, now, [1])
    log: list[tuple[str, str]] = []
    assert retention.run(root, Config(), remover(root, log), now=now).blocked == ""
    assert retention.load_state()["acknowledged"] is True
    res = retention.run(root, Config(), remover(root, log), now=now + timedelta(days=8))
    assert res.removed == [recs[0].folder.name]


def test_clock_safety(tmp_path: Path):
    now = START + timedelta(days=30)
    root, recs = library(tmp_path, now, [10, 10, 10, -5])  # one recording "from the future"
    retention.acknowledge()
    log: list[tuple[str, str]] = []
    res = retention.run(root, Config(), remover(root, log), now=now)
    assert res.blocked == "future" and log == []
    assert retention.run(root, Config(), remover(root, log), now=now, confirmed=True).blocked == "future"
    root2, _ = library(tmp_path / "b", now, [10, 10, 10, 1])
    res = retention.run(root2, Config(), remover(root2, log), now=now)
    assert res.blocked == "share" and log == []  # 3 of 4 at once: ask
    assert len(retention.run(root2, Config(), remover(root2, log), now=now, confirmed=True).removed) == 3


def test_scope_recording_and_disabled(tmp_path: Path):
    now = START + timedelta(days=30)
    root, recs = library(tmp_path, now, [10, 1])
    retention.acknowledge()
    cfg = Config()
    cfg.retention.scope = "recording"
    log: list[tuple[str, str]] = []
    retention.run(root, cfg, remover(root, log), now=now)
    assert not recs[0].folder.exists() and recs[1].folder.exists()
    cfg.retention.enabled = False
    assert retention.plan(root, cfg, now + timedelta(days=30)).removable == []


def test_controller_remove_is_the_one_path_and_refuses_busy(tmp_path: Path):
    from types import SimpleNamespace

    from callrec.controller import Controller

    now = START + timedelta(days=30)
    root, (rec,) = library(tmp_path, now, [10])
    calls: list[Any] = []
    fake = SimpleNamespace(is_busy=lambda r: True, cfg=SimpleNamespace(recordings_path=lambda: root))
    with pytest.raises(LibraryError):
        Controller.remove(fake, rec, "audio", "permanent")  # type: ignore[arg-type]
    fake.is_busy = lambda r: False
    fake.remove = lambda *a, **k: calls.append(a) or "endgültig gelöscht"
    assert Controller.delete(fake, rec, permanent=True) == "deleted"  # type: ignore[arg-type]
    assert calls == [(rec, "recording", "permanent")]


def test_delete_question_names_what_goes_and_sharepoint():
    from callrec.library import RecordingInfo

    info = RecordingInfo(Path("x"), "Jour fixe", handoff={"at": "1"})
    title, msg, button = delete_question([info], "audio_transcript", True)
    assert "Zusammenfassung bleibt" in title and "ENDGÜLTIG" in msg and "SharePoint" in msg and button == "Endgültig löschen"
    _t, msg2, b2 = delete_question([info, info], "recording", False)
    assert "2 Aufnahmen" in msg2 and "Papierkorb" in msg2 and b2 == "In den Papierkorb"


def test_cli_retention_status_pin_run(tmp_path: Path):
    import argparse

    from callrec import paths
    from callrec.cli import build_parser, cmd_retention

    now = datetime.now().astimezone()
    root, (old, new) = library(tmp_path, now, [10, 1])
    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(f'[general]\nrecordings_dir = "{root.as_posix()}"\n[consent]\nmode = "ask"\n',
                                   encoding="utf-8")
    assert build_parser().parse_args(["retention", "pin", "x"]).action == "pin"
    out: list[str] = []
    assert cmd_retention(argparse.Namespace(action="status", folder=None, yes=False), out.append) == 0
    assert any("Beim nächsten Lauf" in line and old.folder.name in line for line in out)
    assert any("Rückfrage" in line for line in out)  # first activation
    assert cmd_retention(argparse.Namespace(action="run", folder=None, yes=False), out.append) == 1
    assert old.transcript_path.exists()
    assert cmd_retention(argparse.Namespace(action="pin", folder=new.folder.name, yes=False), out.append) == 0
    assert retention.is_pinned(new.load_meta())
    assert cmd_retention(argparse.Namespace(action="run", folder=None, yes=True), out.append) == 0
    assert not old.transcript_path.exists() and old.summary_path.exists() and new.transcript_path.exists()
    assert cmd_retention(argparse.Namespace(action="unpin", folder=new.folder.name, yes=False), out.append) == 0
    assert not retention.is_pinned(new.load_meta())
