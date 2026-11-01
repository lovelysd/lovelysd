"""Reorder: planning, faithful re-queue, and end-to-end moves against the mock ComfyUI."""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import graphs  # noqa: E402
import mock_comfy  # noqa: E402
from qi.adapter import parse_queue  # noqa: E402
from qi.catalog import NodeCatalog  # noqa: E402
from qi.comfy_client import ComfyClient  # noqa: E402
from qi.http_server import serve  # noqa: E402
from qi.reorder import (  # noqa: E402
    PlanError, Reorderer, merge_draft, plan_moves, requeue_body, rewrap_list_values,
)
from qi.service import QueueService  # noqa: E402


def apply_plan(current, moves):
    """Order that results from applying moves to current [(id, number)]."""
    numbers = dict(current)
    numbers.update(dict(moves))
    return [pid for pid, _ in sorted(numbers.items(), key=lambda kv: (kv[1], kv[0]))]


class PlanTests(unittest.TestCase):
    CUR = [("a", 1.0), ("b", 2.0), ("c", 3.0), ("d", 4.0), ("e", 5.0)]

    def check(self, desired, max_moves, pinned=frozenset()):
        moves = plan_moves(self.CUR, desired, set(pinned))
        self.assertEqual(apply_plan(self.CUR, moves), desired)
        self.assertLessEqual(len(moves), max_moves)
        self.assertTrue(set(pid for pid, _ in moves).isdisjoint(pinned))
        return moves

    def test_minimal_moves(self):
        self.check(["e", "a", "b", "c", "d"], 1)   # last to front: one re-queue
        self.check(["b", "c", "d", "e", "a"], 1)   # first to back
        self.check(["a", "c", "b", "d", "e"], 1)   # swap neighbours
        self.check(["a", "d", "c", "b", "e"], 2)
        self.check(["e", "d", "c", "b", "a"], 4)   # full reverse: n-1
        self.assertEqual(plan_moves(self.CUR, [p for p, _ in self.CUR]), [])

    def test_pinned_never_moves(self):
        moves = self.check(["b", "a", "c", "d", "e"], 1, pinned={"a"})
        self.assertEqual([p for p, _ in moves], ["b"])

    def test_no_number_ties(self):
        cur = [("a", 1.0), ("b", 2.0), ("c", 3.0)]
        moves = plan_moves(cur, ["a", "c", "b"])  # midpoint would tie with c's old number
        taken = {n for _, n in cur}
        for _, n in moves:
            self.assertNotIn(n, taken - {dict(cur)[pid] for pid, _ in moves})

    def test_repeated_moves_stay_ordered(self):
        cur = list(self.CUR)
        for _ in range(80):  # keep inserting into the same slot (gap halves each time)
            desired = [p for p, _ in sorted(cur, key=lambda kv: kv[1])]
            desired.insert(1, desired.pop())
            moves = plan_moves(sorted(cur, key=lambda kv: kv[1]), desired)
            numbers = dict(cur)
            numbers.update(dict(moves))
            cur = list(numbers.items())
            self.assertEqual(apply_plan(cur, []), desired)

    def test_bad_input(self):
        with self.assertRaises(PlanError):
            plan_moves(self.CUR, ["a", "b"])

    def test_merge_draft_keeps_unmentioned_slots(self):
        current = ["n0", "a", "b", "c", "n1"]  # n0/n1 queued while editing
        self.assertEqual(merge_draft(current, ["c", "a", "b", "gone"]), ["n0", "c", "a", "b", "n1"])


class FaithfulRequeueTests(unittest.TestCase):
    def test_rewrap_only_non_links_in_used_nodes(self):
        prompt = {"1": {"class_type": "A", "inputs": {"tags": ["x", "y"], "link": ["2", 0]}},
                  "2": {"class_type": "B", "inputs": {"pair": ["2", 1]}},
                  "3": {"class_type": "C", "inputs": {"tags": ["z"]}}}
        out = rewrap_list_values(prompt, {"1", "2"})
        self.assertEqual(out["1"]["inputs"]["tags"], {"__value__": ["x", "y"]})
        self.assertEqual(out["1"]["inputs"]["link"], ["2", 0])
        self.assertEqual(out["3"]["inputs"]["tags"], ["z"])  # unused: never validated, left as is
        self.assertEqual(prompt["1"]["inputs"]["tags"], ["x", "y"])  # original untouched

    def test_body_carries_everything(self):
        raw = parse_queue({"queue_running": [], "queue_pending": [[
            5, "11111111-1111-4111-8111-111111111111", graphs.sdxl_txt2img(),
            {"client_id": "tab1", "workflow_metadata": {"workflow_id": "w"}, "create_time": 1}, ["9"]]]})[0]
        body = requeue_body(raw, 2.5)
        self.assertEqual(body["prompt_id"], raw.prompt_id)
        self.assertEqual(body["number"], 2.5)
        self.assertEqual(body["partial_execution_targets"], ["9"])
        self.assertEqual(body["workflow_metadata"], {"workflow_id": "w"})
        self.assertEqual(body["client_id"], "tab1")


class ReorderIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.state = mock_comfy.State(14)  # 1 running + 13 pending, one per sample graph
        self.comfy = ThreadingHTTPServer(("127.0.0.1", 0), mock_comfy.make_handler(self.state))
        threading.Thread(target=self.comfy.serve_forever, daemon=True).start()
        self.client = ComfyClient("http://127.0.0.1:%d" % self.comfy.server_address[1])
        self.backups = tempfile.mkdtemp()
        self.logs = []
        self.reorderer = Reorderer(self.client, lambda: NodeCatalog(graphs.OBJECT_INFO), self.backups,
                                   log=self.logs.append)

    def tearDown(self):
        self.comfy.shutdown()
        self.comfy.server_close()
        shutil.rmtree(self.backups, ignore_errors=True)

    def queue(self):
        items = parse_queue(self.client.get_queue())
        return [i for i in items if i.status == "pending"], [i for i in items if i.status == "running"]

    def test_move_is_byte_identical(self):
        pending, _ = self.queue()
        self.assertEqual(len(pending), 13)
        before = {p.prompt_id: p for p in pending}
        order = [p.prompt_id for p in pending]
        desired = order[::-1]  # includes the list-widget job and the Ref2VA job
        api = [p.prompt_id for p in pending if p.prompt.get("1", {}).get("class_type") == "CloudVideoApiNode"]
        # The API-node job cannot move; keep it where it is in the draft.
        desired.remove(api[0])
        desired.insert(order.index(api[0]), api[0])
        res = self.reorderer.apply(desired)
        self.assertTrue(res["ok"], res)
        moved = [r for r in res["results"] if r["status"] == "moved"]
        self.assertEqual(len(moved), len(res["results"]))
        self.assertTrue(all(not r["diffs"] for r in moved), res)
        after, _ = self.queue()
        self.assertEqual([p.prompt_id for p in after], desired)
        for p in after:  # everything except number/create_time is identical
            b = before[p.prompt_id]
            self.assertEqual(json.dumps(p.prompt, sort_keys=True), json.dumps(b.prompt, sort_keys=True))
            self.assertEqual(sorted(p.outputs), sorted(b.outputs))
            ea = {k: v for k, v in p.extra_data.items() if k != "create_time"}
            eb = {k: v for k, v in b.extra_data.items() if k != "create_time"}
            self.assertEqual(ea, eb)
        self.assertEqual(len(os.listdir(self.backups)), len(moved))

    def test_api_job_is_never_moved(self):
        pending, _ = self.queue()
        order = [p.prompt_id for p in pending]
        api = next(p.prompt_id for p in pending if p.prompt.get("1", {}).get("class_type") == "CloudVideoApiNode")
        desired = [api] + [x for x in order if x != api]
        res = self.reorderer.apply(desired)
        self.assertTrue(res["ok"], res)
        self.assertNotIn(api, [r["promptId"] for r in res["results"]])

    def test_rejected_requeue_is_restored(self):
        pending, _ = self.queue()
        order = [p.prompt_id for p in pending]
        desired = [order[-1]] + order[:-1]
        self.state.reject_next = 1  # the re-queue at the new position is rejected
        res = self.reorderer.apply(desired)
        self.assertEqual(res["results"][0]["status"], "restored")
        after, _ = self.queue()
        self.assertEqual([p.prompt_id for p in after], order)  # back where it was

    def test_job_that_started_running_is_not_requeued(self):
        pending, _ = self.queue()
        order = [p.prompt_id for p in pending]
        first = order[0]
        real_post = self.client.post_json

        def post_and_start(path, data, timeout=None):
            if path == "/queue":  # the job starts running just before the delete arrives
                self.state.advance()
            return real_post(path, data, timeout)
        self.client.post_json = post_and_start
        res = self.reorderer.apply(order[1:] + [first])
        self.assertEqual(res["results"][0]["status"], "skipped")
        self.assertIn("running", res["results"][0]["reason"])
        _, running = self.queue()
        self.assertEqual([r.prompt_id for r in running], [first])
        pending_after, _ = self.queue()
        self.assertNotIn(first, [p.prompt_id for p in pending_after])  # not duplicated

    def test_unloaded_type_info_refuses(self):
        r = Reorderer(self.client, lambda: NodeCatalog(), self.backups)
        pending, _ = self.queue()
        res = r.apply([p.prompt_id for p in pending][::-1])
        self.assertFalse(res["ok"])


class ReorderHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = mock_comfy.State(5)
        cls.comfy = ThreadingHTTPServer(("127.0.0.1", 0), mock_comfy.make_handler(cls.state))
        threading.Thread(target=cls.comfy.serve_forever, daemon=True).start()
        client = ComfyClient("http://127.0.0.1:%d" % cls.comfy.server_address[1])
        cls.backups = tempfile.mkdtemp()
        cls.service = QueueService(client)
        cls.service._load_object_info()
        cls.service.reorderer = Reorderer(client, lambda: cls.service.catalog, cls.backups, log=lambda *_: None)
        cls.httpd = serve(cls.service, "127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for s in (cls.httpd, cls.comfy):
            s.shutdown()
            s.server_close()
        shutil.rmtree(cls.backups, ignore_errors=True)

    def post(self, body, headers):
        req = urllib.request.Request(self.base + "/api/reorder", data=json.dumps(body).encode(),
                                     method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_csrf_guards(self):
        good = {"Content-Type": "application/json", "X-Queue-Inspector": "1"}
        self.assertEqual(self.post({"order": []}, {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.post({"order": []}, {"X-Queue-Inspector": "1", "Content-Type": "text/plain"})[0], 403)
        self.assertEqual(self.post({"order": []}, dict(good, Origin="http://evil.example"))[0], 403)
        status, body = self.post({"order": []}, good)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_snapshot_flags(self):
        with urllib.request.urlopen(self.base + "/api/queue?force=1") as r:
            snap = json.loads(r.read())
        self.assertTrue(snap["reorderEnabled"])

    def test_disabled_by_default(self):
        svc = QueueService(ComfyClient("http://127.0.0.1:1"))
        httpd = serve(svc, "127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request("http://127.0.0.1:%d/api/reorder" % httpd.server_address[1],
                                         data=b'{"order": []}', method="POST",
                                         headers={"Content-Type": "application/json", "X-Queue-Inspector": "1"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req)
            self.assertEqual(cm.exception.code, 405)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
