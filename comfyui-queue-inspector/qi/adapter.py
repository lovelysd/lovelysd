"""Adapter for the raw `GET /queue` payload.

This is the ONLY module that knows the shape of ComfyUI's queue items.
If a future ComfyUI version changes the tuple layout, fix it here.

ComfyUI-version-dependent facts (verified against ComfyUI server.py /
execution.py, 2026-10):

* `/queue` returns {"queue_running": [...], "queue_pending": [...]}.
* Each item is a list: [number, prompt_id, prompt, extra_data, outputs_to_execute].
  The server stores a 6th element (sensitive data) but strips it before
  responding. We read only indices 0..4 and tolerate extra elements.
* `outputs_to_execute` lists the output node ids that passed validation.
  The prompt graph also contains nodes that feed none of them (e.g. a stray
  Load Image left in a template); ComfyUI neither validates nor runs those.
* `queue_pending` is a copy of a Python `heapq` list. Heap order is NOT
  execution order; execution order is ascending `number` (items queued with
  "front" get a negative number). We therefore sort here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional


class QueueFormatError(ValueError):
    """The payload does not look like a ComfyUI queue at all."""


@dataclass
class RawQueueItem:
    status: str  # "running" | "pending"
    prompt_id: str
    number: Optional[float]
    prompt: dict
    extra_data: dict
    outputs: List[str] = field(default_factory=list)  # outputs_to_execute
    warnings: List[str] = field(default_factory=list)


def _parse_item(item: Any, status: str) -> Optional[RawQueueItem]:
    if not isinstance(item, (list, tuple)) or len(item) < 2:
        return None

    prompt_id = item[1]
    if not isinstance(prompt_id, str) or not prompt_id:
        return None

    warnings: List[str] = []

    number = item[0]
    if isinstance(number, bool) or not isinstance(number, (int, float)):
        warnings.append("queue number missing or not numeric")
        number = None

    prompt = item[2] if len(item) > 2 else None
    if not isinstance(prompt, dict):
        warnings.append("prompt graph missing or not an object")
        prompt = {}

    extra_data = item[3] if len(item) > 3 else None
    if not isinstance(extra_data, dict):
        extra_data = {}

    outputs = item[4] if len(item) > 4 else None
    outputs = [str(o) for o in outputs] if isinstance(outputs, (list, tuple)) else []

    return RawQueueItem(
        status=status,
        prompt_id=prompt_id,
        number=number,
        prompt=prompt,
        extra_data=extra_data,
        outputs=outputs,
        warnings=warnings,
    )


def _sort_key(item: RawQueueItem):
    # Items without a number go last but keep their relative order (sort is stable).
    return (item.number is None, item.number if item.number is not None else 0)


def parse_queue(payload: Any) -> List[RawQueueItem]:
    """Return running items first, then pending items in execution order."""
    if not isinstance(payload, dict):
        raise QueueFormatError("queue payload is not an object")
    if "queue_running" not in payload or "queue_pending" not in payload:
        raise QueueFormatError("queue payload lacks queue_running / queue_pending")

    running_raw = payload["queue_running"]
    pending_raw = payload["queue_pending"]
    if not isinstance(running_raw, list) or not isinstance(pending_raw, list):
        raise QueueFormatError("queue_running / queue_pending are not lists")

    running = [p for p in (_parse_item(x, "running") for x in running_raw) if p]
    pending = [p for p in (_parse_item(x, "pending") for x in pending_raw) if p]

    running.sort(key=_sort_key)
    pending.sort(key=_sort_key)

    # A prompt_id should appear once; keep the first occurrence defensively.
    seen = set()
    out: List[RawQueueItem] = []
    for item in running + pending:
        if item.prompt_id in seen:
            continue
        seen.add(item.prompt_id)
        out.append(item)
    return out
