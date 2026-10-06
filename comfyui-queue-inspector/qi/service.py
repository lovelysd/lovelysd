"""Queue snapshot service: decides when to re-read ComfyUI and caches results.

Refresh strategy (why not just fetch /queue every poll):
`/queue` contains the full prompt graph of every job, so with many pending
jobs it can be megabytes. `GET /prompt` returns only the remaining count and
is tiny. We fetch the full queue only when that count changes, on manual
refresh, or every FULL_REFRESH_SECONDS as a safety net (a job finishing and
another being queued between two polls leaves the count unchanged).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from .adapter import QueueFormatError
from .catalog import NodeCatalog
from .comfy_client import ComfyClient, ComfyUnavailable
from .viewmodel import QueueViewModel

FULL_REFRESH_SECONDS = 5.0
OBJECT_INFO_RETRY_SECONDS = 60.0
OBJECT_INFO_FIRST_RETRY_SECONDS = 10.0  # while not loaded yet (ComfyUI still starting)


class QueueService:
    def __init__(self, client: ComfyClient):
        self.client = client
        self.catalog = NodeCatalog()
        self.vm = QueueViewModel()
        self._lock = threading.Lock()
        self._version = 0
        self._signature: Optional[tuple] = None
        self._items: List[Dict[str, Any]] = []
        self._error: Optional[str] = None
        self._last_full = 0.0
        self._last_remaining: Optional[int] = None
        self._checked_at: Optional[float] = None
        self._object_info_loading = False
        self._object_info_last_try = 0.0
        self._unknown_seen = set()  # unknown node classes that already triggered a reload

    # ---------------------------------------------------------- object_info

    def _maybe_load_object_info(self, force: bool = False):
        now = time.time()
        if self._object_info_loading:
            return
        wait = OBJECT_INFO_RETRY_SECONDS if self.catalog.available else OBJECT_INFO_FIRST_RETRY_SECONDS
        if not force and now - self._object_info_last_try < wait:
            return
        self._object_info_loading = True
        self._object_info_last_try = now
        threading.Thread(target=self._load_object_info, daemon=True).start()

    def _load_object_info(self):
        try:
            info = self.client.get_object_info()
            if isinstance(info, dict) and info:
                with self._lock:
                    if set(info) != self.catalog.class_names():
                        self.catalog = NodeCatalog(info)
                        self.vm.clear_cache()  # re-parse with better type info
                        self._last_full = 0.0  # force a full refresh on next poll
        except ComfyUnavailable:
            pass
        finally:
            self._object_info_loading = False

    def start(self):
        self._maybe_load_object_info(force=True)

    # ---------------------------------------------------------- snapshot

    def snapshot(self, force: bool = False) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            try:
                remaining = self.client.get_queue_remaining()
                need_full = (
                    force
                    or self._error is not None
                    or remaining is None
                    or remaining != self._last_remaining
                    or now - self._last_full >= FULL_REFRESH_SECONDS
                )
                if need_full:
                    payload = self.client.get_queue()
                    # Reload node type info if ComfyUI was not ready yet, or a job uses a
                    # node class we have not seen (e.g. a custom node installed later).
                    unknown = self.vm.unknown_classes(payload, self.catalog) - self._unknown_seen
                    if unknown:
                        self._unknown_seen |= unknown
                        self._maybe_load_object_info(force=True)
                    elif not self.catalog.available:
                        self._maybe_load_object_info()
                    self._items = self.vm.build(payload, self.catalog)
                    self._last_full = now
                    self._last_remaining = remaining
                self._error = None
            except ComfyUnavailable:
                self._error = "Unable to read queue."
                self._items = []
                self._last_remaining = None
            except QueueFormatError as exc:
                self._error = f"Unable to read queue. Unexpected format: {exc}"
                self._items = []
                self._last_remaining = None
            self._checked_at = now

            signature = (self._error, tuple(
                (i["promptId"], i["status"], i["order"]) for i in self._items
            ), id(self.catalog))
            if signature != self._signature:
                self._signature = signature
                self._version += 1

            running = sum(1 for i in self._items if i["status"] == "running")
            return {
                "version": self._version,
                "ok": self._error is None,
                "error": self._error,
                "checkedAt": self._checked_at,
                "running": running,
                "pending": len(self._items) - running,
                "typeInfo": self.catalog.available,
                "items": self._items,
            }
