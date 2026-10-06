"""HTTP-level tests with a fake ComfyUI client (no network to ComfyUI needed)."""

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import graphs  # noqa: E402
from qi.comfy_client import ComfyUnavailable  # noqa: E402
from qi.http_server import serve  # noqa: E402
from qi.service import QueueService  # noqa: E402


class FakeClient:
    base_url = "http://fake"

    def __init__(self):
        self.up = True
        self.queue_calls = 0
        self.payload = {"queue_running": [], "queue_pending": [
            [2, "b", graphs.img2img(), {}, []], [1, "a", graphs.sdxl_txt2img(), {}, []]]}

    def _check(self):
        if not self.up:
            raise ComfyUnavailable("down")

    def get_queue_remaining(self):
        self._check()
        return len(self.payload["queue_running"]) + len(self.payload["queue_pending"])

    def get_queue(self):
        self._check()
        self.queue_calls += 1
        return self.payload

    def get_object_info(self):
        self._check()
        return graphs.OBJECT_INFO

    def get_image(self, filename, subfolder, img_type):
        self._check()
        if filename == "face_02.png" and subfolder == "faces":
            return 200, "image/png", b"\x89PNG fake"
        return 404, "application/json", b""


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = FakeClient()
        cls.service = QueueService(cls.client)
        cls.service._load_object_info()  # synchronously, so no background reload races the tests
        cls.httpd = serve(cls.service, "127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path, method="GET"):
        req = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()

    def test_queue_and_unchanged(self):
        status, _, body = self.get("/api/queue?force=1")
        snap = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(snap["ok"])
        self.assertEqual([i["promptId"] for i in snap["items"]], ["a", "b"])
        status, _, body = self.get("/api/queue?v=%d" % snap["version"])
        slim = json.loads(body)
        self.assertTrue(slim.get("unchanged"))
        self.assertNotIn("items", slim)

    def test_cheap_poll_skips_full_fetch(self):
        self.get("/api/queue?force=1")
        before = self.client.queue_calls
        self.get("/api/queue")
        self.assertEqual(self.client.queue_calls, before)  # count unchanged, within 5 s

    def test_comfy_down(self):
        self.client.up = False
        try:
            snap = json.loads(self.get("/api/queue")[2])
            self.assertFalse(snap["ok"])
            self.assertEqual(snap["error"], "Unable to read queue.")
            self.assertEqual(snap["items"], [])
        finally:
            self.client.up = True

    def test_image_proxy(self):
        status, ctype, body = self.get("/api/image?filename=face_02.png&subfolder=faces&type=input")
        self.assertEqual((status, ctype), (200, "image/png"))
        self.assertEqual(self.get("/api/image?filename=gone.png&type=input")[0], 404)
        self.assertEqual(self.get("/api/image?filename=x.png&type=models")[0], 400)

    def test_read_only_and_static(self):
        for method in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.get("/api/queue", method)[0], 405)
        self.assertEqual(self.get("/")[0], 200)
        self.assertEqual(self.get("/static/app.js")[0], 200)
        self.assertEqual(self.get("/static/../qi/service.py")[0], 404)
        self.assertEqual(self.get("/static/%2e%2e/inspector.py")[0], 404)


if __name__ == "__main__":
    unittest.main()
