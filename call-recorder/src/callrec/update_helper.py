"""Update helper: runs OUTSIDE the app and its venv (base Python, ``-I``), standard library only.

Started by :func:`callrec.updater.launch_helper` with a plan file. Steps:

1. wait until the app (``app_pid``) has exited - its venv files are locked on Windows while it runs;
2. ``git checkout --detach <target>``; verify ``HEAD == target`` and a clean ``call-recorder`` tree
   (nothing of the new version runs before this check);
3. only if the requirement files changed: copy the venv to ``.venv.previous`` and reinstall the
   pinned dependencies (plus the NVIDIA packages if they were installed);
4. run ``python -m callrec postinstall`` of the new version (shortcut, autostart, GPU check,
   install record) and ``python -m callrec --version`` as smoke test;
5. restart the app. On any failure: check out the previous commit, put the previous venv back,
   write the reason to the status file (the app shows it at its next start) and restart the app.

Shows a small progress window when tkinter is available, otherwise runs silently (log file).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_HTTP_LOW_SPEED_LIMIT": "1000", "GIT_HTTP_LOW_SPEED_TIME": "30"}
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class Failed(RuntimeError):
    pass


class Helper:
    def __init__(self, plan: dict[str, Any], report: Any = None) -> None:
        self.p = plan
        self.report = report or (lambda text: None)
        self.log_path = Path(plan["log"])
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.venv_replaced = False

    # -- helpers ---------------------------------------------------------
    def log(self, text: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')} {text}"
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def step(self, text: str) -> None:
        self.log(text)
        self.report(text)

    def run(self, args: list[str], cwd: str | None = None, timeout: float = 1800, git: bool = False) -> str:
        env = {**os.environ, **GIT_ENV} if git else dict(os.environ)
        self.log("$ " + " ".join(args))
        try:
            p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env,  # noqa: S603
                               encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Failed(f"{args[0]} nicht ausführbar: {exc}") from exc
        self.log((p.stdout + p.stderr)[-4000:])
        if p.returncode != 0:
            raise Failed(f"„{' '.join(args[:4])} …“ fehlgeschlagen (Code {p.returncode}): {(p.stderr or p.stdout).strip()[-300:]}")
        return p.stdout.strip()

    def git(self, *args: str) -> str:
        return self.run(["git", *args], cwd=self.p["repo_root"], timeout=600, git=True)

    def wait_for_app(self, timeout: float = 90) -> None:
        pid = int(self.p.get("app_pid") or 0)
        end = time.monotonic() + timeout
        while pid and time.monotonic() < end:
            if not _alive(pid):
                return
            time.sleep(0.5)
        if pid and _alive(pid):
            raise Failed("CallRecorder wurde nicht beendet – Update abgebrochen, nichts geändert.")

    def checkout(self, commit: str) -> None:
        self.git("-c", "advice.detachedHead=false", "checkout", "--detach", commit)
        head = self.git("rev-parse", "HEAD")
        if head != commit:
            raise Failed(f"Nach dem Auschecken steht {head[:10]} statt {commit[:10]} – abgebrochen.")
        dirty = self.git("status", "--porcelain", "--untracked-files=no", "--", "call-recorder")
        if dirty:
            raise Failed("Der Programmordner enthält nach dem Auschecken Änderungen – abgebrochen.")

    def backup_venv(self) -> None:
        venv, prev = Path(self.p["venv_dir"]), Path(self.p["venv_dir"] + ".previous")
        if prev.exists():
            shutil.rmtree(prev)
        self.step("Sichere die bisherige Python-Umgebung …")
        shutil.copytree(venv, prev, symlinks=True)
        self.venv_replaced = True

    def restore_venv(self) -> None:
        venv, prev = Path(self.p["venv_dir"]), Path(self.p["venv_dir"] + ".previous")
        if not prev.exists():
            return
        broken = Path(self.p["venv_dir"] + ".failed")
        if broken.exists():
            shutil.rmtree(broken, ignore_errors=True)
        if venv.exists():
            venv.rename(broken)
        shutil.copytree(prev, venv, symlinks=True)
        shutil.rmtree(broken, ignore_errors=True)

    def install_requirements(self) -> None:
        py, root = self.p["venv_python"], self.p["app_root"]
        self.step("Installiere geänderte Abhängigkeiten (kann einige Minuten dauern) …")
        self.run([py, "-m", "pip", "install", "-r", "requirements.txt", "-c", "constraints-win.txt"], cwd=root)
        if self.p.get("with_cuda"):
            self.run([py, "-m", "pip", "install", "-r", "requirements-cuda.txt", "-c", "constraints-win.txt"], cwd=root)
        self.run([py, "-m", "pip", "install", "--no-deps", "-e", "."], cwd=root)

    def post_install(self) -> None:
        py, root = self.p["venv_python"], self.p["app_root"]
        self.step("Verknüpfung, Autostart und GPU prüfen …")
        self.run([py, "-m", "callrec", "postinstall"], cwd=root, timeout=600)
        out = self.run([py, "-m", "callrec", "--version"], cwd=root, timeout=120)
        if self.p.get("version") and self.p["version"] not in out:
            raise Failed(f"Die neue Version meldet sich als „{out}“ statt {self.p['version']}.")

    def write_status(self, ok: bool, message: str) -> None:
        data = {"ok": ok, "message": message, "mode": self.p.get("mode", "update"), "target": self.p["target"],
                "version": self.p.get("version", ""), "previous": self.p.get("previous", ""),
                "at": datetime.now().astimezone().isoformat(timespec="seconds"), "log": str(self.log_path)}
        Path(self.p["status"]).parent.mkdir(parents=True, exist_ok=True)
        Path(self.p["status"]).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def restart(self) -> None:
        if not self.p.get("restart"):
            return
        try:
            subprocess.Popen(self.p["restart"], cwd=self.p["app_root"], close_fds=True,  # noqa: S603
                             creationflags=0x00000008 if sys.platform == "win32" else 0)
        except OSError as exc:
            self.log(f"Neustart fehlgeschlagen: {exc}")

    # -- the run -----------------------------------------------------------
    def main(self) -> bool:
        target, previous = self.p["target"], self.p["previous"]
        what = "Wiederherstellung" if self.p.get("mode") == "restore" else "Update"
        try:
            self.step("Warte, bis CallRecorder beendet ist …")
            self.wait_for_app()
        except Failed as exc:
            self.write_status(False, str(exc))
            return False
        try:
            self.step(f"Wechsle auf Version {self.p.get('version', '')} ({target[:10]}) …")
            self.checkout(target)
            if self.p.get("mode") == "restore" and Path(self.p["venv_dir"] + ".previous").exists() \
                    and self.p.get("requirements_changed"):
                self.step("Stelle die vorherige Python-Umgebung wieder her …")
                self.restore_venv()
            elif self.p.get("requirements_changed"):
                self.backup_venv()
                self.install_requirements()
            self.post_install()
        except Exception as exc:  # noqa: BLE001 - every failure is rolled back
            self.step(f"{what} fehlgeschlagen: {exc} – stelle den vorherigen Stand wieder her …")
            reason = str(exc)
            try:
                self.git("-c", "advice.detachedHead=false", "checkout", "--detach", previous)
                if self.venv_replaced:
                    self.restore_venv()
                rolled = "Der vorherige Stand wurde wiederhergestellt."
            except Exception as exc2:  # noqa: BLE001
                rolled = f"Auch die Wiederherstellung schlug fehl ({exc2}) – bitte install-callrecorder.ps1 erneut ausführen."
            self.write_status(False, f"{what} auf {self.p.get('version', '')} fehlgeschlagen: {reason} {rolled} "
                                     f"Einzelheiten: {self.log_path}")
            self.restart()
            return False
        self.write_status(True, f"{what} abgeschlossen: CallRecorder {self.p.get('version', '')} ({target[:10]}).")
        self.step("Fertig – CallRecorder wird neu gestartet.")
        self.restart()
        return True


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)  # QUERY_LIMITED | SYNCHRONIZE
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT = running
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def main(argv: list[str]) -> int:
    plan = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    try:
        import tkinter as tk
    except ImportError:
        return 0 if Helper(plan).main() else 1
    root = tk.Tk()
    root.title("CallRecorder wird aktualisiert")
    var = tk.StringVar(value="Vorbereitung …")
    tk.Label(root, textvariable=var, width=70, anchor="w", justify="left", padx=16, pady=16, wraplength=480).pack()
    result: dict[str, bool] = {}

    def work() -> None:
        result["ok"] = Helper(plan, report=lambda t: root.after(0, var.set, t)).main()
        root.after(1500, root.destroy)

    threading.Thread(target=work, daemon=True).start()
    root.mainloop()
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
