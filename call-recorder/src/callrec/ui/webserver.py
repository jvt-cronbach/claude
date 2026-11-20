"""Loopback-only HTTP server for the bundled web UI.

* binds to 127.0.0.1 on a random port; every URL starts with a per-session random token
  (``/<token>/index.html``) - a request without it gets 404, as does a foreign Host header
  (DNS rebinding);
* serves only the files that ship in ``callrec/ui/web`` (fixed list taken at start, no
  directories, no path tricks) and clips that the app published explicitly (speaker samples);
* sends a strict Content-Security-Policy: scripts, styles, media only from this origin, no
  remote content at all.

pywebview's own server is not used: it serves a whole directory without a token.
"""

from __future__ import annotations

import logging
import mimetypes
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
STATIC_SUFFIXES = {".html", ".css", ".js", ".svg", ".png", ".ico", ".woff2"}
# 'unsafe-eval': pywebview returns the results of js_api calls and runs evaluate_js through eval()
# (webview/util.py js_bridge_call, webview/window.py evaluate_js); no page script uses eval.
CSP = ("default-src 'none'; script-src 'self' 'unsafe-eval'; style-src 'self'; img-src 'self' data:; "
       "media-src 'self'; font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'; object-src 'none'")


def static_files(root: Path = WEB_DIR) -> dict[str, Path]:
    out: dict[str, Path] = {}
    if root.is_dir():
        for p in root.iterdir():
            if p.is_file() and p.suffix.lower() in STATIC_SUFFIXES and not p.name.startswith("."):
                out[p.name] = p.resolve()
    return out


class UiServer:
    def __init__(self, root: Path = WEB_DIR, host: str = "127.0.0.1") -> None:
        self.token = secrets.token_urlsafe(24)
        self.files = static_files(root)
        self._clips: dict[str, Path] = {}
        self._lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "CallRecorder"
            sys_version = ""

            def log_message(self, fmt: str, *args: Any) -> None:  # quiet
                log.debug("ui-server: " + fmt, *args)

            def do_HEAD(self) -> None:  # noqa: N802
                self._serve(head=True)

            def do_GET(self) -> None:  # noqa: N802
                self._serve(head=False)

            def _deny(self, code: int = 404) -> None:
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()

            def _serve(self, head: bool) -> None:
                host = (self.headers.get("Host") or "").strip()
                if host not in server.allowed_hosts():
                    self._deny(403)
                    return
                path = server.resolve(self.path)
                if path is None:
                    self._deny()
                    return
                try:
                    data = path.read_bytes()
                except OSError:
                    self._deny()
                    return
                ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
                    ctype += "; charset=utf-8"
                status, start, end = 200, 0, len(data)
                rng = self.headers.get("Range") or ""
                if rng.startswith("bytes=") and data:
                    try:
                        a, _, b = rng[6:].split(",")[0].partition("-")
                        start = int(a) if a else max(0, len(data) - int(b))
                        end = (int(b) + 1) if (a and b) else len(data)
                        end = min(end, len(data))
                        if start >= end:
                            raise ValueError
                        status = 206
                    except ValueError:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{len(data)}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                body = data[start:end]
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end - 1}/{len(data)}")
                self.send_header("Content-Security-Policy", CSP)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if not head:
                    self.wfile.write(body)

        self.httpd = ThreadingHTTPServer((host, 0), Handler)
        self.httpd.daemon_threads = True
        self.host = host
        self.port = int(self.httpd.server_address[1])
        self._thread: threading.Thread | None = None

    def allowed_hosts(self) -> set[str]:
        return {f"{self.host}:{self.port}", f"localhost:{self.port}"}

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"

    def url(self, name: str = "index.html") -> str:
        return f"{self.origin}/{self.token}/{name}"

    def resolve(self, raw_path: str) -> Path | None:
        """The file for a request path, or None (wrong token, unknown file, anything else)."""
        parts = unquote(urlsplit(raw_path).path).split("/")
        if len(parts) < 3 or parts[0] != "" or not secrets.compare_digest(parts[1], self.token):
            return None
        rest = parts[2:]
        if len(rest) == 1:
            return self.files.get(rest[0])
        if len(rest) == 2 and rest[0] == "clip":
            with self._lock:
                return self._clips.get(rest[1])
        return None

    def publish(self, path: Path) -> str:
        """Make one file (a speaker clip) reachable under an unguessable name; returns its URL."""
        name = secrets.token_urlsafe(16) + path.suffix.lower()
        with self._lock:
            self._clips[name] = Path(path).resolve()
            if len(self._clips) > 200:  # keep the registry small
                for k in list(self._clips)[:100]:
                    self._clips.pop(k, None)
        return f"{self.origin}/{self.token}/clip/{name}"

    def is_own_url(self, url: str) -> bool:
        u = urlsplit(url or "")
        return f"{u.scheme}://{u.netloc}" == self.origin and u.path.startswith(f"/{self.token}/")

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="ui-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            log.debug("UI-Server nicht sauber beendet", exc_info=True)
