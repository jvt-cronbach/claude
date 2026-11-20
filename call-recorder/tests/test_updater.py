"""In-app updates: release pointer checks, the helper (checkout, rollback) - with real local git repos."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from callrec import updater as up
from callrec.update_helper import Helper

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git fehlt")

URL = "https://github.com/example/claude"
BRANCH = "claude/test-branch"

MAIN = """import sys
from callrec import __version__
if sys.argv[1:] == ["--version"]:
    print("callrec " + __version__)
elif sys.argv[1:] == ["postinstall"]:
    sys.exit(int(open("postinstall_rc").read()) if __import__("os").path.exists("postinstall_rc") else 0)
"""


def g(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


def commit(origin: Path, version: str, msg: str, extra: dict[str, str] | None = None) -> str:
    app = origin / "call-recorder"
    (app / "src" / "callrec").mkdir(parents=True, exist_ok=True)
    (app / "src" / "callrec" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (app / "src" / "callrec" / "__main__.py").write_text(MAIN, encoding="utf-8")
    (app / "requirements.txt").write_text("numpy==2.5.5\n", encoding="utf-8")
    for name, text in (extra or {}).items():
        (app / name).write_text(text, encoding="utf-8")
    g(origin, "add", "-A")
    g(origin, "commit", "-q", "-m", msg)
    return g(origin, "rev-parse", "HEAD")


def pointer(origin: Path, sha: str | None, version: str) -> str:
    rel = {"schema": 1, "version": version, "commit": sha, "date": "2026-11-20", "notes": ["Neu"], "python": ">=3.12"}
    (origin / "call-recorder" / "release.json").write_text(json.dumps(rel), encoding="utf-8")
    g(origin, "add", "-A")
    g(origin, "commit", "-q", "-m", "release")
    return g(origin, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path: Path):
    origin = tmp_path / "origin"
    origin.mkdir()
    g(origin, "init", "-q", "-b", BRANCH)
    g(origin, "config", "uploadpack.allowFilter", "true")
    old = commit(origin, "0.1.0", "old")
    reviewed = commit(origin, "0.2.0", "reviewed", {"requirements.txt": "numpy==2.5.5\nrequests==2.34.2\n"})
    wip = commit(origin, "0.3.0", "work in progress")
    pointer(origin, reviewed, "0.2.0")
    inst = tmp_path / "Programs" / "CallRecorder"
    g(tmp_path, "clone", "-q", "--no-local", str(origin), str(inst))
    g(inst, "-c", "advice.detachedHead=false", "checkout", "-q", "--detach", old)

    def runner(args, cwd, timeout):
        args = [f"file://{origin}" if a == URL else a for a in args]
        rc, out, err = up.run_git(args, cwd, timeout)
        return rc, out.replace(f"file://{origin}", URL), err

    u = up.Updater(URL, BRANCH, git=runner, root=inst / "call-recorder")
    return {"origin": origin, "inst": inst, "old": old, "reviewed": reviewed, "wip": wip, "u": u}


def test_check_offers_exactly_the_released_commit_not_the_tip(world):
    res = world["u"].check()
    assert res.available and res.release.commit == world["reviewed"] and res.target == world["reviewed"]
    assert res.release.version == "0.2.0" and res.requirements_changed
    assert res.installed_commit == world["old"]


def test_pointer_to_foreign_or_mismatching_commit_is_refused(world, tmp_path: Path):
    origin = world["origin"]
    pointer(origin, world["wip"], "0.2.0")  # names WIP with the wrong version
    with pytest.raises(up.UpdateError, match="meldet Version 0.3.0"):
        world["u"].check()
    pointer(origin, "1" * 40, "0.2.0")  # not part of the branch
    with pytest.raises(up.UpdateError, match="nicht zum Zweig"):
        world["u"].check()
    pointer(origin, None, "0.2.0")
    with pytest.raises(up.UpdateError, match="noch keine Version"):
        world["u"].check()


def test_installed_release_or_newer_is_not_updated(world):
    g(world["inst"], "-c", "advice.detachedHead=false", "checkout", "-q", "--detach", world["reviewed"])
    assert "aktuell" in world["u"].check().message
    g(world["inst"], "-c", "advice.detachedHead=false", "checkout", "-q", "--detach", world["wip"])
    res = world["u"].check()
    assert not res.available and "abweichender oder neuerer" in res.message


def test_zip_install_and_missing_git_are_explained(tmp_path: Path):
    u = up.Updater(URL, BRANCH, root=tmp_path)
    with pytest.raises(up.UpdateError, match="install-callrecorder.ps1"):
        u.repo_root()

    def no_git(args, cwd, timeout):
        raise up.UpdateError(up.INSTALL_HINT)

    with pytest.raises(up.UpdateError, match="winget install Git.Git"):
        up.Updater(URL, BRANCH, git=no_git, root=tmp_path).check()


def test_source_validation_and_config(tmp_path: Path):
    from callrec.config import Config, ConfigError, apply_preset, config_from_dict

    for bad in ("http://github.com/a/b", "https://evil.example/a/b", "https://github.com/a/b/../c", "file:///x"):
        with pytest.raises(up.UpdateError):
            up.validate_source(bad, BRANCH)
    with pytest.raises(up.UpdateError):
        up.validate_source(URL, "--upload-pack=x")
    with pytest.raises(ConfigError):
        config_from_dict({"update": {"repo_url": "https://evil.example/x/y"}})
    cfg = apply_preset(Config(), {"update": {"repo_url": "https://github.com/evil/repo"}}, "p")
    assert cfg.update.repo_url == Config().update.repo_url and any("[update]" in w for w in cfg.warnings)


def test_release_parsing_and_python_range():
    with pytest.raises(up.UpdateError):
        up.Release.parse('{"schema": 1, "version": "0.2", "commit": "abc"}')
    assert up.Release.parse(json.dumps({"schema": 1, "version": "0.2.0", "commit": "a" * 40, "notes": "x"})).notes == ["x"]
    assert up.python_ok(">=3.12,<3.14", (3, 12)) and not up.python_ok(">=3.12,<3.14", (3, 14))
    assert up.version_tuple("0.10.1") > up.version_tuple("0.9.9")


def _fake_python(tmp_path: Path, pip_rc: int = 0) -> Path:
    """A stand-in for the venv python: runs the checked-out package; "-m pip" exits with ``pip_rc``."""
    script = tmp_path / "fakepython"
    script.write_text(f"""#!/bin/sh
