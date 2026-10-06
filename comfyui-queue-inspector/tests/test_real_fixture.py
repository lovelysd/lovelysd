"""Tests against a /queue payload and /object_info captured from a real ComfyUI.

Fixture origin: ComfyUI master @ 2026-10-05, CPU mode. Jobs were queued via
POST /prompt, three of them with "front": true. The running job uses a
test-only custom node (QITestSleep) that is absent from object_info, which
doubles as the "unknown custom node" case.
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from qi.catalog import NodeCatalog  # noqa: E402
from qi.viewmodel import QueueViewModel  # noqa: E402


def load(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return json.load(f)


QUEUE = load("queue_real_comfyui.json")
INFO = load("object_info_subset.json")


class RealFixtureTests(unittest.TestCase):
    def build(self, catalog):
        return QueueViewModel().build(QUEUE, catalog)

    def test_raw_pending_is_heap_order(self):
        numbers = [x[0] for x in QUEUE["queue_pending"]]
        self.assertNotEqual(numbers, sorted(numbers), "fixture should exercise heap order")

    def test_execution_order(self):
        items = self.build(NodeCatalog(INFO))
        self.assertEqual(items[0]["status"], "running")
        nums = [i["number"] for i in items[1:]]
        self.assertEqual(nums, sorted(nums))
        self.assertLess(nums[0], 0)  # "front" jobs first

    def test_roles_same_with_and_without_type_info(self):
        with_info = self.build(NodeCatalog(INFO))
        without = self.build(NodeCatalog())
        for a, b in zip(with_info, without):
            self.assertEqual(a["prompts"], b["prompts"], a["promptId"])

    def test_extraction(self):
        items = {i["number"]: i for i in self.build(NodeCatalog(INFO))}
        wan = items[3]
        self.assertEqual([m["display"] for m in wan["mainModels"]], ["wan2.1_i2v_480p_14B_fp8"])
        self.assertEqual([e["text"] for e in wan["prompts"]["positive"]], ["a woman walking on the beach"])
        self.assertEqual([e["text"] for e in wan["prompts"]["negative"]], ["static, blurry, worst quality"])
        self.assertEqual([x["filename"] for x in wan["inputImages"]], ["IMG_2034.png"])

        img2img = items[2]
        ref = img2img["inputImages"][0]
        self.assertEqual((ref["subfolder"], ref["filename"], ref["type"]), ("faces", "face_02.png", "input"))

        flux = items[4]
        self.assertEqual(sorted(e["text"] for e in flux["prompts"]["positive"]),
                         ["a neon city at night, rain", "neon city"])

        zero = items[-8]
        self.assertEqual(zero["prompts"]["negative"][0]["field"], "")

        api_job = items[7]
        self.assertIsNone(api_job["workflowId"])  # queued without extra_pnginfo

    def test_unknown_custom_node_does_not_break(self):
        running = self.build(NodeCatalog(INFO))[0]
        self.assertEqual(running["models"], [])
        self.assertEqual(running["prompts"], {"positive": [], "negative": [], "other": []})


if __name__ == "__main__":
    unittest.main()
