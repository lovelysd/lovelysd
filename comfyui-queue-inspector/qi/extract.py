"""Extract prompts, models and input images from an API-format prompt graph.

API-format graph (what the server executes):
    {"<node_id>": {"class_type": "...", "inputs": {...}, "_meta": {"title": "..."}}}
An input value is either a literal or a link [source_node_id, output_index].
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .catalog import NodeCatalog

# ---------------------------------------------------------------- helpers


def is_link(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and isinstance(value[0], (str, int))
        and not isinstance(value[0], bool)
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    )


def _node_sort_key(node_id: str):
    try:
        return (0, float(node_id), node_id)
    except ValueError:
        return (1, 0.0, node_id)


def _nodes(prompt: dict) -> Dict[str, dict]:
    return {str(k): v for k, v in prompt.items() if isinstance(v, dict)}


def _class(node: dict) -> str:
    c = node.get("class_type")
    return c if isinstance(c, str) else ""


def _inputs(node: dict) -> dict:
    i = node.get("inputs")
    return i if isinstance(i, dict) else {}


def _title(node: dict) -> str:
    meta = node.get("_meta")
    if isinstance(meta, dict) and isinstance(meta.get("title"), str):
        return meta["title"]
    return _class(node)


# ---------------------------------------------------------------- prompts

# Field names that hold prompt text on common nodes. Used when /object_info
# is unavailable, and in addition to it for known names.
TEXT_FIELD_NAMES = {
    "text", "text_g", "text_l", "clip_l", "t5xxl", "prompt",
    "positive_prompt", "negative_prompt", "string", "string_a", "string_b",
}

# Conditioning inputs that only *consume* conditioning (samplers/guiders).
# Pass-through nodes with "positive"/"negative" inputs are handled the same
# way because tracing preserves the role by port name.
ROLE_INPUTS = {"positive": "positive", "negative": "negative"}

# Guider-style single-conditioning inputs that mean "positive".
POSITIVE_ONLY_INPUTS = {
    "BasicGuider": ("conditioning",),
    "DualCFGGuider": ("cond1", "cond2"),
}

# Nodes that replace the conditioning with zeros: the upstream text is not
# used for this role, so tracing stops and a note is recorded.
ZERO_OUT_CLASSES = {"ConditioningZeroOut"}

_COND_NAME_RE = re.compile(r"cond|positive|negative|^pos$|^neg$", re.IGNORECASE)


def _is_text_field(catalog: NodeCatalog, cls: str, name: str) -> bool:
    if name in TEXT_FIELD_NAMES:
        return True
    return bool(catalog.is_multiline_string(cls, name))


def _is_string_link_input(catalog: NodeCatalog, cls: str, name: str) -> bool:
    t = catalog.input_type(cls, name)
    if t is not None:
        return t == "STRING"
    return name in TEXT_FIELD_NAMES or name.startswith("string")


class _PromptTracer:
    def __init__(self, nodes: Dict[str, dict], catalog: NodeCatalog):
        self.nodes = nodes
        self.catalog = catalog
        self.roles: Dict[Tuple[str, str], Set[str]] = {}
        self.texts: Dict[Tuple[str, str], str] = {}
        self.notes: Dict[str, List[dict]] = {"positive": [], "negative": []}
        self._visited: Set[Tuple[str, Any, str]] = set()

    def _record(self, node_id: str, field: str, text: str, role: str):
        key = (node_id, field)
        self.texts[key] = text
        self.roles.setdefault(key, set()).add(role)

    def _collect_text(self, node_id: str, role: str):
        """Record literal text fields of a node, following STRING links upstream."""
        vkey = (node_id, "text", role)
        if vkey in self._visited:
            return
        self._visited.add(vkey)
        node = self.nodes.get(node_id)
        if node is None:
            return
        cls = _class(node)
        for name, value in _inputs(node).items():
            if isinstance(value, str):
                if _is_text_field(self.catalog, cls, name):
                    self._record(node_id, name, value, role)
            elif is_link(value) and _is_string_link_input(self.catalog, cls, name):
                self._collect_text(str(value[0]), role)

    def trace(self, link: Any, role: str):
        if not is_link(link):
            return
        node_id, out_idx = str(link[0]), link[1]
        vkey = (node_id, out_idx, role)
        if vkey in self._visited:
            return
        self._visited.add(vkey)

        node = self.nodes.get(node_id)
        if node is None:
            return
        cls = _class(node)
        inputs = _inputs(node)

        if cls in ZERO_OUT_CLASSES:
            self.notes[role].append({
                "nodeId": node_id, "title": _title(node), "classType": cls,
                "field": "", "text": "", "note": "Conditioning zeroed out (no text used)",
            })
            return

        # Text literals on this node (e.g. CLIPTextEncode.text).
        self._collect_text(node_id, role)

        # Decide which upstream conditioning edges carry this role.
        out_names = self.catalog.output_names(cls)
        out_name = None
        if out_names is not None and 0 <= out_idx < len(out_names):
            out_name = out_names[out_idx].lower()

        has_pos = is_link(inputs.get("positive"))
        has_neg = is_link(inputs.get("negative"))

        # Pass-through node with separate positive/negative ports: follow only
        # the port that matches the output we came from.
        if out_name in ("positive", "negative") and is_link(inputs.get(out_name)):
            self.trace(inputs[out_name], role)
            return
        if out_name is None and has_pos and has_neg and out_idx in (0, 1):
            # Unknown node: by convention output 0 = positive, 1 = negative.
            self.trace(inputs["positive" if out_idx == 0 else "negative"], role)
            return

        for name, value in inputs.items():
            if not is_link(value):
                continue
            t = self.catalog.input_type(cls, name)
            if t is not None:
                follow = t == "CONDITIONING"
            else:
                follow = bool(_COND_NAME_RE.search(name))
            if follow:
                self.trace(value, role)


def _entry(nodes: Dict[str, dict], node_id: str, field: str, text: str, note: str = "") -> dict:
    node = nodes.get(node_id, {})
    return {
        "nodeId": node_id,
        "title": _title(node),
        "classType": _class(node),
        "field": field,
        "text": text,
        "note": note,
    }


def extract_prompts(prompt: dict, catalog: Optional[NodeCatalog] = None) -> dict:
    catalog = catalog or NodeCatalog()
    nodes = _nodes(prompt)
    tracer = _PromptTracer(nodes, catalog)

    for node_id in sorted(nodes, key=_node_sort_key):
        node = nodes[node_id]
        cls = _class(node)
        inputs = _inputs(node)
        for name, role in ROLE_INPUTS.items():
            if is_link(inputs.get(name)):
                tracer.trace(inputs[name], role)
        for name in POSITIVE_ONLY_INPUTS.get(cls, ()):
            if is_link(inputs.get(name)):
                tracer.trace(inputs[name], "positive")

    positive: List[dict] = []
    negative: List[dict] = []
    other: List[dict] = []

    def key_order(k: Tuple[str, str]):
        return (_node_sort_key(k[0]), k[1])

    for key in sorted(tracer.roles, key=key_order):
        roles = tracer.roles[key]
        text = tracer.texts[key]
        if roles == {"positive"}:
            positive.append(_entry(nodes, key[0], key[1], text))
        elif roles == {"negative"}:
            negative.append(_entry(nodes, key[0], key[1], text))
        else:
            other.append(_entry(nodes, key[0], key[1], text, "Used as both positive and negative"))

    # Remaining prompt-like text that no sampler path reached.
    for node_id in sorted(nodes, key=_node_sort_key):
        node = nodes[node_id]
        cls = _class(node)
        for name, value in _inputs(node).items():
            if (node_id, name) in tracer.roles:
                continue
            if isinstance(value, str) and value.strip() and _is_text_field(catalog, cls, name):
                other.append(_entry(nodes, node_id, name, value))

    # Zero-out notes only matter when nothing else was found for that role.
    if not positive:
        positive.extend(tracer.notes["positive"])
    if not negative:
        negative.extend(tracer.notes["negative"])

    return {"positive": positive, "negative": negative, "other": other}


# ---------------------------------------------------------------- models

MODEL_EXTENSIONS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft", ".onnx")

KIND_LABELS = {
    "checkpoint": "Checkpoint",
    "diffusion_model": "Diffusion model",
    "lora": "LoRA",
    "text_encoder": "Text encoder",
    "clip_vision": "CLIP vision",
    "vae": "VAE",
    "controlnet": "ControlNet",
    "upscale": "Upscale model",
    "style_model": "Style model",
    "other": "Other model",
}
MAIN_KINDS = ("checkpoint", "diffusion_model")
KIND_ORDER = list(KIND_LABELS)

_CLIP_NAME_RE = re.compile(r"^clip_name\d*$")


def _model_kind(cls: str, field: str, value: Any) -> Optional[str]:
    if field == "ckpt_name":
        return "checkpoint"
    if field == "unet_name":
        return "diffusion_model"
    if field == "lora_name":
        return "lora"
    if _CLIP_NAME_RE.match(field):
        return "clip_vision" if "vision" in cls.lower() else "text_encoder"
    if field == "vae_name":
        return "vae"
    if field == "control_net_name":
        return "controlnet"
    if field == "style_model_name":
        return "style_model"
    if field == "model_name" and "upscale" in cls.lower():
        return "upscale"
    if isinstance(value, str) and value.lower().endswith(MODEL_EXTENSIONS):
        return "other"
    return None


def display_model_name(value: str) -> str:
    base = re.split(r"[\\/]", value)[-1]
    for ext in MODEL_EXTENSIONS:
        if base.lower().endswith(ext):
            return base[: -len(ext)]
    return base


def _strength(inputs: dict) -> Optional[str]:
    parts = []
    for key, label in (("strength_model", "model"), ("strength_clip", "clip"), ("strength", "")):
        if key not in inputs:
            continue
        v = inputs[key]
        if is_link(v):
            text = "linked"
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            text = ("%g" % v)
        else:
            continue
        parts.append(f"{label} {text}".strip())
    return ", ".join(parts) if parts else None


def extract_models(prompt: dict) -> List[dict]:
    nodes = _nodes(prompt)
    out: List[dict] = []
    for node_id in sorted(nodes, key=_node_sort_key):
        node = nodes[node_id]
        cls = _class(node)
        if "loadimage" in cls.lower():
            continue
        inputs = _inputs(node)
        for field, value in inputs.items():
            kind = _model_kind(cls, field, value)
            if kind is None:
                continue
            if is_link(value):
                name, display = None, "(linked)"
            elif isinstance(value, str) and value:
                name, display = value, display_model_name(value)
            else:
                continue
            entry = {
                "kind": kind,
                "kindLabel": KIND_LABELS[kind],
                "name": name,
                "display": display,
                "nodeId": node_id,
                "classType": cls,
                "title": _title(node),
                "field": field,
            }
            if kind == "lora":
                entry["strength"] = _strength(inputs)
            out.append(entry)
    out.sort(key=lambda m: KIND_ORDER.index(m["kind"]))  # stable: keeps node order inside a kind
    return out


def main_models(models: List[dict]) -> List[dict]:
    return [m for m in models if m["kind"] in MAIN_KINDS]


# ---------------------------------------------------------------- input images

IMAGE_LOADER_CLASSES = {"LoadImage", "LoadImageMask", "LoadImageOutput"}
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")
_ANNOTATION_RE = re.compile(r"\s*\[(input|output|temp)\]\s*$")


def parse_image_ref(value: str, default_type: str = "input") -> dict:
    """Split a LoadImage value into the parts `/view` expects.

    Values look like "photo.png", "sub/dir/photo.png [input]" or, with the
    asset system enabled, "blake3:<hash>" (resolved by /view itself).
    """
    if value.startswith("blake3:"):
        return {
            "raw": value, "filename": value, "subfolder": "", "type": default_type,
            "display": "asset " + value[len("blake3:"):][:10] + "…", "isAsset": True,
        }
    name = value
    img_type = default_type
    m = _ANNOTATION_RE.search(name)
    if m:
        img_type = m.group(1)
        name = name[: m.start()]
    name = name.replace("\\", "/")
    if "/" in name:
        subfolder, filename = name.rsplit("/", 1)
    else:
        subfolder, filename = "", name
    return {
        "raw": value, "filename": filename, "subfolder": subfolder, "type": img_type,
        "display": filename, "isAsset": False,
    }


def extract_input_images(prompt: dict) -> List[dict]:
    nodes = _nodes(prompt)
    out: List[dict] = []
    seen = set()
    for node_id in sorted(nodes, key=_node_sort_key):
        node = nodes[node_id]
        cls = _class(node)
        known = cls in IMAGE_LOADER_CLASSES
        if not known and "loadimage" not in cls.lower():
            continue
        value = _inputs(node).get("image")
        if not isinstance(value, str) or not value:
            continue
        if not known and not (value.startswith("blake3:") or
                              _ANNOTATION_RE.sub("", value).lower().endswith(IMAGE_EXTENSIONS)):
            continue
        if value in seen:
            continue
        seen.add(value)
        ref = parse_image_ref(value, "output" if cls == "LoadImageOutput" else "input")
        ref["nodeId"] = node_id
        ref["title"] = _title(node)
        out.append(ref)
    return out
