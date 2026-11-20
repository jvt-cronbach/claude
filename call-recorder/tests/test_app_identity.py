"""App logo and Windows identity (0.3.1): icon files, AppUserModelID before any window, shortcut."""

from __future__ import annotations

import struct
from typing import Any

import pytest

from callrec.ui import appid


def ico_sizes(path) -> list[int]:
    raw = path.read_bytes()
    _r, kind, n = struct.unpack_from("<HHH", raw, 0)
    assert kind == 1
    sizes = []
    for i in range(n):
        w, _h, _c, _res, _planes, _bpp, length, offset = struct.unpack_from("<BBBBHHII", raw, 6 + 16 * i)
        assert raw[offset:offset + 8] == b"\x89PNG\r\n\x1a\n" and offset + length <= len(raw)
        sizes.append(w or 256)
    return sizes


def test_icon_files_exist_with_the_expected_sizes():
    from PIL import Image

    for name in ("callrec.ico", "callrec-recording.ico"):
        assert ico_sizes(appid.ASSETS / name) == [16, 20, 24, 32, 40, 48, 64, 256]
    for variant in ("callrec", "callrec-recording", "callrec-busy"):
        for size in (16, 24, 32, 48, 64, 256):
            with Image.open(appid.png_path(size, variant)) as im:
                assert im.size == (size, size) and im.mode == "RGBA"
    assert appid.icon_path().exists() and appid.icon_path(True).exists()
    from callrec.ui.webserver import WEB_DIR

    assert (WEB_DIR / "favicon.svg").read_text(encoding="utf-8").startswith("<svg")


def test_tray_uses_the_same_artwork():
    from callrec.ui.tray import BUSY, IDLE, RECORDING, app_icon, make_icon

    assert make_icon(IDLE).tobytes() == app_icon("idle").tobytes()
    assert make_icon(RECORDING, True).tobytes() == app_icon("recording").tobytes()
    assert make_icon(BUSY).tobytes() == app_icon("busy").tobytes()


def test_app_user_model_id_is_set_before_any_window(monkeypatch: pytest.MonkeyPatch):
    from callrec.ui import app as app_mod

    order: list[str] = []

    class Shell32:
        def SetCurrentProcessExplicitAppUserModelID(self, app_id: str) -> int:  # noqa: N802
            order.append(f"aumid:{app_id}")
            return 0

    real = appid.set_app_user_model_id
    monkeypatch.setattr(app_mod, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(appid, "set_app_user_model_id", lambda shell32=None, app_id=appid.APP_ID: real(Shell32(), app_id))
    monkeypatch.setattr(app_mod, "acquire_single_instance", lambda: True)
    monkeypatch.setattr(app_mod, "load_config", lambda: _cfg("web"))
    monkeypatch.setattr(app_mod, "enable_dpi_awareness", lambda per_monitor=False: None)
    import callrec.ui.webapp as webapp

    monkeypatch.setattr(webapp, "run_web_app", lambda *a, **k: order.append("web-window") or 0)
    assert app_mod.run_tray_app() == 0
    monkeypatch.setattr(app_mod, "load_config", lambda: _cfg("tk"))
    monkeypatch.setattr(app_mod, "_run_tk", lambda *a, **k: order.append("tk-window") or 0)
    assert app_mod.run_tray_app() == 0
    assert order == [f"aumid:{appid.APP_ID}", "web-window", f"aumid:{appid.APP_ID}", "tk-window"]


def _cfg(frontend: str) -> Any:
    from callrec.config import Config

    c = Config()
    c.ui.frontend = frontend
    return c


def test_set_app_user_model_id_reports_failure():
    class Bad:
        def SetCurrentProcessExplicitAppUserModelID(self, app_id: str) -> int:  # noqa: N802
            return -2147024809  # E_INVALIDARG

    assert appid.set_app_user_model_id(Bad()) is False


def test_shortcut_carries_icon_and_app_id():
    s = appid.shortcut_script(r"C:\Program Files\Py\pythonw.exe", "-m callrec tray", r"C:\Apps\O'Brien",
                              r"C:\Apps\callrec.ico")
    assert "$l.IconLocation = 'C:\\Apps\\callrec.ico,0'" in s
    assert "[CallRecAumid]::Set($p, 'CallRecorder.CallRecorder.App')" in s
    assert "9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3" in s and "k.pid = 5" in s  # PKEY_AppUserModel_ID
    assert "'C:\\Apps\\O''Brien'" in s  # quoted
    assert "\n'@" in s  # here-string terminator at the start of a line
    # COM methods return their HRESULT (no hidden [out, retval] - matters for 32-bit PowerShell)
    assert s.count("[PreserveSig] int") == 5
    # a failing Add-Type/property write leaves a working shortcut and does not fail postinstall
    assert s.index("$l.Save()") < s.index("try {") and "catch {" in s and "-ErrorAction Stop" in s


def test_postinstall_shortcut_uses_the_identity(monkeypatch: pytest.MonkeyPatch):
    import subprocess

    from callrec import cli

    seen: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: seen.append(cmd))
    cli.create_start_menu_shortcut()
    script = seen[0][-1]
    assert str(appid.icon_path()) in script and appid.APP_ID in script and "-m callrec tray" in script


def test_tk_window_icon_and_recording_variant(monkeypatch: pytest.MonkeyPatch):
    calls: list[Any] = []

    class Root:
        def iconbitmap(self, default: str) -> None:
            calls.append(default)

        def iconphoto(self, default: bool, img: Any) -> None:
            calls.append(img)

    monkeypatch.setattr(appid.sys, "platform", "win32")
    appid.tk_set_icon(Root())
    appid.tk_set_icon(Root(), recording=True)
    assert calls == [str(appid.icon_path()), str(appid.icon_path(True))]
