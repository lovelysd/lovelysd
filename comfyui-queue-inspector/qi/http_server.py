"""Local HTTP server: serves the web UI and a small read-only JSON API.

Routes
  GET /                    -> web/index.html
  GET /static/<file>       -> whitelisted files in web/
  GET /api/queue?v=N       -> normalized queue snapshot ({"unchanged": true} if version == N)
  GET /api/queue?force=1   -> same, but re-read ComfyUI immediately (manual refresh)
  GET /api/image?filename=&subfolder=&type=  -> proxied ComfyUI /view (input preview)
  GET /api/info            -> ComfyUI URL and version info
  POST /api/reorder        -> {"order": [prompt_id, ...]}  ONLY with --allow-reorder

Without --allow-reorder every non-GET request gets 405, so nothing can modify
ComfyUI. With it, /api/reorder is the single state-changing route; it only
accepts same-origin JSON requests carrying the X-Queue-Inspector header
(a cross-site page cannot send that header without a CORS preflight, which
this server never approves), so other websites cannot trigger it.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .comfy_client import ComfyUnavailable
from .service import QueueService

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "favicon.svg": "image/svg+xml",
}
IMAGE_TYPES = {"input", "output", "temp"}


def make_handler(service: QueueService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ComfyUIQueueInspector/" + __version__

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        # -------------------------------------------------- responses

        def _send(self, status: int, body: bytes, ctype: str, cache: str = "no-store"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, data):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        # -------------------------------------------------- routing

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)
            try:
                if path in ("/", "/index.html"):
                    return self._static("index.html")
                if path.startswith("/static/"):
                    return self._static(path[len("/static/"):])
                if path == "/api/queue":
                    return self._queue(query)
                if path == "/api/image":
                    return self._image(query)
                if path == "/api/info":
                    return self._json(200, {"comfyUrl": service.client.base_url, "version": __version__})
                self._json(404, {"error": "not found"})
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/reorder" and service.reorderer is not None:
                try:
                    return self._reorder()
                except (BrokenPipeError, ConnectionResetError):
                    return
            self._json(405, {"error": "read-only"})

        def _not_allowed(self):
            self._json(405, {"error": "read-only"})

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

        def _same_origin(self) -> bool:
            if self.headers.get("X-Queue-Inspector") != "1":
                return False
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return False
            origin = self.headers.get("Origin")
            host = self.headers.get("Host")
            return origin is None or (host is not None and origin == "http://" + host)

        def _reorder(self):
            if not self._same_origin():
                return self._json(403, {"error": "forbidden"})
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 1_000_000:
                return self._json(400, {"error": "bad request"})
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return self._json(400, {"error": "bad request"})
            order = data.get("order") if isinstance(data, dict) else None
            if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
                return self._json(400, {"error": "order must be a list of prompt ids"})
            self._json(200, service.reorder(order))

        # -------------------------------------------------- handlers

        def _static(self, name: str):
            if name not in STATIC_FILES:
                return self._json(404, {"error": "not found"})
            with open(os.path.join(WEB_DIR, name), "rb") as f:
                body = f.read()
            self._send(200, body, STATIC_FILES[name])

        def _queue(self, query):
            force = query.get("force", ["0"])[0] == "1"
            snap = service.snapshot(force=force)
            known = query.get("v", [None])[0]
            if known is not None and not force and known == str(snap["version"]):
                slim = {k: v for k, v in snap.items() if k != "items"}
                slim["unchanged"] = True
                return self._json(200, slim)
            self._json(200, snap)

        def _image(self, query):
            filename = query.get("filename", [""])[0]
            subfolder = query.get("subfolder", [""])[0]
            img_type = query.get("type", ["input"])[0]
            if not filename or img_type not in IMAGE_TYPES:
                return self._json(400, {"error": "bad request"})
            try:
                status, ctype, body = service.client.get_image(filename, subfolder, img_type)
            except ComfyUnavailable:
                return self._json(502, {"error": "comfy_unreachable", "comfyStatus": None,
                                        "message": "Unable to reach ComfyUI at " + service.client.base_url})
            # Errors carry the reason so the preview can say *why* (missing file,
            # rejected path, not an image) instead of a bare "not found".
            if status != 200:
                return self._json(404, {"error": "comfy_status", "comfyStatus": status,
                                        "message": "ComfyUI /view returned HTTP %d" % status})
            if not ctype.startswith("image/"):
                return self._json(415, {"error": "not_image", "comfyStatus": status, "contentType": ctype,
                                        "message": "ComfyUI returned %s, not an image" % ctype})
            self._send(200, body, ctype, cache="private, max-age=60")

    return Handler


def serve(service: QueueService, host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    httpd.daemon_threads = True
    return httpd
