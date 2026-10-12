"""Normalized queue item model consumed by the web UI.

The UI never sees raw ComfyUI data; it sees only what `QueueViewModel.build` returns.
Parsed results are cached per prompt_id because a queued prompt graph is
immutable once queued - only its status/position changes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from .adapter import RawQueueItem, parse_queue
from .catalog import NodeCatalog
from .extract import extract_input_images, extract_models, extract_prompts, main_models, used_nodes


def _workflow_id(extra_data: dict) -> Optional[str]:
    pnginfo = extra_data.get("extra_pnginfo")
    if isinstance(pnginfo, dict):
        wf = pnginfo.get("workflow")
        if isinstance(wf, dict) and isinstance(wf.get("id"), str):
            return wf["id"]
    return None


def build_details(raw: RawQueueItem, catalog: NodeCatalog) -> Dict[str, Any]:
    """Expensive, status-independent part of an item (cached by prompt_id)."""
    warnings = list(raw.warnings)
    used = used_nodes(raw.prompt, raw.outputs)  # ignore nodes that feed no output
    try:
        prompts = extract_prompts(raw.prompt, catalog, used)
    except Exception as exc:  # never let one odd graph break the table
        prompts = {"positive": [], "negative": [], "other": []}
        warnings.append(f"prompt extraction failed: {exc}")
    try:
        models = extract_models(raw.prompt, used)
    except Exception as exc:
        models = []
        warnings.append(f"model extraction failed: {exc}")
    try:
        images = extract_input_images(raw.prompt, used)
    except Exception as exc:
        images = []
        warnings.append(f"image extraction failed: {exc}")

    create_time = raw.extra_data.get("create_time")
    return {
        "promptId": raw.prompt_id,
        "number": raw.number,
        "createTime": create_time if isinstance(create_time, (int, float)) else None,
        "workflowId": _workflow_id(raw.extra_data),
        "nodeCount": len(raw.prompt),
        "unusedNodeCount": len(raw.prompt) - len(used) if used is not None else 0,
        "mainModels": main_models(models),
        "models": models,
        "inputImages": images,
        "prompts": prompts,
        "warnings": warnings,
    }


class QueueViewModel:
    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}

    def clear_cache(self):
        self._cache.clear()

    def build(self, payload: Any, catalog: NodeCatalog) -> List[Dict[str, Any]]:
        raw_items = parse_queue(payload)
        items: List[Dict[str, Any]] = []
        alive = set()
        for order, raw in enumerate(raw_items, start=1):
            alive.add(raw.prompt_id)
            details = self._cache.get(raw.prompt_id)
            if details is None:
                details = build_details(raw, catalog)
                self._cache[raw.prompt_id] = details
            item = dict(details)
            item["status"] = raw.status
            item["order"] = order
            item["number"] = raw.number
            items.append(item)
        # Drop finished jobs from the cache.
        for pid in list(self._cache):
            if pid not in alive:
                del self._cache[pid]
        return items

    def unknown_classes(self, payload: Any, catalog: NodeCatalog) -> Set[str]:
        """Node classes used by not-yet-parsed jobs that the catalog does not know."""
        found: Set[str] = set()
        if not catalog.available:
            return found
        try:
            raw_items = parse_queue(payload)
        except Exception:
            return found
        for raw in raw_items:
            if raw.prompt_id in self._cache:
                continue
            for node in raw.prompt.values():
                if isinstance(node, dict):
                    cls = str(node.get("class_type", ""))
                    if cls and not catalog.knows(cls):
                        found.add(cls)
        return found
