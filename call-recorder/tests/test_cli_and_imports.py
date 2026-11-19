from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

import numpy as np
import pytest

import callrec
from callrec import cli
from callrec.audio.wasapi import WasapiBackend, WasapiUnavailable


def all_modules() -> list[str]:
    return [m.name for m in pkgutil.walk_packages(callrec.__path__, "callrec.")]


@pytest.mark.parametrize("name", all_modules())
def test_every_module_imports(name: str):
    if name.startswith("callrec.ui.") and name != "callrec.ui.app":
        pytest.importorskip("tkinter")
    if name == "callrec.ui.tray":
        # pystray is imported lazily inside TrayIcon, so the module itself must import
        pass
    importlib.import_module(name)


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows guard")
def test_windows_only_parts_fail_cleanly():
    with pytest.raises(WasapiUnavailable):
        WasapiBackend()
    from callrec.hotkey import GlobalHotkey, parse_hotkey

    hk = GlobalHotkey(parse_hotkey("ctrl+alt+r"), lambda: None)
    assert hk.start() is False and "Windows" in hk.error


def test_tray_icon_images():
    from callrec.ui.tray import IDLE, RECORDING, make_icon

    assert make_icon(IDLE).size == (64, 64)
    assert make_icon(RECORDING, recording=True).getpixel((32, 32)) == (255, 255, 255, 255)


def test_parser_presets():
    p = cli.build_parser()
    a = p.parse_args(["process", "X", "--steps", "resummarize"])
    assert a.steps == {"summarize"}
    a = p.parse_args(["process", "X", "--steps", "transcribe,render"])
    assert a.steps == {"transcribe", "render"}
    with pytest.raises(SystemExit):
        p.parse_args(["process", "X", "--steps", "bogus"])
    assert p.parse_args(["summarize", "X"]).steps == {"summarize"}
    assert p.parse_args(["repair", "X"]).steps == {"finalize"}


def test_cli_profiles_and_assign(tmp_path: Path, capsys, monkeypatch):
    from callrec.factory import profile_store
    from callrec.models import SpeakerInfo
    from tests.conftest import make_recording

    store = profile_store()
    store.add_sample("Anna Beispiel", "anna@firma.de", np.ones(8), "m")
    assert cli.main(["profiles", "list"]) == 0
    assert "Anna Beispiel" in capsys.readouterr().out
    assert cli.main(["profiles", "delete", "niemand"]) == 1
    assert cli.main(["profiles", "delete", "Anna Beispiel"]) == 0
    assert profile_store().all_profiles() == []

    rec = make_recording(tmp_path / "recs", with_parts=False)
    # minimal processed state
    from callrec.models import AsrResult, AsrSegment, Word

    seg = AsrResult("de", 3, [AsrSegment(0, 1, "Hallo", [Word(0, 1, " Hallo")])])
    rec.save_asr("mic", seg)
    rec.save_asr("remote", seg)
    rec.save_speakers({"ME": SpeakerInfo("ME", "Ich", name="Ich Selbst", source="self"), "S1": SpeakerInfo("S1", "Sprecher 1")})
    rec.turns_path.write_text('[{"start": 0, "end": 1, "speaker": "S1"}]', encoding="utf-8")
    rec.save_embeddings({"S1": np.ones(8, dtype=np.float32)}, "m")
    assert cli.main(["speakers", str(rec.folder)]) == 0
    out = capsys.readouterr().out
    assert "Sprecher 1" in out and "Anna Beispiel" in out
    assert cli.main(["assign", str(rec.folder), "S1=Anna Beispiel"]) == 0
    assert "Anna Beispiel:** Hallo" in rec.transcript_path.read_text(encoding="utf-8")
    assert profile_store().find("Anna Beispiel", "", "m") is not None
    assert cli.main(["purge-voiceprints", str(rec.folder)]) == 0
    assert not rec.embeddings_path.exists()


def test_cli_config_and_event(tmp_path: Path, capsys):
    assert cli.main(["config", "check"]) == 0
    assert "gültig" in capsys.readouterr().out
    from tests.test_ics import OUTLOOK_ICS

    ics = tmp_path / "e.ics"
    ics.write_text(OUTLOOK_ICS, encoding="utf-8")
    assert cli.main(["event", "--ics", str(ics)]) == 0
    out = capsys.readouterr().out
    assert "Projekt Jour fixe" in out and "_Projekt-Jour-fixe" in out


def test_cli_handoff_preset_versions_llm(tmp_path: Path, capsys):
    from callrec.config import Config, save_config
    from tests.test_handoff import SECRET, processed_recording

    share = tmp_path / "share"
    share.mkdir()
    cfg = Config()
    cfg.handoff.folder = str(share)
    save_config(cfg)
    rec = processed_recording(tmp_path / "recs")
    assert cli.main(["handoff", str(rec.folder), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "verlässt bewusst" in out and SECRET not in out
    assert (share / f"{rec.folder.name}.md").exists() and rec.load_meta()["handoffs"]

    assert cli.main(["preset", "save", "Genau"]) == 0
    assert cli.main(["preset", "list"]) == 0
    assert "Genau" in capsys.readouterr().out
    assert cli.main(["versions", str(rec.folder)]) == 0
    assert "summary:" in capsys.readouterr().out
    # nothing listens on 127.0.0.1:11434 here -> graceful message, no crash
    assert cli.main(["llm", "models"]) == 1
    assert "nicht erreichbar" in capsys.readouterr().out
    assert cli.main(["config", "files"]) == 0
    assert "glossary.txt" in capsys.readouterr().out
