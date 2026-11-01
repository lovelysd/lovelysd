"""Mock ComfyUI server for developing / demoing the inspector without ComfyUI.

Serves GET /queue, /prompt, /object_info, /view, /history/<id> with sample
jobs, and advances the queue every --step seconds (running job finishes,
next starts). POST /prompt and POST /queue {"delete": [...]} mimic the parts
of ComfyUI that matter for re-queuing: validation unwraps {"__value__": ...}
widget values in place and rejects bare non-link lists in the nodes feeding
the outputs, "workflow_metadata" is rebuilt, and "create_time" is stamped.

    python tools/mock_comfy.py --port 8188 [--jobs 30] [--step 8]
    python inspector.py --comfy http://127.0.0.1:8188
"""

import argparse
import json
import os
import struct
import sys
import threading
import time
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests"))

import graphs  # noqa: E402

LONG_PROMPT = (
    "A cinematic wide shot of an old lighthouse on a rocky cliff at dusk, waves crashing below, "
    "volumetric light beams cutting through sea mist, dramatic clouds, film grain, 35mm, "
    "highly detailed textures on weathered stone, warm window glow, cold blue ambient light, "
) * 6


def _png(w, h, seed):
    rows = []
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            row += bytes(((x * 255 // w + seed * 40) % 256, (y * 255 // h) % 256, (seed * 70 + 90) % 256))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 6)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", raw) + chunk(b"IEND", b"")


IMAGES = {("", "IMG_2034.png"): _png(640, 400, 1), ("faces", "face_02.png"): _png(400, 520, 2),
          ("deno", "ref_face.png"): _png(480, 480, 3), ("", "hero.jpg"): _png(600, 400, 4)}


def sample_graphs():
    long_g = graphs.sdxl_txt2img()
    long_g["6"]["inputs"]["text"] = LONG_PROMPT
    missing = graphs.img2img()
    missing["11"]["inputs"]["image"] = "deleted_photo.png"
    multi = graphs.wan_i2v()
    multi["53"] = graphs.node("LoadImage", image="faces/face_02.png [input]")
    listed = graphs.sdxl_txt2img()
    listed["9"]["inputs"]["tags"] = {"__value__": ["portrait", "cinematic"]}  # list widget value
    return [graphs.wan_i2v(), graphs.img2img(), graphs.sdxl_txt2img(), long_g, graphs.flux_guider(),
            graphs.zero_out_negative(), graphs.shared_encoder_and_custom_text(), missing, multi,
            graphs.string_concat_prompt(), graphs.minimax_ref2va(), listed, graphs.api_node_job()]


OUTPUT_CLASSES = ("SaveImage", "SaveVideo")


def _with_output(g):
    """Real ComfyUI rejects graphs without an output node or with links to
    missing nodes; complete the test graphs so they validate."""
    for v in list(g.values()):
        for x in v.get("inputs", {}).values():
            if isinstance(x, list) and len(x) == 2 and isinstance(x[1], int) and str(x[0]) not in g:
                g[str(x[0])] = graphs.node("MockSource")
    if not any(v["class_type"] in OUTPUT_CLASSES for v in g.values()):
        src = next((k for k, v in g.items() if v["class_type"] in ("KSampler", "SamplerCustomAdvanced")),
                   next(iter(g)))
        g["99"] = graphs.node("SaveImage", images=[src, 0], filename_prefix="mock")
    return g


def _ancestors(prompt, outputs):
    seen, stack = set(), list(outputs)
    while stack:
        nid = stack.pop()
        if nid in seen or nid not in prompt:
            continue
        seen.add(nid)
        for v in prompt[nid].get("inputs", {}).values():
            if isinstance(v, list) and len(v) == 2 and str(v[0]) in prompt:
                stack.append(str(v[0]))
    return seen


class State:
    def __init__(self, jobs):
        self.lock = threading.Lock()
        self.running = []
        self.pending = []
        self.history = set()
        self.counter = 0
        self.reject_next = 0  # tests: reject this many next POST /prompt
        gs = sample_graphs()
        for n in range(jobs):
            g = _with_output(json.loads(json.dumps(gs[n % len(gs)])))
            status, res = self.submit({"prompt": g, "client_id": "mock-client",
                                       "extra_data": {"extra_pnginfo": {"workflow": {"id": str(uuid.uuid4())}}}})
            if status != 200:
                raise RuntimeError("mock sample job %d rejected: %s" % (n, res))
        self.advance()

    def submit(self, body):
        """Mimic POST /prompt. Returns (status, response)."""
        with self.lock:
            if self.reject_next:
                self.reject_next -= 1
                return 400, {"error": {"type": "mock_reject", "message": "rejected by test"}, "node_errors": {}}
            prompt = body.get("prompt")
            if not isinstance(prompt, dict):
                return 400, {"error": {"type": "no_prompt", "message": "No prompt provided"}}
            partial = body.get("partial_execution_targets")
            outputs = [k for k, v in prompt.items() if v.get("class_type") in OUTPUT_CLASSES
                       and (partial is None or k in partial)]
            if not outputs:
                return 400, {"error": {"type": "prompt_no_outputs", "message": "Prompt has no outputs"}}
            for nid in _ancestors(prompt, outputs):  # validation edits the prompt in place
                inputs = prompt[nid].get("inputs", {})
                for name, v in list(inputs.items()):
                    if isinstance(v, dict) and "__value__" in v:
                        inputs[name] = v["__value__"]
                    elif isinstance(v, list) and not (len(v) == 2 and str(v[0]) in prompt and isinstance(v[1], int)):
                        return 400, {"error": {"type": "bad_linked_input",
                                               "message": "Bad linked input, must be a length-2 list"}}
            if "number" in body:
                number = float(body["number"])
            else:
                number = self.counter
                if body.get("front"):
                    number = -number
                self.counter += 1
            pid = body.get("prompt_id") or str(uuid.uuid4())
            extra = body.get("extra_data") or {}
            if "client_id" in body:
                extra["client_id"] = body["client_id"]
            extra.pop("workflow_metadata", None)
            md = body.get("workflow_metadata")
            if not isinstance(md, dict):
                wid = extra.get("extra_pnginfo", {}).get("workflow", {}).get("id")
                md = {"workflow_id": wid} if wid else None
            if md is not None:
                extra["workflow_metadata"] = md
            extra["create_time"] = int(time.time() * 1000) + self.counter  # differs per submit
            self.counter += 1
            self.pending.append([number, pid, prompt, extra, sorted(outputs)])
            return 200, {"prompt_id": pid, "number": number, "node_errors": {}}

    def delete(self, ids):
        with self.lock:
            self.pending = [p for p in self.pending if p[1] not in ids]

    def advance(self):
        with self.lock:
            if self.running:
                self.history.add(self.running[0][1])
            self.running = []
            if self.pending:
                self.pending.sort(key=lambda x: (x[0], x[1]))
                self.running = [self.pending.pop(0)]
                # Return pending in a scrambled heap-like order like the real server.
                self.pending = self.pending[::2] + self.pending[1::2]

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps({"queue_running": self.running, "queue_pending": self.pending}))


def make_handler(state):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, data, status=200):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlsplit(self.path)
            q = parse_qs(u.query)
            if u.path == "/queue":
                return self._json(state.snapshot())
            if u.path == "/prompt":
                s = state.snapshot()
                return self._json({"exec_info": {"queue_remaining": len(s["queue_running"]) + len(s["queue_pending"])}})
            if u.path.startswith("/history/"):
                pid = u.path[len("/history/"):]
                return self._json({pid: {}} if pid in state.history else {})
            if u.path == "/object_info":
                return self._json(graphs.OBJECT_INFO)
            if u.path == "/view":
                key = (q.get("subfolder", [""])[0], q.get("filename", [""])[0])
                data = IMAGES.get(key)
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            u = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if u.path == "/prompt":
                status, data = state.submit(body)
                return self._json(data, status)
            if u.path == "/queue":
                if body.get("delete"):
                    state.delete(set(body["delete"]))
                return self._json({})
            self.send_response(404)
            self.end_headers()
    return H


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8188)
    p.add_argument("--jobs", type=int, default=30)
    p.add_argument("--step", type=float, default=8.0, help="seconds per job; 0 = never advance")
    a = p.parse_args()
    state = State(a.jobs)

    if a.step > 0:
        def loop():
            while True:
                time.sleep(a.step)
                state.advance()
        threading.Thread(target=loop, daemon=True).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(state))
    print(f"Mock ComfyUI on http://127.0.0.1:{a.port}  ({a.jobs} jobs, step {a.step}s)")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
