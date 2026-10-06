"""Minimal read-only HTTP client for the local ComfyUI server.

Only GET requests exist in this module on purpose: the inspector must never
change the queue. Python's urllib sends no Origin header, so ComfyUI's
origin-check middleware (which rejects cross-origin browser requests) does
not apply - this is why the browser talks to us and we talk to ComfyUI.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

USER_AGENT = "ComfyUI-Queue-Inspector"


class ComfyUnavailable(Exception):
    pass


class ComfyClient:
    def __init__(self, base_url: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _url(self, path: str, query: Optional[Dict[str, str]] = None) -> str:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    def _get(self, path: str, query=None, timeout=None) -> Tuple[int, Dict[str, str], bytes]:
        req = urllib.request.Request(self._url(path, query), headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), b""
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ComfyUnavailable(str(exc)) from exc

    def get_json(self, path: str, timeout=None) -> Any:
        status, _, body = self._get(path, timeout=timeout)
        if status != 200:
            raise ComfyUnavailable(f"{path} returned HTTP {status}")
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise ComfyUnavailable(f"{path} returned invalid JSON") from exc

    def get_queue(self) -> Any:
        return self.get_json("/queue")

    def get_queue_remaining(self) -> Optional[int]:
        """Cheap check via GET /prompt -> {"exec_info": {"queue_remaining": N}}."""
        data = self.get_json("/prompt")
        try:
            n = data["exec_info"]["queue_remaining"]
            return int(n)
        except (KeyError, TypeError, ValueError):
            return None

    def get_object_info(self) -> Any:
        return self.get_json("/object_info", timeout=max(self.timeout, 30.0))

    def get_image(self, filename: str, subfolder: str, img_type: str) -> Tuple[int, str, bytes]:
        query = {"filename": filename, "type": img_type}
        if subfolder:
            query["subfolder"] = subfolder
        status, headers, body = self._get("/view", query, timeout=max(self.timeout, 15.0))
        ctype = headers.get("Content-Type") or headers.get("content-type") or "application/octet-stream"
        return status, ctype, body
