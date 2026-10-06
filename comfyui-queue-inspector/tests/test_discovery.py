"""ComfyUI address auto-detection (portable :8188 vs. Desktop :8000)."""

import os
import socket
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import inspector  # noqa: E402
import mock_comfy  # noqa: E402
from qi.comfy_client import ComfyClient  # noqa: E402
from qi.service import QueueService  # noqa: E402


def closed_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here afterwards
    return "http://127.0.0.1:%d" % port


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        state = mock_comfy.State(3)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), mock_comfy.make_handler(state))
        cls.live = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_pick_first_answering(self):
        dead = closed_port_url()
        self.assertEqual(inspector.pick_comfy_url([dead, self.live]), self.live)
        self.assertEqual(inspector.pick_comfy_url([dead]), dead)

    def test_switches_when_current_is_down(self):
        dead = closed_port_url()
        service = QueueService(ComfyClient(dead), candidates=[dead, self.live])
        first = service.snapshot()
        self.assertFalse(first["ok"])
        self.assertEqual(first["comfyUrl"], self.live)  # switched for the next poll
        second = service.snapshot()
        self.assertTrue(second["ok"])
        self.assertEqual(second["running"] + second["pending"], 3)

    def test_fixed_address_does_not_switch(self):
        dead = closed_port_url()
        service = QueueService(ComfyClient(dead), candidates=[dead])
        service.snapshot()
        self.assertEqual(service.client.base_url, dead)


if __name__ == "__main__":
    unittest.main()
