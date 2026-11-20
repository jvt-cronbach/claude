"""Generate the CallRecorder app icon (SVG -> PNG -> multi-resolution ICO), reproducibly.

    python tools/make_icons.py

Needs Node.js with the ``playwright`` package and its Chromium (renders the SVG exactly as a
browser does) - only on the developer machine. The generated files are committed, so the user's PC
needs no tooling. Output: ``src/callrec/ui/assets`` (ICO, PNGs, SVGs) and ``src/callrec/ui/web/favicon.svg``.

Artwork: a white microphone in a rounded indigo square with a small red "recording" dot - the same
mark as the web UI header. 16-24 px use a simplified glyph (no cradle arc, thicker shapes, bigger
dot) instead of a scaled-down copy, so it stays crisp in the taskbar and the tray.
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "callrec" / "ui" / "assets"
WEB = ROOT / "src" / "callrec" / "ui" / "web"

INDIGO = "#5B5FC7"
RED = "#C4314B"
AMBER = "#B85E00"
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 256)
PNG_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def svg_full(fill: str = INDIGO, dot: str = RED, ring: str = "#fff") -> str:
    """32-unit artwork (32 px and larger)."""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        f'<rect x="1" y="1" width="30" height="30" rx="8" fill="{fill}"/>'
        '<rect x="12" y="6.5" width="8" height="12.5" rx="4" fill="#fff"/>'
        '<path d="M8.75 15.5a7.25 7.25 0 0 0 14.5 0" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>'
        '<path d="M16 22.75V25.5" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>'
        '<path d="M12.5 25.6h7" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>'
        f'<circle cx="24.6" cy="7.6" r="3.6" fill="{ring}"/><circle cx="24.6" cy="7.6" r="2.2" fill="{dot}"/>'
        "</svg>"
    )


def svg_small(fill: str = INDIGO, dot: str = RED, ring: str = "#fff") -> str:
    """16-unit simplified glyph (16-24 px): capsule + stem + base, big dot, pixel-aligned."""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
        f'<rect x="0" y="0" width="16" height="16" rx="3.5" fill="{fill}"/>'
        '<rect x="5" y="2.5" width="5" height="8" rx="2.5" fill="#fff"/>'
        '<rect x="7" y="10" width="1" height="2.5" fill="#fff"/>'
        '<rect x="4.5" y="12.5" width="6" height="1.5" rx="0.75" fill="#fff"/>'
        f'<circle cx="12.6" cy="3.4" r="2.6" fill="{ring}"/><circle cx="12.6" cy="3.4" r="1.6" fill="{dot}"/>'
        "</svg>"
    )


VARIANTS = {
    "callrec": dict(fill=INDIGO, dot=RED, ring="#fff"),
    # while recording: the whole mark turns red (window icon / taskbar button), the dot white
    "callrec-recording": dict(fill=RED, dot="#fff", ring=RED),
    # tray only: processing
    "callrec-busy": dict(fill=AMBER, dot=RED, ring="#fff"),
}

RENDER_JS = r"""
const { chromium } = require('playwright');
const jobs = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ deviceScaleFactor: 1 });
  for (const j of jobs) {
    await page.setViewportSize({ width: j.size, height: j.size });
    await page.setContent(`<html><body style="margin:0;background:transparent">` +
      j.svg.replace('<svg ', `<svg width="${j.size}" height="${j.size}" style="display:block" `) + `</body></html>`);
    await page.screenshot({ path: j.out, omitBackground: true, clip: { x: 0, y: 0, width: j.size, height: j.size } });
  }
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
"""


def write_ico(path: Path, pngs: list[tuple[int, bytes]]) -> None:
    """ICO container with PNG-compressed entries (supported by Windows Vista and later at every size)."""
    header = struct.pack("<HHH", 0, 1, len(pngs))
    entries = b""
    offset = 6 + 16 * len(pngs)
    data = b""
    for size, png in pngs:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        data += png
    path.write_bytes(header + entries + data)


def ico_sizes(path: Path) -> list[int]:
    """The image sizes stored in an ICO file (for tests)."""
    raw = path.read_bytes()
    _r, kind, n = struct.unpack_from("<HHH", raw, 0)
    if kind != 1:
        raise ValueError("kein ICO")
    return [(raw[6 + 16 * i] or 256) for i in range(n)]


def main() -> int:
    node = shutil.which("node")
    if node is None:
        print("Node.js mit playwright wird benötigt.", file=sys.stderr)
        return 1
    ASSETS.mkdir(parents=True, exist_ok=True)
    (ASSETS / "png").mkdir(exist_ok=True)
    jobs = []
    for name, colours in VARIANTS.items():
        (ASSETS / f"{name}.svg").write_text(svg_full(**colours), encoding="utf-8")
        (ASSETS / f"{name}-small.svg").write_text(svg_small(**colours), encoding="utf-8")
        for size in sorted(set(ICO_SIZES) | set(PNG_SIZES)):
            svg = svg_small(**colours) if size <= 24 else svg_full(**colours)
            jobs.append({"svg": svg, "size": size, "out": str(ASSETS / "png" / f"{name}-{size}.png")})
    with tempfile.TemporaryDirectory() as tmp:
        jf = Path(tmp) / "jobs.json"
        jf.write_text(json.dumps(jobs), encoding="utf-8")
        js = Path(tmp) / "render.js"
        js.write_text(RENDER_JS, encoding="utf-8")
        subprocess.run([node, str(js), str(jf)], check=True, cwd=tmp)  # noqa: S603
    for name in ("callrec", "callrec-recording"):
        write_ico(ASSETS / f"{name}.ico", [(s, (ASSETS / "png" / f"{name}-{s}.png").read_bytes()) for s in ICO_SIZES])
    # keep only the PNGs that are used (window/tray/README) besides the ICO sources
    (WEB / "favicon.svg").write_text(svg_full(), encoding="utf-8")
    print(f"Icons geschrieben: {ASSETS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
