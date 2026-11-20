"""In-app updates from GitHub: install exactly the released commit, never the branch tip.

Release pointer
---------------
``call-recorder/release.json`` on the configured branch names the released version::

    {"schema": 1, "version": "0.2.0", "commit": "<40-hex SHA>", "date": "2026-11-20",
     "notes": ["…"], "python": ">=3.12,<3.14"}

Work in progress is pushed to the branch all the time, so the branch tip is never installed.
A release is cut by committing a change of *only* this file that names an already reviewed,
earlier commit (a file cannot name the SHA of the commit that contains it). The updater

1. fetches the branch tip over HTTPS from the configured repository (``[update]`` in config.toml
   only - never from a preset or from the repository content),
2. reads ``release.json`` from the tip *as data* (``git show``), nothing is executed,
3. requires the named commit to be an ancestor of the tip, its ``src/callrec/__init__.py`` to
   declare the same version, and the running Python to satisfy ``python``,
4. hands over to a small helper process outside the venv that waits for the app to exit, checks
   out exactly that commit (detached), verifies ``HEAD`` and a clean tree, reinstalls the
   dependencies only when the requirement files changed (with a copy of the venv for rollback),
   runs ``callrec postinstall`` and restarts the app - or rolls everything back.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from callrec import __version__, paths

log = logging.getLogger(__name__)

SUBDIR = "call-recorder"
RELEASE_FILE = f"{SUBDIR}/release.json"
TIP_REF = "refs/callrec/update-tip"
REMOTE = "callrec-update"  # remote whose URL is (re)set from config.toml before every fetch
# files whose change means "reinstall the dependencies"
REQUIREMENT_FILES = tuple(f"{SUBDIR}/{n}" for n in ("requirements.txt", "requirements-cuda.txt", "constraints-win.txt",
                                                     "pyproject.toml"))
FETCH_TIMEOUT_S = 180  # whole fetch; a VPN that blocks GitHub fails within this
GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",  # never hang on a console prompt (Git Credential Manager still shows its window)
    "GIT_HTTP_LOW_SPEED_LIMIT": "1000",  # abort a transfer slower than 1 kB/s ...
    "GIT_HTTP_LOW_SPEED_TIME": "30",  # ... for 30 s
    # no hooks (global or in .git/hooks) and no fsmonitor daemon for the update's git commands
    "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": os.devnull,
    "GIT_CONFIG_KEY_1": "core.fsmonitor", "GIT_CONFIG_VALUE_1": "false",
}
_SHA = re.compile(r"^[0-9a-f]{40}$")
_VERSION = re.compile(r"^\d+(\.\d+){1,3}$")
_VERSION_IN_SOURCE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
INSTALL_HINT = ("Git ist nicht installiert. Für Updates aus der App einmalig Git für Windows installieren: "
                "winget install Git.Git (danach CallRecorder neu starten).")


class UpdateError(RuntimeError):
    """Plain-German reason why checking or updating is not possible."""


Runner = Callable[[Sequence[str], Path | None, float], tuple[int, str, str]]


def run_git(args: Sequence[str], cwd: Path | None = None, timeout: float = 60.0) -> tuple[int, str, str]:
    env = {**os.environ, **GIT_ENV}
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True,  # noqa: S603, S607
                           timeout=timeout, env=env, encoding="utf-8", errors="replace", **kwargs)
    except FileNotFoundError:
        raise UpdateError(INSTALL_HINT) from None
    except subprocess.TimeoutExpired:
        raise UpdateError(f"Zeitüberschreitung nach {int(timeout)} s bei „git {args[0]}“ – ist GitHub erreichbar "
                          "(VPN, Proxy, Firewall)? Später erneut versuchen.") from None
    return p.returncode, p.stdout, p.stderr


@dataclass
class Release:
    version: str
    commit: str
    date: str = ""
    notes: list[str] = field(default_factory=list)
    python: str = ""

    @staticmethod
    def parse(text: str) -> Release:
        try:
            d = json.loads(text)
        except ValueError as exc:
            raise UpdateError(f"release.json ist ungültig: {exc}") from None
        if not isinstance(d, dict) or d.get("schema") != 1:
            raise UpdateError("release.json hat ein unbekanntes Format.")
        if d.get("commit") is None:
            raise UpdateError("Es ist noch keine Version zur Installation freigegeben (release.json nennt keinen Stand).")
        version, commit = str(d.get("version") or ""), str(d.get("commit") or "").lower()
        if not _VERSION.match(version) or not _SHA.match(commit):
            raise UpdateError("release.json nennt keine gültige Version oder keinen vollständigen Commit (40 Zeichen).")
        notes = d.get("notes") or []
        if isinstance(notes, str):
            notes = [notes]
        return Release(version, commit, str(d.get("date") or ""), [str(n) for n in notes], str(d.get("python") or ""))


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def python_ok(spec: str, version: tuple[int, int] | None = None) -> bool:
    """``spec`` like ">=3.12,<3.14" (only >=, >, <, <=, == of major.minor)."""
    cur = version or (sys.version_info[0], sys.version_info[1])
    for part in filter(None, (s.strip() for s in spec.split(","))):
        m = re.match(r"^(>=|<=|==|>|<)\s*(\d+)\.(\d+)$", part)
        if not m:
            continue
        op, want = m.group(1), (int(m.group(2)), int(m.group(3)))
        ok = {">=": cur >= want, "<=": cur <= want, "==": cur == want, ">": cur > want, "<": cur < want}[op]
        if not ok:
            return False
    return True


@dataclass
class UpdateCheck:
    installed_version: str
    installed_commit: str
    release: Release | None
    available: bool
    message: str  # German summary for the dialog / notification
    requirements_changed: bool = False
    repo_root: str = ""
    target: str = ""  # commit to install (release commit, or the previous one for a restore)
    target_version: str = ""


def app_root() -> Path:
    """The ``call-recorder`` folder of this installation."""
    return Path(__file__).resolve().parents[2]


def install_file() -> Path:
    return paths.data_dir() / "install.json"


def status_file() -> Path:
    return paths.data_dir() / "update_status.json"


def read_install_info() -> dict[str, Any]:
    try:
        d = json.loads(install_file().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


class Updater:
    def __init__(self, repo_url: str, branch: str, git: Runner = run_git, root: Path | None = None) -> None:
        validate_source(repo_url, branch)
        self.repo_url = repo_url
        self.branch = branch
        self.git = git
        self.root = root or app_root()

    def _ok(self, args: Sequence[str], timeout: float = 60.0, what: str = "") -> str:
        rc, out, err = self.git(args, self.repo_root(), timeout)
        if rc != 0:
            raise UpdateError(f"{what or 'git ' + args[0]} fehlgeschlagen: {(err or out).strip()[:300]}")
        return out.strip()

    def repo_root(self) -> Path:
        rc, out, _err = self.git(["rev-parse", "--show-toplevel"], self.root, 15)
        if rc != 0:
            raise UpdateError("Diese Installation ist kein Git-Arbeitsordner (z. B. aus einer ZIP-Datei). Einmalig "
                              "install-callrecorder.ps1 ausführen (README, Abschnitt „Updates“) – Einstellungen, "
                              "Modelle, Stimmprofile und Aufnahmen bleiben erhalten.")
        return Path(out.strip())

    def installed_commit(self) -> str:
        return self._ok(["rev-parse", "HEAD"], what="Installierten Stand lesen")

    def local_changes(self) -> list[str]:
        out = self._ok(["status", "--porcelain", "--untracked-files=no", "--", SUBDIR], what="Status lesen")
        return [line for line in out.splitlines() if line.strip()]

    def _set_remote(self) -> None:
        """The update source always comes from config.toml, never from what the checkout says."""
        rc, out, _e = self.git(["remote", "get-url", REMOTE], self.repo_root(), 15)
        if rc != 0:
            self._ok(["remote", "add", REMOTE, self.repo_url], 15, "Update-Quelle eintragen")
        elif out.strip() != self.repo_url:
            self._ok(["remote", "set-url", REMOTE, self.repo_url], 15, "Update-Quelle eintragen")

    def fetch_release(self) -> Release:
        self._set_remote()
        self._ok(["fetch", "--no-tags", "--filter=blob:none", REMOTE, f"+refs/heads/{self.branch}:{TIP_REF}"],
                 FETCH_TIMEOUT_S, "Abruf von GitHub")
        text = self._ok(["show", f"{TIP_REF}:{RELEASE_FILE}"], 60, "release.json lesen")
        rel = Release.parse(text)
        rc, _o, _e = self.git(["merge-base", "--is-ancestor", rel.commit, TIP_REF], self.repo_root(), 60)
        if rc != 0:
            raise UpdateError(f"Die Freigabe {rel.version} nennt den Stand {rel.commit[:10]}, der nicht zum Zweig "
                              f"{self.branch} gehört – Update abgelehnt.")
        src = self._ok(["show", f"{rel.commit}:{SUBDIR}/src/callrec/__init__.py"], 60, "Version des Stands lesen")
        m = _VERSION_IN_SOURCE.search(src)
        if not m or m.group(1) != rel.version:
            raise UpdateError(f"Der freigegebene Stand {rel.commit[:10]} meldet Version {m.group(1) if m else '?'}, "
                              f"release.json aber {rel.version} – Update abgelehnt.")
        return rel

    def check(self) -> UpdateCheck:
        root = self.repo_root()
        head = self.installed_commit()
        rel = self.fetch_release()
        installed = __version__
        if not python_ok(rel.python):
            return UpdateCheck(installed, head, rel, False,
                               f"Version {rel.version} braucht Python {rel.python}; installiert ist "
                               f"{sys.version_info[0]}.{sys.version_info[1]}. Bitte zuerst Python aktualisieren "
                               "und install-callrecorder.ps1 erneut ausführen.", repo_root=str(root))
        if rel.commit == head:
            return UpdateCheck(installed, head, rel, False, f"CallRecorder {installed} ist aktuell.", repo_root=str(root))
        rc, _o, _e = self.git(["merge-base", "--is-ancestor", head, rel.commit], root, 60)
        if rc != 0:
            return UpdateCheck(installed, head, rel, False,
                               f"Installiert ist ein abweichender oder neuerer Stand ({head[:10]}) als die Freigabe "
                               f"{rel.version} – kein Update angeboten.", repo_root=str(root))
        rc, _o, _e = self.git(["diff", "--quiet", head, rel.commit, "--", *REQUIREMENT_FILES], root, 120)
        return UpdateCheck(installed, head, rel, True, f"Version {rel.version} ist verfügbar (installiert: {installed}).",
                           requirements_changed=rc != 0, repo_root=str(root), target=rel.commit,
                           target_version=rel.version)


def restore_check(up: Updater, previous: str, previous_version: str = "") -> UpdateCheck:
    """ "Vorherige Version wiederherstellen": back to the commit installed before the last update."""
    if not _SHA.match(previous):
        raise UpdateError("Keine vorherige Version bekannt.")
    root = up.repo_root()
    head = up.installed_commit()
    rc, _o, _e = up.git(["cat-file", "-e", f"{previous}^{{commit}}"], root, 30)
    if rc != 0:
        raise UpdateError("Der vorherige Stand ist im Programmordner nicht mehr vorhanden.")
    rc, _o, _e = up.git(["diff", "--quiet", head, previous, "--", *REQUIREMENT_FILES], root, 120)
    return UpdateCheck(__version__, head, None, True, f"Zurück auf den Stand {previous[:10]}.",
                       requirements_changed=rc != 0, repo_root=str(root), target=previous,
                       target_version=previous_version)


def validate_source(repo_url: str, branch: str) -> None:
    if not re.match(r"^https://github\.com/[\w.-]+/[\w.-]+(\.git)?$", repo_url):
        raise UpdateError(f"Update-Quelle {repo_url!r} ist nicht erlaubt (nur https://github.com/<Besitzer>/<Repository>).")
    if not re.match(r"^[\w./-]+$", branch) or ".." in branch or branch.startswith(("-", "/")):
        raise UpdateError(f"Ungültiger Zweig {branch!r}.")


# ---------------------------------------------------------------------------
# hand-over to the helper process
# ---------------------------------------------------------------------------
def helper_plan(check: UpdateCheck, target: str, version: str, mode: str = "update") -> dict[str, Any]:
    """Everything the helper needs (no code of the new version runs before its checks)."""
    exe = Path(sys.executable)
    scripts = exe.parent
    pythonw = scripts / ("pythonw.exe" if sys.platform == "win32" else exe.name)
    import importlib.util

    try:
        cuda = importlib.util.find_spec("nvidia.cublas") is not None
    except (ImportError, ValueError):
        cuda = False
    return {
        "mode": mode,  # update | restore
        "repo_root": check.repo_root,
        "app_root": str(app_root()),
        "venv_python": str(scripts / ("python.exe" if sys.platform == "win32" else exe.name)),
        "venv_dir": str(Path(sys.prefix)),
        "target": target,
        "version": version,
        "previous": check.installed_commit,
        "previous_version": check.installed_version,
        "requirements_changed": check.requirements_changed,
        "with_cuda": cuda,
        "app_pid": os.getpid(),
        "restart": [str(pythonw if pythonw.exists() else exe), "-m", "callrec", "tray"],
        "log": str(paths.log_dir() / "update.log"),
        "status": str(status_file()),
        "install": str(install_file()),
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def base_python() -> str:
    """The interpreter the venv was made from (its files are not locked by the running app)."""
    base = Path(getattr(sys, "_base_executable", "") or sys.executable)
    if sys.platform == "win32":
        w = base.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return str(base)


def launch_helper(plan: dict[str, Any], popen: Callable[..., Any] = subprocess.Popen) -> Path:
    """Copy the helper out of the program folder and start it detached; the app must quit next."""
    from callrec import update_helper

    work = Path(tempfile.mkdtemp(prefix="callrec_update_"))
    script = work / "callrec_update_helper.py"
    shutil.copy2(update_helper.__file__, script)
    plan_file = work / "plan.json"
    plan_file.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    kwargs: dict[str, Any] = {"cwd": str(work), "close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    popen([base_python(), "-I", str(script), str(plan_file)], **kwargs)  # noqa: S603
    log.info("Update-Helfer gestartet (%s -> %s)", plan["previous"][:10], plan["target"][:10])
    return plan_file


def take_status() -> dict[str, Any] | None:
    """Result of the last update run (shown once at the next start)."""
    p = status_file()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("shown"):
        return None
    d["shown"] = True
    try:
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return d


def record_install(git: Runner = run_git, root: Path | None = None) -> dict[str, Any]:
    """``callrec postinstall``: remember the installed commit/version (keeps the previous one)."""
    rc, out, _e = git(["rev-parse", "HEAD"], root or app_root(), 15)
    commit = out.strip() if rc == 0 else ""
    info = read_install_info()
    if commit and info.get("commit") and info.get("commit") != commit:
        info["previous_commit"], info["previous_version"] = info["commit"], info.get("version", "")
    info.update({"commit": commit, "version": __version__, "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")})
    install_file().parent.mkdir(parents=True, exist_ok=True)
    install_file().write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return info


def version_text() -> str:
    info = read_install_info()
    commit = str(info.get("commit") or "")
    return f"CallRecorder {__version__}" + (f" ({commit[:10]})" if commit else "")


def as_dict(check: UpdateCheck) -> dict[str, Any]:
    return asdict(check)
