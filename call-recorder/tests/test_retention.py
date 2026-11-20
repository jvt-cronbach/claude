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
    # nor speaker data written back into the reduced folder (CLI "callrec assign", an open dialog)
    from callrec.review import Assignment, apply_assignments

    with pytest.raises(FileNotFoundError, match="gelöscht"):
        apply_assignments(rec, [Assignment("S1", "Anna")], ProfileStore(PlainFilePersistence(tmp_path / "y.json")),
                          lambda: None)
    assert not (rec.folder / "data").exists()
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
    # the runs alone (nobody saw the notice): the unprocessed one stays, however often it runs
    for h in (25, 49, 73):
        retention.run(root, cfg, remover(root, log), now=now + timedelta(hours=h),
                      busy=lambda r: r.folder == recs[3].folder)
    assert len(log) == 1
    # shown as "wird gelöscht" (main window / notification) for 24 h: then the unprocessed one goes too
    shown = now + timedelta(hours=80)
    retention.mark_surfaced(retention.plan(root, cfg, shown), shown)
    p = retention.plan(root, cfg, shown + timedelta(hours=12), busy=lambda r: r.folder == recs[3].folder)
    assert any(i.rec.folder == recs[1].folder and "24 h" in i.waiting for i in p.waiting)
    retention.run(root, cfg, remover(root, log), now=shown + timedelta(hours=25),
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


def test_first_activation_without_old_recordings_asks_at_the_first_deletion(tmp_path: Path):
    """An update brings retention to a library of recent recordings: nothing is due now, so nothing
    is asked - but the very first deletion still needs the user's answer."""
    now = START + timedelta(days=30)
    root, recs = library(tmp_path, now, [1])
    log: list[tuple[str, str]] = []
    assert retention.run(root, Config(), remover(root, log), now=now).blocked == ""
    assert not retention.load_state().get("acknowledged")
    res = retention.run(root, Config(), remover(root, log), now=now + timedelta(days=8))
    assert res.blocked == "first" and log == []
    res = retention.run(root, Config(), remover(root, log), now=now + timedelta(days=8), confirmed=True,
                        only=[recs[0].folder.name])
    assert res.removed == [recs[0].folder.name]
    assert retention.load_state()["acknowledged"] is True


def test_empty_library_needs_no_question(tmp_path: Path):
    root = tmp_path / "lib"
    root.mkdir()
    assert retention.run(root, Config(), remover(root, []), now=START).blocked == ""
    assert retention.load_state()["acknowledged"] is True


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
    # a multi-selection names what it contains (items can be scrolled or filtered out of view)
    other = RecordingInfo(Path("y"), "Kundentermin Müller", started_at=START)
    _t, msg3, _b = delete_question([info, other], "recording", True)
    assert "• Jour fixe" in msg3 and "• Kundentermin Müller (19.11.2026 14:00)" in msg3


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


def test_profile_provenance_shows_deleted_and_reduced_recordings(tmp_path: Path):
    from callrec.ui.dto import provenance_state

    now = START + timedelta(days=30)
    root, (a, b) = library(tmp_path, now, [10, 10])
    assert provenance_state(root, a.folder.name) == ""
    retention.remove(a, root, "audio_transcript", "permanent")
    retention.remove(b, root, "recording", "permanent")
    assert provenance_state(root, a.folder.name) == " (nur noch Zusammenfassung)"
    assert provenance_state(root, b.folder.name) == " (Aufnahme gelöscht)"
    assert provenance_state(None, b.folder.name) == ""


# ---------------------------------------------------------------------------
# review findings (0.3.1): things the automatic deletion must never do
# ---------------------------------------------------------------------------
def light(root: Path, name: str, start: Any, *, transcript: bool = True, **extra: Any) -> Recording:
    """A small finished recording folder (no audio processing) with a free start value."""
    import json

    f = root / name
    (f / "audio").mkdir(parents=True)
    (f / "audio" / "mic.flac").write_bytes(b"x")
    (f / "data").mkdir()
    (f / "data" / "utterances.json").write_text('[{"text": "geheimer Inhalt"}]', encoding="utf-8")
    if transcript:
        (f / "transcript.md").write_text("geheimer Inhalt", encoding="utf-8")
    (f / "summary.md").write_text("Zusammenfassung", encoding="utf-8")
    meta: dict[str, Any] = {"schema": 1, "title": name, "recording": {"status": "stopped", "started_at": start},
                            "processing": {"status": "done"}, **extra}
    (f / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return Recording(f)


def iso(d: datetime) -> str:
    return d.isoformat()


def test_first_activation_with_only_held_back_old_recordings_still_asks(tmp_path: Path):
    """Old recordings that are only *waiting* (unprocessed/busy) at the first run must not count as
    "nothing old": later, when they become removable, the first deletion is still asked."""
    now = START + timedelta(days=30)
    root = tmp_path / "lib"
    old = light(root, "a", iso(now - timedelta(days=10)), transcript=False)  # never processed
    log: list[tuple[str, str]] = []
    assert retention.run(root, Config(), remover(root, log), now=now).removed == []
    assert not retention.load_state().get("acknowledged")
    shown = now + timedelta(hours=1)
    retention.mark_surfaced(retention.plan(root, Config(), shown), shown)
    res = retention.run(root, Config(), remover(root, log), now=shown + timedelta(days=2))
    assert res.blocked == "first" and log == [] and (old.folder / "data").exists()


def test_confirmed_run_removes_only_what_the_question_listed(tmp_path: Path):
    now = START + timedelta(days=30)
    root = tmp_path / "lib"
    a = light(root, "a", iso(now - timedelta(days=10)))
    b = light(root, "b", iso(now - timedelta(days=6, hours=23)))  # due an hour later
    light(root, "c", iso(now - timedelta(days=1)))
    log: list[tuple[str, str]] = []
    res = retention.run(root, Config(), remover(root, log), now=now)
    assert res.blocked == "first"
    listed = [i.id for i in retention.plan(root, Config(), now).removable]
    assert listed == ["a"]
    # the user answers two hours later: b is due by then but was not in the question
    res = retention.run(root, Config(), remover(root, log), now=now + timedelta(hours=2), confirmed=True, only=listed)
    assert res.removed == ["a"] and b.transcript_path.exists() and not a.transcript_path.exists()


def test_clock_jump_is_caught_when_most_of_the_library_is_already_reduced(tmp_path: Path):
    """After weeks of retention most recordings are summaries only: a clock that jumps a month ahead
    must still not silently take all remaining audio/transcripts at once."""
    now = START + timedelta(days=60)
    root = tmp_path / "lib"
    for i in range(20):
        light(root, f"old{i:02d}", iso(now - timedelta(days=20 + i)),
              retention={"removed": "audio_transcript", "at": iso(now)})
    fresh = [light(root, f"new{i}", iso(now - timedelta(hours=h))) for i, h in enumerate((24, 2, 5))]
    st = retention.load_state()
    st.update({"acknowledged": True, "last_run": iso(now)})
    retention.save_state(st)
    log: list[tuple[str, str]] = []
    res = retention.run(root, Config(), remover(root, log), now=now + timedelta(days=30))
    assert res.blocked == "share" and log == [] and all(r.transcript_path.exists() for r in fresh)
    # the normal daily rhythm still deletes the one that is due, without asking
    st = retention.load_state()
    st["last_run"] = iso(now + timedelta(days=5, hours=1))
    retention.save_state(st)
    res = retention.run(root, Config(), remover(root, log), now=now + timedelta(days=6, hours=1))
    assert res.blocked == "" and res.removed == ["new0"]


def test_missing_or_malformed_start_is_skipped_never_old(tmp_path: Path):
    now = START + timedelta(days=30)
    root = tmp_path / "lib"
    bad = [light(root, "n1", None), light(root, "n2", 12345), light(root, "n3", "gestern"),
           light(root, "n4", "")]
    weird = light(root, "n5", iso(now - timedelta(days=30)), processing=["kaputt"])
    naive = light(root, "n6", (now - timedelta(days=6, hours=20)).replace(tzinfo=None).isoformat())
    foreign = light(root, "n7", iso(now - timedelta(days=30)))  # a folder of another program with a meta.json
    foreign.save_meta({k: v for k, v in foreign.load_meta().items() if k != "schema"})
    retention.acknowledge()
    log: list[tuple[str, str]] = []
    res = retention.run(root, Config(), remover(root, log), now=now)
    assert res.removed == [] and log == []
    assert all(r.transcript_path.exists() for r in [*bad, weird, naive, foreign])


def test_processing_in_another_process_is_not_deleted(tmp_path: Path):
    """``callrec process`` on the command line (or a second app) - the app's own queue cannot see it."""
    now = START + timedelta(days=30)
    root = tmp_path / "lib"
    rec = light(root, "a", iso(now - timedelta(days=10)))
    rec.update_meta(["processing", "status"], "running")
    retention.acknowledge()
    p = retention.plan(root, Config(), now)
    assert p.removable == [] and p.waiting and "verarbeitet" in p.waiting[0].waiting


def test_locked_file_leaves_a_labelled_recording_that_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from callrec import library

    now = START + timedelta(days=30)
    root = tmp_path / "lib"
    rec = light(root, "a", iso(now - timedelta(days=10)))
    retention.acknowledge()
    real = library.unlink_tree

    def locked(p: Path) -> None:
        if p.name == "data":
            raise PermissionError("[WinError 32] Der Prozess kann nicht auf die Datei zugreifen")
        real(p)

    monkeypatch.setattr(library, "unlink_tree", locked)
    log: list[tuple[str, str]] = []
    res = retention.run(root, Config(), remover(root, log), now=now)
    assert res.failed and not (rec.folder / "audio").exists() and (rec.folder / "data").exists()
    meta = rec.load_meta()
    assert retention.removed_scope(meta) == "" and retention.pending_scope(meta) == "audio_transcript"
    info = read_info(rec.folder)
    assert info.reduced and info.removed_text.startswith("Löschen unvollständig")
    assert not action_states(info, False, False, True)["resummarize"]
    with pytest.raises(FileNotFoundError, match="nicht vollständig gelöscht"):
        make_pipeline(ProfileStore(PlainFilePersistence(tmp_path / "x.json")))[0].run(rec, {"render"})
    # the next run finishes it (no 24 h hold although the transcript is gone already)
    monkeypatch.setattr(library, "unlink_tree", real)
    res = retention.run(root, Config(), remover(root, log), now=now + timedelta(hours=1))
    assert res.removed == ["a"] and not (rec.folder / "data").exists() and rec.summary_path.exists()
    assert retention.removed_scope(rec.load_meta()) == "audio_transcript"


def test_nothing_removed_leaves_the_recording_unchanged(tmp_path: Path):
    root = tmp_path / "lib"
    rec = light(root, "a", iso(START))

    def refuse(p: Path) -> None:
        raise LibraryError("Löschen abgebrochen - die Aufnahme ist unverändert.")

    with pytest.raises(LibraryError):
        retention.remove(rec, root, "audio_transcript", "recycle_bin", recycler=refuse)
    assert "retention" not in rec.load_meta() and read_info(rec.folder).removed_text == ""


def test_whole_recording_keeps_meta_until_the_rest_is_gone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from callrec import library

    root = tmp_path / "lib"
    rec = light(root, "a", iso(START))
    real = library.unlink_tree

    def locked(p: Path) -> None:
        if p.name == "transcript.md":  # audio and data are gone already
            raise PermissionError("gesperrt")
        real(p)

    monkeypatch.setattr(library, "unlink_tree", locked)
    with pytest.raises(LibraryError):
        retention.remove(rec, root, "recording", "permanent")
    assert not (rec.folder / "audio").exists()
    assert rec.meta_path.exists() and retention.pending_scope(rec.load_meta()) == "recording"
    assert read_info(rec.folder).removed_text.startswith("Löschen unvollständig")
    monkeypatch.setattr(library, "unlink_tree", real)
    retention.remove(rec, root, "recording", "permanent")
    assert not rec.folder.exists()


@pytest.mark.skipif(not hasattr(__import__("os"), "symlink"), reason="symlinks")
def test_links_inside_a_recording_are_removed_not_followed(tmp_path: Path):
    import os
    import stat

    root = tmp_path / "lib"
    outside = tmp_path / "Dokumente"
    (outside / "sub").mkdir(parents=True)
    (outside / "wichtig.txt").write_text("bleibt", encoding="utf-8")
    (outside / "sub" / "x.txt").write_text("bleibt", encoding="utf-8")
    os.chmod(outside / "wichtig.txt", 0o644)
    rec = light(root, "a", iso(START))
    import shutil

    shutil.rmtree(rec.folder / "data")
    os.symlink(outside, rec.folder / "data", target_is_directory=True)
    os.symlink(outside / "wichtig.txt", rec.folder / "transcript.v1.md")
    os.symlink(outside, rec.folder / "audio" / "link", target_is_directory=True)
    retention.remove(rec, root, "audio_transcript", "permanent")
    assert (outside / "wichtig.txt").read_text(encoding="utf-8") == "bleibt" and (outside / "sub" / "x.txt").exists()
    assert stat.S_IMODE(os.stat(outside / "wichtig.txt").st_mode) == 0o644  # not even its mode changed
    assert not (rec.folder / "data").exists() and not os.path.lexists(rec.folder / "data")
    rec2 = light(root, "b", iso(START))
    os.symlink(outside, rec2.folder / "data" / "link", target_is_directory=True)
    retention.remove(rec2, root, "recording", "permanent")
    assert not rec2.folder.exists() and (outside / "sub" / "x.txt").exists()


def test_leftover_transcript_temp_files_go_with_the_transcript(tmp_path: Path):
    root = tmp_path / "lib"
    rec = light(root, "a", iso(START))
    (rec.folder / "transcript.v2.md.tmp").write_text("geheimer Inhalt", encoding="utf-8")
    (rec.folder / "summary.v1.md").write_text("Zusammenfassung v1", encoding="utf-8")
    retention.remove(rec, root, "audio_transcript", "permanent")
    assert sorted(p.name for p in rec.folder.iterdir()) == ["meta.json", "summary.md", "summary.v1.md"]


def test_fake_clock_over_many_days(tmp_path: Path):
    """Daily runs over five weeks with a recording every 3 days, one never processed and one pinned:
    nothing goes before its age, the unprocessed one only after it was shown for 24 h, the pinned never."""
    t0 = START
    root = tmp_path / "lib"
    retention.acknowledge()
    removed_at: dict[str, datetime] = {}
    starts: dict[str, datetime] = {}
    cfg = Config()
    for day in range(36):
        now = t0 + timedelta(days=day, hours=9)
        if day % 3 == 0 and day < 24:
            name = f"r{day:02d}"
            starts[name] = now - timedelta(hours=1)
            light(root, name, iso(starts[name]), transcript=day != 6, keep=day == 9)
        if day == 20:  # the user opens the main window once
            retention.mark_surfaced(retention.plan(root, cfg, now), now)
        res = retention.run(root, cfg, lambda rec, scope, mode: retention.remove(rec, root, scope, mode), now=now)
        assert not res.blocked, (day, res.blocked_text)
        for rid in res.removed:
            removed_at[rid] = now
    for name, at in removed_at.items():
        assert at - starts[name] >= timedelta(days=7), name
    assert "r09" not in removed_at  # pinned
    assert removed_at["r06"] >= t0 + timedelta(days=21, hours=9)  # shown on day 20, held 24 h
    assert {n for n in starts if starts[n] + timedelta(days=7) <= t0 + timedelta(days=35)} - {"r09"} == set(removed_at)


def test_open_question_or_dialog_holds_a_recording_and_the_answer_is_exactly_the_list(tmp_path: Path):
    """The after-recording question / speaker dialog of an old recording is open (not in the queue):
    the automatic deletion must not take it meanwhile. "Wie eingestellt löschen" deletes only the
    recordings the question listed."""
    import threading
    from types import SimpleNamespace

    from callrec.ui.actions import AppActions

    now = datetime.now().astimezone()
    root = tmp_path / "lib"
    a = light(root, "a", iso(now - timedelta(days=10)))
    cfg = Config()
    cfg.general.recordings_dir = str(root)
    act = object.__new__(AppActions)
    act.cfg = cfg
    act._held, act._held_lock = {}, threading.Lock()
    act.controller = SimpleNamespace(is_busy=lambda r: False,
                                     remove=lambda rec, scope, mode, **k: retention.remove(rec, root, scope, mode))
    with act.hold(a):
        assert act.retention_busy(a)
        assert retention.plan(root, cfg, busy=act.retention_busy).removable == []
        with pytest.raises(LibraryError):
            act._retention_remove(a, "audio_transcript", "permanent")
    assert not act.retention_busy(a) and a.transcript_path.exists()
    runs: list[Any] = []
    act.run_retention = lambda confirmed=False, done=None, only=None, **k: runs.append((confirmed, only))  # type: ignore[method-assign]
    act.dialogs = SimpleNamespace(ask_retention=lambda *a, **k: "delete")
    act.ui = SimpleNamespace(notify=lambda *a: None)
    assert act.ask_retention("first", "x") == "delete"
    assert runs == [(True, ["a"])]


# ---------------------------------------------------------------------------
# second review of 0.3.1
# ---------------------------------------------------------------------------
def _actions(root: Path, cfg: Config, **kw: Any) -> Any:
    import threading
    from types import SimpleNamespace

    from callrec.ui.actions import AppActions

    act = object.__new__(AppActions)
    act.cfg = cfg
    act._held, act._held_lock = {}, threading.Lock()
    act.controller = kw.get("controller") or SimpleNamespace(
        is_busy=lambda r: False, remove=lambda rec, scope, mode, **k: retention.remove(rec, root, scope, mode))
    act.ui = SimpleNamespace(notify=lambda *a: None, post=lambda fn: fn())
    act.on_changed, act.on_config_changed = [], []
    return act


def test_a_run_while_switched_off_is_no_answer_to_the_first_question(tmp_path: Path):
    """``callrec retention run`` with retention off must not count as "nothing to ask": switched on
    later, the old recordings are listed and asked about first."""
    now = START + timedelta(days=30)
    root = tmp_path / "lib"
    old = [light(root, f"o{i}", iso(now - timedelta(days=30))) for i in range(3)]
    for i in range(5):
        light(root, f"y{i}", iso(now - timedelta(days=1)))
    cfg = Config()
    cfg.retention.enabled = False
    log: list[tuple[str, str]] = []
    retention.run(root, cfg, remover(root, log), now=now)
    assert not retention.load_state().get("acknowledged")
    cfg.retention.enabled = True
    res = retention.run(root, cfg, remover(root, log), now=now)
    assert res.blocked == "first" and log == [] and all(r.transcript_path.exists() for r in old)


def test_keep_and_off_are_no_answer_to_the_first_deletion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from types import SimpleNamespace

    from callrec.ui import actions as actions_mod

    now = datetime.now().astimezone()
    root = tmp_path / "lib"
    a = light(root, "a", iso(now - timedelta(days=10)))
    cfg = Config()
    cfg.general.recordings_dir = str(root)
    act = _actions(root, cfg)
    act.dialogs = SimpleNamespace(ask_retention=lambda *x, **k: "keep")
    assert act.ask_retention("first", "x") == "keep"
    assert retention.is_pinned(a.load_meta()) and not retention.load_state().get("acknowledged")
    b = light(root, "b", iso(now - timedelta(days=10)))  # due later: still asked first
    assert retention.run(root, cfg, remover(root, []), now=now).blocked == "first" and b.transcript_path.exists()
    monkeypatch.setattr(actions_mod, "save_config", lambda base: None)
    act.apply_saved_config = lambda: None
    act.dialogs = SimpleNamespace(ask_retention=lambda *x, **k: "off")
    assert act.ask_retention("first", "x") == "off"
    assert not retention.load_state().get("acknowledged")


def test_the_run_decides_by_the_settings_at_its_start_and_stops_when_they_change(tmp_path: Path):
    now = START + timedelta(days=30)
    root = tmp_path / "lib"
    recs = [light(root, f"o{i}", iso(now - timedelta(days=30))) for i in range(2)]
    for i in range(4):
        light(root, f"y{i}", iso(now - timedelta(days=1)))
    retention.acknowledge()
    cfg = Config()
    cfg.general.recordings_dir = str(root)
    used: list[str] = []

    def go(rec: Recording, scope: str, mode: str) -> str:
        used.append(scope)
        cfg.retention.scope = "recording"  # settings saved while the run is under way
        return retention.remove(rec, root, scope, mode)

    res = retention.run(root, cfg, go, now=now)
    assert used == ["audio_transcript"] and len(res.removed) == 1
    assert all(r.folder.exists() and r.summary_path.exists() for r in recs)


def test_an_answer_given_to_other_settings_deletes_nothing(tmp_path: Path):
    """The question said "Audio und Transkript"; meanwhile the settings say "die ganze Aufnahme"."""
    now = START + timedelta(days=30)
    root = tmp_path / "lib"
    a = light(root, "a", iso(now - timedelta(days=10)))
    cfg = Config()
    cfg.general.recordings_dir = str(root)
    asked = retention.settings_key(cfg, root)
    cfg.retention.scope = "recording"
    log: list[tuple[str, str]] = []
    res = retention.run(root, cfg, remover(root, log), now=now, confirmed=True, only=["a"], expect=asked)
    assert res.blocked == "changed" and log == [] and a.summary_path.exists()
    assert not retention.load_state().get("acknowledged")
    cfg.retention.scope = "audio_transcript"
    res = retention.run(root, cfg, remover(root, log), now=now, confirmed=True, only=["a"], expect=asked)
    assert res.removed == ["a"] and a.summary_path.exists()


def test_each_recording_is_checked_again_right_before_it_is_deleted(tmp_path: Path):
    now = START + timedelta(days=30)
    root = tmp_path / "lib"
    a = light(root, "a", iso(now - timedelta(days=30)))
    b = light(root, "b", iso(now - timedelta(days=30)))
    c = light(root, "c", iso(now - timedelta(days=30)))
    for i in range(4):
        light(root, f"y{i}", iso(now - timedelta(days=1)))
    retention.acknowledge()

    def go(rec: Recording, scope: str, mode: str) -> str:  # newest first: c, then b and a
        if rec.folder == c.folder:
            retention.set_pinned(b, True)  # "Behalten" clicked while the first one is deleted
            a.update_meta(["processing", "status"], "running")  # "callrec process" started meanwhile
        return retention.remove(rec, root, scope, mode)

    res = retention.run(root, Config(), go, now=now)
    assert res.removed == ["c"] and not c.transcript_path.exists()
    assert b.transcript_path.exists() and a.transcript_path.exists()


def test_reduced_meta_keeps_no_error_text_of_a_failed_summary(tmp_path: Path):
    """A server's error body can quote the request (the transcript)."""
    now = START + timedelta(days=30)
    root = tmp_path / "lib"
    a = light(root, "a", iso(now - timedelta(days=10)), processing={
        "status": "done", "summary": {"skipped": "KI-Server-Fehler 422: … geheimer Inhalt …", "status": "failed"}})
    retention.remove(a, root, "audio_transcript", "permanent")
    text = a.meta_path.read_text(encoding="utf-8")
    assert "geheimer Inhalt" not in text and a.load_meta()["processing"]["summary"]["status"] == "failed"


def test_busy_and_open_dialogs_are_recognised_however_the_folder_is_spelled(tmp_path: Path):
    """The web UI names a folder resolved (mapped drive -> UNC, junction, 8.3 name), the lists as
    configured: a queued job or an open dialog must hold the recording either way."""
    import threading
    from types import SimpleNamespace

    from callrec.controller import Controller
    from callrec.jobs import ProcessingQueue

    now = datetime.now().astimezone()
    real = tmp_path / "real"
    real.mkdir()
    root = tmp_path / "linked"
    root.symlink_to(real, target_is_directory=True)
    a = light(root, "a", iso(now - timedelta(days=10)))
    b = light(root, "b", iso(now - timedelta(days=10)))
    q = ProcessingQueue.__new__(ProcessingQueue)
    q._lock, q._pending, q._current = threading.Lock(), [SimpleNamespace(recording=Recording(a.folder.resolve()))], None
    ctl = Controller.__new__(Controller)
    ctl.queue, ctl.current = q, None
    assert q.is_busy_with(a.folder) and ctl.is_busy(a)
    ctl.current = Recording(b.folder.resolve())
    assert ctl.is_busy(b)
    ctl.current = None
    cfg = Config()
    cfg.general.recordings_dir = str(root)
    act = _actions(root, cfg, controller=ctl)
    with act.hold(Recording(b.folder.resolve())):
        assert act.retention_busy(b)
        p = retention.plan(root, cfg, now, busy=act.retention_busy)
        assert p.removable == [] and {i.id for i in p.waiting} == {"a", "b"}


def test_a_linked_folder_outside_is_neither_listed_nor_pinned_through_the_question(tmp_path: Path):
    now = START + timedelta(days=30)
    root = tmp_path / "lib"
    root.mkdir()
    outside = light(tmp_path / "elsewhere", "x", iso(now - timedelta(days=30)))
    before = outside.meta_path.read_bytes()
    (root / "x").symlink_to(outside.folder, target_is_directory=True)
    light(root, "a", iso(now - timedelta(days=1)))
    p = retention.plan(root, Config(), now)
    assert p.removable == [] and p.candidates == 1
    retention.pin_all(p.removable)
    assert outside.meta_path.read_bytes() == before


def test_huge_age_means_never_and_does_not_overflow(tmp_path: Path):
    now = START + timedelta(days=30)
    root = tmp_path / "lib"
    light(root, "a", iso(now - timedelta(days=3000)))
    cfg = Config()
    cfg.retention.max_age_days = 10**12
    p = retention.plan(root, cfg, now)
    assert p.removable == [] and not p.blocked


def test_recycle_bin_that_leaves_the_file_is_a_failure_not_a_deletion(tmp_path: Path):
    now = START + timedelta(days=30)
    root = tmp_path / "lib"
    a = light(root, "a", iso(now - timedelta(days=10)))
    with pytest.raises(LibraryError):
        retention.remove(a, root, "audio_transcript", "recycle_bin", recycler=lambda p: None)
    assert "retention" not in a.load_meta() and a.transcript_path.exists()  # nothing moved: unchanged


def test_hidden_classic_window_does_not_count_as_shown(tmp_path: Path):
    """The classic main window refreshes its list in the tray too (after processing, after a
    deletion): that is no "shown as wird gelöscht"."""
    from types import SimpleNamespace

    pytest.importorskip("tkinter")
    from callrec.ui.mainwindow import MainWindow

    now = datetime.now().astimezone()
    root = tmp_path / "lib"
    light(root, "a", iso(now - timedelta(days=10)), transcript=False)  # never processed
    plan = retention.plan(root, Config(), now)
    btn = SimpleNamespace(pack=lambda **k: None, pack_forget=lambda: None)

    def window(visible: bool) -> Any:
        return SimpleNamespace(visible=visible, win=SimpleNamespace(winfo_viewable=lambda: visible),
                               retention_var=SimpleNamespace(set=lambda t: None), ret_keep_btn=btn, ret_now_btn=btn,
                               px=lambda v: v)

    MainWindow.show_retention(window(False), plan)
    assert not (retention.load_state().get("surfaced") or {})
    MainWindow.show_retention(window(True), plan)
    assert "a" in retention.load_state()["surfaced"]


@pytest.mark.parametrize("scope", ["audio_transcript", "recording"])
def test_a_partly_emptied_audio_folder_is_labelled_not_rolled_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                    scope: str):
    """One track deleted, the other one locked by a player: the recording must say "Löschen
    unvollständig" (never processed again, finished by the next run) - not look untouched."""
    import os

    root = tmp_path / "lib"
    a = light(root, "a", iso(START))
    (a.folder / "audio" / "mix.flac").write_bytes(b"y")
    real, calls = os.unlink, [0]

    def locked_second(p: Any, *args: Any, **kw: Any) -> None:
        if str(p).endswith(".flac"):
            calls[0] += 1
            if calls[0] == 2:
                raise PermissionError(13, "gesperrt", str(p))
        real(p, *args, **kw)

    monkeypatch.setattr(os, "unlink", locked_second)
    with pytest.raises(LibraryError):
        retention.remove(a, root, scope, "permanent")
    monkeypatch.setattr(os, "unlink", real)
    assert len(list((a.folder / "audio").iterdir())) == 1
    assert retention.pending_scope(a.load_meta()) == scope and read_info(a.folder).removed_text.startswith("Löschen unv")
