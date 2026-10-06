"""Optional node-type knowledge from ComfyUI's `GET /object_info`.

`/object_info` describes every installed node class: input names and types,
output types and output names. With it, prompt tracing can follow exactly the
CONDITIONING edges and pick the right port on pass-through nodes such as
ControlNetApplyAdvanced or WanImageToVideo (outputs "positive"/"negative").

Every method returns None when the catalog does not know the answer, so the
callers can fall back to name-based heuristics.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


class NodeCatalog:
    def __init__(self, object_info: Optional[Dict[str, Any]] = None):
        self._info = object_info if isinstance(object_info, dict) else {}

    @property
    def available(self) -> bool:
        return bool(self._info)

    def class_names(self) -> set:
        return set(self._info)

    def knows(self, class_type: str) -> bool:
        return isinstance(self._info.get(class_type), dict)

    def output_names(self, class_type: str) -> Optional[List[str]]:
        info = self._info.get(class_type)
        if not isinstance(info, dict):
            return None
        names = info.get("output_name") or info.get("output")
        if isinstance(names, (list, tuple)):
            return [str(n) for n in names]
        return None

    def _input_spec(self, class_type: str, name: str) -> Optional[Tuple[Any, dict]]:
        info = self._info.get(class_type)
        if not isinstance(info, dict):
            return None
        inputs = info.get("input")
        if not isinstance(inputs, dict):
            return None
        for section in ("required", "optional", "hidden"):
            group = inputs.get(section)
            if isinstance(group, dict) and name in group:
                spec = group[name]
                if isinstance(spec, (list, tuple)) and spec:
                    opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
                    return spec[0], opts
                return None
        return None

    def input_type(self, class_type: str, name: str) -> Optional[str]:
        spec = self._input_spec(class_type, name)
        if spec is None:
            return None
        t = spec[0]
        if isinstance(t, (list, tuple)):
            return "COMBO"
        return str(t)

    def is_multiline_string(self, class_type: str, name: str) -> Optional[bool]:
        spec = self._input_spec(class_type, name)
        if spec is None:
            return None
        t, opts = spec
        return t == "STRING" and bool(opts.get("multiline"))
