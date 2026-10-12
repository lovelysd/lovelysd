import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import graphs  # noqa: E402
from qi.adapter import QueueFormatError, parse_queue  # noqa: E402
from qi.catalog import NodeCatalog  # noqa: E402
from qi.extract import (  # noqa: E402
    extract_input_images, extract_models, extract_prompts, main_models, parse_image_ref, used_nodes,
)
from qi.viewmodel import QueueViewModel  # noqa: E402

CATALOG = NodeCatalog(graphs.OBJECT_INFO)
NO_CATALOG = NodeCatalog()


def texts(entries):
    return [e["text"] for e in entries]


def item(number, pid, prompt=None, extra=None):
    return [number, pid, prompt or {}, extra or {}, ["9"]]


class AdapterTests(unittest.TestCase):
    def test_pending_sorted_by_number_not_heap_order(self):
        # A valid heap layout whose array order is NOT sorted.
        payload = {"queue_running": [item(3, "run")],
                   "queue_pending": [item(4, "a"), item(9, "d"), item(5, "b"), item(-1, "front"), item(7, "c")]}
        got = [(i.status, i.prompt_id) for i in parse_queue(payload)]
        self.assertEqual(got, [("running", "run"), ("pending", "front"), ("pending", "a"),
                               ("pending", "b"), ("pending", "c"), ("pending", "d")])

    def test_tolerates_extra_tuple_elements_and_skips_garbage(self):
        payload = {"queue_running": [],
                   "queue_pending": [[1, "x", {}, {}, [], {"sensitive": 1}, "future"], "junk", [2], [3, None]]}
        got = parse_queue(payload)
        self.assertEqual([i.prompt_id for i in got], ["x"])

    def test_bad_payload_raises(self):
        with self.assertRaises(QueueFormatError):
            parse_queue({"queue_running": []})
        with self.assertRaises(QueueFormatError):
            parse_queue([])

    def test_missing_graph_warns(self):
        got = parse_queue({"queue_running": [], "queue_pending": [[1, "x"]]})
        self.assertEqual(got[0].prompt, {})
        self.assertTrue(got[0].warnings)


class PromptTests(unittest.TestCase):
    def check_both(self, graph, fn):
        for cat in (CATALOG, NO_CATALOG):
            with self.subTest(catalog=cat.available):
                fn(extract_prompts(graph, cat))

    def test_sdxl_positive_negative(self):
        def check(p):
            self.assertEqual(texts(p["positive"]), ["a cinematic portrait of a cat"])
            self.assertEqual(texts(p["negative"]), ["low quality, blurry"])
            self.assertEqual(p["other"], [])
        self.check_both(graphs.sdxl_txt2img(), check)

    def test_wan_pass_through_ports(self):
        def check(p):
            self.assertEqual(texts(p["positive"]), ["a woman walking on the beach"])
            self.assertEqual(texts(p["negative"]), ["static, blurry, worst quality"])
        self.check_both(graphs.wan_i2v(), check)

    def test_flux_basic_guider(self):
        def check(p):
            self.assertEqual(sorted(texts(p["positive"])), ["a neon city at night, rain", "neon city"])
            self.assertEqual(p["negative"], [])
        self.check_both(graphs.flux_guider(), check)

    def test_zero_out_negative_is_noted_not_misclassified(self):
        def check(p):
            self.assertEqual(texts(p["positive"]), ["sunset over mountains"])
            self.assertEqual(len(p["negative"]), 1)
            self.assertEqual(p["negative"][0]["field"], "")
            self.assertIn("zeroed", p["negative"][0]["note"])
            self.assertEqual(p["other"], [])
        self.check_both(graphs.zero_out_negative(), check)

    def test_shared_encoder_and_unknown_node_go_to_other(self):
        def check(p):
            self.assertEqual(p["positive"], [])
            self.assertEqual(p["negative"], [])
            self.assertEqual(texts(p["other"]), ["same text both ways", "unconnected custom prompt text"])
            self.assertIn("both", p["other"][0]["note"])
        self.check_both(graphs.shared_encoder_and_custom_text(), check)

    def test_linked_text_is_followed(self):
        def check(p):
            self.assertEqual(texts(p["positive"]), ["masterpiece, ", "a red fox"])
            self.assertEqual(texts(p["negative"]), ["ugly"])
            self.assertEqual(p["other"], [])
        self.check_both(graphs.string_concat_prompt(), check)

    def test_cycle_does_not_hang(self):
        g = {"1": graphs.node("Weird", conditioning=["2", 0]), "2": graphs.node("Weird", conditioning=["1", 0]),
             "3": graphs.node("KSampler", positive=["1", 0])}
        p = extract_prompts(g, NO_CATALOG)
        self.assertEqual(p["positive"], [])


