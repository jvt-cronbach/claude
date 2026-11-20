"""'Mit Windows starten': registry entry logic with a fake registry (no Windows needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from callrec import setupcheck
from callrec.autostart import (
    ARGS,
    OFF,
    ON,
    STALE,
    UNAVAILABLE,
    Autostart,
    command_executable,
    desired_command,
)


class FakeReg:
    def __init__(self, value: str | None = None) -> None:
        self.value = value
        self.writes = 0

    def read(self):
        return self.value

    def write(self, command):
        self.value = command
        self.writes += 1

    def delete(self):
        self.value = None


@pytest.fixture
def install(tmp_path: Path) -> Path:
    exe = tmp_path / "cr" / ".venv" / "Scripts" / "pythonw.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    return exe


def test_enable_disable_and_status(install: Path, tmp_path: Path):
    reg = FakeReg()
    a = Autostart(reg, str(install), startup_dir=tmp_path / "startup")
    assert a.status().state == OFF
    st = a.enable()
    assert st.state == ON and reg.value == f'"{install}" {ARGS}' and "--minimized" in reg.value
    assert a.disable().state == OFF and reg.value is None


def test_python_exe_is_replaced_by_pythonw(install: Path):
    assert desired_command(str(install.with_name("python.exe"))) == f'"{install}" {ARGS}'


def test_moved_folder_is_stale_and_repaired(install: Path, tmp_path: Path):
    reg = FakeReg(f'"{tmp_path / "alt" / ".venv" / "Scripts" / "pythonw.exe"}" {ARGS}')
    a = Autostart(reg, str(install), startup_dir=tmp_path / "startup")
    st = a.status()
    assert st.state == STALE and "verschoben" in st.detail
    msg = a.repair_if_stale()
    assert "korrigiert" in msg and a.status().state == ON and reg.writes == 1
    assert a.repair_if_stale() == "" and reg.writes == 1  # nothing to do any more


def test_other_installation_is_stale(install: Path, tmp_path: Path):
    other = tmp_path / "zweite" / "pythonw.exe"
    other.parent.mkdir()
    other.write_text("")
    a = Autostart(FakeReg(f'"{other}" {ARGS}'), str(install), startup_dir=tmp_path / "startup")
    assert a.status().state == STALE


def test_legacy_startup_shortcut_is_converted(install: Path, tmp_path: Path):
    startup = tmp_path / "startup"
    startup.mkdir()
    lnk = startup / "CallRecorder.lnk"
    lnk.write_text("x")
    reg = FakeReg()
    a = Autostart(reg, str(install), startup_dir=startup)
    assert a.status().state == STALE and a.status().legacy_shortcut == lnk
    a.repair_if_stale()
    assert not lnk.exists() and a.status().state == ON


def test_unavailable_off_windows():
    a = Autostart(None)
    assert a.status().state == UNAVAILABLE and a.repair_if_stale() == ""
    with pytest.raises(OSError):
        a.enable()


def test_command_executable():
    assert command_executable('"C:\\A B\\pythonw.exe" -m callrec tray') == "C:\\A B\\pythonw.exe"
    assert command_executable("C:\\x\\pythonw.exe -m callrec") == "C:\\x\\pythonw.exe"


def test_setup_check_item(install: Path, tmp_path: Path):
    reg = FakeReg()
    a = Autostart(reg, str(install), startup_dir=tmp_path / "s")
    item = setupcheck.check_autostart(a.status())
    assert item.state == setupcheck.OPTIONAL and ("Mit Windows starten", setupcheck.A_AUTOSTART_ON) in item.actions
    a.enable()
    assert setupcheck.check_autostart(a.status()).state == setupcheck.OK
    reg.value = '"C:\\weg\\pythonw.exe" -m callrec tray --minimized'
    item = setupcheck.check_autostart(a.status())
    assert item.state == setupcheck.WARN and ("Jetzt korrigieren", setupcheck.A_AUTOSTART_ON) in item.actions
    assert setupcheck.check_autostart(Autostart(None).status()).state == setupcheck.UNKNOWN


def test_cli_tray_minimized_flag(monkeypatch):
    from callrec import cli

    seen = []
    monkeypatch.setattr(cli, "main_tray", lambda minimized=False, **_k: seen.append(minimized) or 0)
    assert cli.main(["tray", "--minimized"]) == 0 and cli.main(["tray"]) == 0
    assert seen == [True, False]


def test_setup_ps1_writes_the_same_entry():
    text = (Path(__file__).resolve().parents[1] / "setup.ps1").read_text(encoding="utf-8-sig")
    assert 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run' in text
    assert f'"`"$VenvPythonW`" {ARGS}"' in text and '-Name "CallRecorder"' in text


def test_missing_pythonw_is_never_written(tmp_path: Path):
    py = tmp_path / "py" / "python.exe"
    py.parent.mkdir()
    py.write_text("")
    assert desired_command(str(py)) == f'"{py}" {ARGS}'  # no pythonw.exe next to it: keep python.exe
    reg = FakeReg()
    a = Autostart(reg, str(tmp_path / "weg" / "python.exe"), startup_dir=tmp_path / "startup")
    with pytest.raises(OSError):
        a.enable()
    assert reg.value is None


def test_repair_leaves_an_existing_other_installation_alone(install: Path, tmp_path: Path):
    other = tmp_path / "zweite" / "pythonw.exe"
    other.parent.mkdir()
    other.write_text("")
    reg = FakeReg(f'"{other}" {ARGS}')
    a = Autostart(reg, str(install), startup_dir=tmp_path / "startup")
    assert a.repair_if_stale() == "" and reg.value == f'"{other}" {ARGS}' and reg.writes == 0
    # same program, older arguments: repaired
    reg2 = FakeReg(f'"{install}" -m callrec tray')
    a2 = Autostart(reg2, str(install), startup_dir=tmp_path / "startup")
    assert "korrigiert" in a2.repair_if_stale() and reg2.value == f'"{install}" {ARGS}'
    # moved folder, and this installation is gone too: nothing is written
    reg3 = FakeReg(f'"{tmp_path / "alt" / "pythonw.exe"}" {ARGS}')
    a3 = Autostart(reg3, str(tmp_path / "auch-weg" / "pythonw.exe"), startup_dir=tmp_path / "startup")
    assert "nicht korrigiert" in a3.repair_if_stale() and reg3.writes == 0


def test_settings_box_unticked_on_a_stale_entry_removes_it():
    from callrec.autostart import settings_change

    assert settings_change(STALE, True, False) == "disable"  # shown ticked (stale), user unticks
    assert settings_change(STALE, True, True) == ""
    assert settings_change(OFF, False, True) == "enable"
    assert settings_change(ON, True, True) == ""
    assert settings_change(UNAVAILABLE, False, True) == ""