if [ "$2" = "pip" ]; then exit {pip_rc}; fi
PYTHONPATH=src exec "{sys.executable}" "$@"
""", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _plan(world, tmp_path: Path, python: Path, target: str, version: str, req: bool = False) -> dict:
    venv = tmp_path / "venv"
    venv.mkdir(exist_ok=True)
    (venv / "marker").write_text("alt", encoding="utf-8")
    return {"mode": "update", "repo_root": str(world["inst"]), "app_root": str(world["inst"] / "call-recorder"),
            "venv_python": str(python), "venv_dir": str(venv), "target": target, "version": version,
            "previous": world["old"], "requirements_changed": req, "with_cuda": False, "app_pid": 0, "restart": [],
            "log": str(tmp_path / "update.log"), "status": str(tmp_path / "status.json"),
            "install": str(tmp_path / "install.json")}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake interpreter")
def test_helper_updates_to_the_released_commit(world, tmp_path: Path):
    world["u"].check()  # fetches the commits into the installation
    plan = _plan(world, tmp_path, _fake_python(tmp_path), world["reviewed"], "0.2.0")
    assert Helper(plan).main() is True
    assert g(world["inst"], "rev-parse", "HEAD") == world["reviewed"]
    st = json.loads(Path(plan["status"]).read_text(encoding="utf-8"))
    assert st["ok"] and "0.2.0" in st["message"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake interpreter")
def test_helper_rolls_back_code_and_venv_on_failure(world, tmp_path: Path):
    world["u"].check()
    plan = _plan(world, tmp_path, _fake_python(tmp_path, pip_rc=1), world["reviewed"], "0.2.0", req=True)
    assert Helper(plan).main() is False
    assert g(world["inst"], "rev-parse", "HEAD") == world["old"]
    assert (Path(plan["venv_dir"]) / "marker").read_text(encoding="utf-8") == "alt"
    st = json.loads(Path(plan["status"]).read_text(encoding="utf-8"))
    assert not st["ok"] and "wiederhergestellt" in st["message"]
    # a failing post-install step of the new version is rolled back as well
    (world["inst"] / "call-recorder" / "postinstall_rc").write_text("1", encoding="utf-8")
    plan = _plan(world, tmp_path, _fake_python(tmp_path), world["reviewed"], "0.2.0")
    assert Helper(plan).main() is False and g(world["inst"], "rev-parse", "HEAD") == world["old"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake interpreter")
def test_helper_refuses_when_the_app_does_not_exit(world, tmp_path: Path):
    plan = _plan(world, tmp_path, _fake_python(tmp_path), world["reviewed"], "0.2.0")
    plan["app_pid"] = os.getpid()  # "the app" (this test process) never exits
    h = Helper(plan)
    h.wait_for_app = lambda timeout=0.1: Helper.wait_for_app(h, 0.2)  # type: ignore[method-assign]
    assert h.main() is False
    assert g(world["inst"], "rev-parse", "HEAD") == world["old"]  # nothing changed


def test_status_shown_once_and_install_record(isolated_home: Path, tmp_path: Path):
    up.status_file().parent.mkdir(parents=True, exist_ok=True)
    up.status_file().write_text(json.dumps({"ok": False, "message": "kaputt"}), encoding="utf-8")
    assert up.take_status()["message"] == "kaputt" and up.take_status() is None

    shas = iter(["a" * 40, "b" * 40])

    def fake(args, cwd, timeout):
        return 0, next(shas), ""

    up.record_install(fake, tmp_path)
    info = up.record_install(fake, tmp_path)
    assert info["commit"] == "b" * 40 and info["previous_commit"] == "a" * 40
    assert ("b" * 10) in up.version_text()


def test_launch_helper_copies_the_script_out_of_the_program_folder(tmp_path: Path):
    calls = []
    plan = {"previous": "a" * 40, "target": "b" * 40}
    plan_file = up.launch_helper(plan, popen=lambda args, **kw: calls.append((args, kw)))
    args, kw = calls[0]
    assert args[1] == "-I" and Path(args[2]).parent == plan_file.parent and Path(args[2]).exists()
    assert json.loads(plan_file.read_text(encoding="utf-8"))["target"] == "b" * 40


def test_update_waits_while_recording_or_processing(monkeypatch):
    pytest.importorskip("tkinter")
    from callrec.ui.actions import AppActions

    notes: list[tuple[str, str]] = []
    later: list = []

    class Q:
        def snapshot(self):
            class S:
                current = object()
                pending: list = []
            return S()

    class Ctl:
        is_recording = False

    class Root:
        def after(self, ms, fn):
            later.append(fn)

    a = AppActions.__new__(AppActions)
    a.controller, a.queue = Ctl(), Q()  # type: ignore[assignment]
    a.ui = type("U", (), {"notify": lambda self, t, m: notes.append((t, m)), "root": Root()})()
    launched = []
    monkeypatch.setattr(up, "launch_helper", lambda plan: launched.append(plan))
    assert a.start_update(object(), wait=False) is False and "verarbeitet" in notes[-1][1]
    assert a.start_update(object(), wait=True) is False and later and launched == []