class ModelTests(unittest.TestCase):
    def test_checkpoint_and_lora(self):
        models = extract_models(graphs.sdxl_txt2img())
        self.assertEqual([(m["kind"], m["display"]) for m in models],
                         [("checkpoint", "juggernautXL_v9"), ("lora", "detail_tweaker")])
        self.assertEqual(models[1]["strength"], "model 0.8, clip 1")
        self.assertEqual([m["display"] for m in main_models(models)], ["juggernautXL_v9"])

    def test_wan_models(self):
        kinds = [(m["kind"], m["display"]) for m in extract_models(graphs.wan_i2v())]
        self.assertEqual(kinds, [
            ("diffusion_model", "wan2.1_i2v_480p_14B_fp8"),
            ("text_encoder", "umt5_xxl_fp8"),
            ("clip_vision", "clip_vision_h"),
            ("vae", "wan_2.1_vae"),
        ])

    def test_dual_clip_and_linked_and_generic(self):
        g = graphs.flux_guider()
        g["40"] = graphs.node("SomePackLoader", model_file="custom\\folder\\thing.gguf")
        g["41"] = graphs.node("CheckpointLoaderSimple", ckpt_name=["99", 0])
        models = extract_models(g)
        got = [(m["kind"], m["display"]) for m in models]
        self.assertIn(("text_encoder", "t5xxl_fp16"), got)
        self.assertIn(("text_encoder", "clip_l"), got)
        self.assertIn(("other", "thing"), got)
        self.assertIn(("checkpoint", "(linked)"), got)

    def test_no_models(self):
        self.assertEqual(extract_models({}), [])


class ImageTests(unittest.TestCase):
    def test_annotated_subfolder(self):
        imgs = extract_input_images(graphs.img2img())
        self.assertEqual(len(imgs), 1)
        self.assertEqual((imgs[0]["filename"], imgs[0]["subfolder"], imgs[0]["type"]),
                         ("face_02.png", "faces", "input"))
        self.assertEqual(imgs[0]["classType"], "LoadImage")

    def test_parse_variants(self):
        r = parse_image_ref("a\\b\\c.png")
        self.assertEqual((r["subfolder"], r["filename"]), ("a/b", "c.png"))
        r = parse_image_ref("x.png [output]")
        self.assertEqual((r["filename"], r["type"]), ("x.png", "output"))
        r = parse_image_ref("blake3:0123456789abcdef")
        self.assertTrue(r["isAsset"])
        self.assertEqual(r["filename"], "blake3:0123456789abcdef")

    def test_multiple_dedup_and_none(self):
        g = {"1": graphs.node("LoadImage", image="a.png"), "2": graphs.node("LoadImage", image="b.png"),
             "3": graphs.node("LoadImage", image="a.png"), "4": graphs.node("LoadImageMask", image="m.png"),
             "5": graphs.node("SomeLoadImageBatch", image="not-an-image-path")}
        self.assertEqual([i["filename"] for i in extract_input_images(g)], ["a.png", "b.png", "m.png"])
        self.assertEqual(extract_input_images(graphs.sdxl_txt2img()), [])


class UsedNodesAndMultiLoaderTests(unittest.TestCase):
    def test_used_nodes_skip_stray(self):
        used = used_nodes(graphs.minimax_ref2va(), ["9"])
        self.assertNotIn("212", used)
        self.assertNotIn("213", used)
        self.assertIn("30", used)

    def test_unknown_outputs_mean_all_nodes(self):
        self.assertIsNone(used_nodes(graphs.minimax_ref2va(), []))
        self.assertIsNone(used_nodes(graphs.minimax_ref2va(), ["404"]))

    def test_ref2va_item(self):
        vm = QueueViewModel()
        payload = {"queue_running": [], "queue_pending": [
            [1, "r", graphs.minimax_ref2va(), {}, ["9"]]]}
        it = vm.build(payload, NO_CATALOG)[0]
        imgs = [(i["subfolder"], i["filename"], i["external"], i["comfyFolder"]) for i in it["inputImages"]]
        self.assertEqual(imgs, [("deno", "ref_face.png", False, True), ("", "hero.jpg", False, True),
                                ("", "bg.png", True, False)])  # disabled one and stray #212 left out
        self.assertEqual([m["display"] for m in it["mainModels"]], ["minimax_h3_ref2va"])  # not leftover
        self.assertEqual(texts(it["prompts"]["positive"]), ["<Picture 1> walks into <Picture 2>"])
        self.assertEqual(texts(it["prompts"]["negative"]), ["blurry"])
        self.assertEqual(it["unusedNodeCount"], 2)


class ViewModelTests(unittest.TestCase):
    def test_order_status_and_cache(self):
        vm = QueueViewModel()
        p1 = {"queue_running": [item(1, "a", graphs.sdxl_txt2img())],
              "queue_pending": [item(3, "c", graphs.wan_i2v()), item(2, "b", graphs.img2img())]}
        items = vm.build(p1, CATALOG)
        self.assertEqual([(i["promptId"], i["status"], i["order"]) for i in items],
                         [("a", "running", 1), ("b", "pending", 2), ("c", "pending", 3)])
        self.assertEqual(items[1]["inputImages"][0]["filename"], "face_02.png")

        # "a" finished, "b" started: same parsed details object is reused for b.
        cached_b = vm._cache["b"]
        p2 = {"queue_running": [item(2, "b", graphs.img2img())], "queue_pending": [item(3, "c", graphs.wan_i2v())]}
        items = vm.build(p2, CATALOG)
        self.assertEqual([(i["promptId"], i["status"], i["order"]) for i in items],
                         [("b", "running", 1), ("c", "pending", 2)])
        self.assertIs(vm._cache["b"], cached_b)
        self.assertNotIn("a", vm._cache)

    def test_large_queue(self):
        import time
        vm = QueueViewModel()
        payload = {"queue_running": [], "queue_pending": [item(n, f"id{n}", graphs.wan_i2v()) for n in range(300)]}
        t = time.perf_counter()
        vm.build(payload, CATALOG)
        first = time.perf_counter() - t
        t = time.perf_counter()
        vm.build(payload, CATALOG)
        second = time.perf_counter() - t
        self.assertLess(first, 2.0)
        self.assertLess(second, first)


if __name__ == "__main__":
    unittest.main()
