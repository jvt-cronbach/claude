"""Playwright/Chromium drives the real web frontend (served by the real loopback server) against a
fake bridge whose data comes from the real DTO functions (tests/web_fixtures.py).

Skipped when Node.js with the ``playwright`` package (and its Chromium) is not available.
Screenshots: set CALLREC_UI_SCREENSHOTS to a folder.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _node_playwright() -> str | None:
    node = shutil.which("node")
    if node is None:
        return None
    try:
        r = subprocess.run([node, "-e", "require('playwright')"], capture_output=True, timeout=30)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired):
        return None
    return node if r.returncode == 0 else None


NODE = _node_playwright()
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js mit playwright nicht verfügbar")


def test_web_frontend_end_to_end(tmp_path: Path):
    from callrec.ui.webserver import UiServer
    from tests import web_fixtures

    fixtures = web_fixtures.write(tmp_path / "fx", web_fixtures.build(tmp_path / "recs"))
    server = UiServer()
    server.start()
    shots = os.environ.get("CALLREC_UI_SCREENSHOTS", "-")
    try:
        assert NODE is not None
        r = subprocess.run([NODE, str(HERE / "web" / "e2e.js"), server.url("index.html"), str(fixtures), shots],  # noqa: S603
                           capture_output=True, text=True, timeout=1500)
    finally:
        server.stop()
    lines = [x for x in r.stdout.strip().splitlines() if x.startswith("{")]
    assert lines, r.stdout[-2000:] + r.stderr[-2000:]
    results = json.loads(lines[-1])["results"]
    failed = {x["name"]: x["errors"] for x in results if not x["ok"]}
    assert len(results) >= 15
    assert not failed, json.dumps(failed, ensure_ascii=False, indent=1)
