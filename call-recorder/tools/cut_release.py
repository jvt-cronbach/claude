"""Maintainer: point release.json at a reviewed commit (run from the repository, standard library only).

    python call-recorder/tools/cut_release.py <reviewed-commit> [--notes "Zeile 1" --notes "Zeile 2"]

Scheme (a file cannot name the SHA of the commit that contains it): first commit and review the
version itself (with ``__version__`` raised in ``src/callrec/__init__.py``), then run this script with
that reviewed commit and commit ONLY ``call-recorder/release.json`` on top. The updater installs the
named commit - never the branch tip and never the pointer commit - after checking that the named
commit is an ancestor of the branch tip and declares the same version.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

SUBDIR = "call-recorder"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()  # noqa: S603, S607


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("commit")
    ap.add_argument("--notes", action="append", default=[])
    ap.add_argument("--python", default=">=3.12,<3.14")
    a = ap.parse_args(argv)
    sha = git("rev-parse", "--verify", f"{a.commit}^{{commit}}")
    subprocess.run(["git", "merge-base", "--is-ancestor", sha, "HEAD"], check=True)  # noqa: S603, S607
    src = git("show", f"{sha}:{SUBDIR}/src/callrec/__init__.py")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', src, re.MULTILINE)
    if not m:
        print("__version__ nicht gefunden", file=sys.stderr)
        return 1
    root = Path(git("rev-parse", "--show-toplevel"))
    path = root / SUBDIR / "release.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if old.get("version") == m.group(1) and old.get("commit") not in (None, sha):
        print(f"Version {m.group(1)} ist schon als {old['commit'][:10]} freigegeben - erst __version__ erhöhen.",
              file=sys.stderr)
        return 1
    data = {"schema": 1, "version": m.group(1), "commit": sha, "date": date.today().isoformat(),
            "notes": a.notes or old.get("notes", []), "python": a.python}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"release.json -> {m.group(1)} ({sha[:10]}). Jetzt NUR diese Datei committen und pushen:\n"
          f"  git add {SUBDIR}/release.json && git commit -m \"Release {m.group(1)}\" && git push")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
