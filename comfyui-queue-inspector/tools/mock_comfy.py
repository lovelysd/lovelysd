"""Mock ComfyUI server for developing / demoing the inspector without ComfyUI.

Serves GET /queue, /prompt, /object_info and /view with sample jobs, and
advances the queue every --step seconds (running job finishes, next starts).

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
    return [graphs.wan_i2v(), graphs.img2img(), graphs.sdxl_txt2img(), long_g, graphs.flux_guider(),
            graphs.zero_out_negative(), graphs.shared_encoder_and_custom_text(), missing, multi,
            graphs.string_concat_prompt(), graphs.minimax_ref2va()]


class State:
    def __init__(self, jobs):
        self.lock = threading.Lock()
        self.running = []
        self.pending = []
        gs = sample_graphs()
        for n in range(jobs):
            g = gs[n % len(gs)]
            outputs = [k for k, v in g.items() if v["class_type"] in ("SaveImage", "SaveVideo")]
            self.pending.append([n, str(uuid.uuid4()), g,
                                 {"create_time": int(time.time() * 1000),
                                  "extra_pnginfo": {"workflow": {"id": str(uuid.uuid4())}}}, outputs])
        self.advance()

    def advance(self):
        with self.lock:
            self.running = []
            if self.pending:
                self.pending.sort(key=lambda x: x[0])
                self.running = [self.pending.pop(0)]
                # Return pending in a scrambled heap-like order like the real server.
                self.pending = self.pending[::2] + self.pending[1::2]

    def snapshot(self):
        with self.lock:
            return {"queue_running": list(self.running), "queue_pending": list(self.pending)}


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
