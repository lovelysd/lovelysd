"""Hand-written API-format graphs and a minimal /object_info used by tests.

Node class names, input names and output names mirror ComfyUI core
(nodes.py, comfy_extras/*) as of 2026-10.
"""


def node(cls, title=None, **inputs):
    return {"class_type": cls, "inputs": inputs, "_meta": {"title": title or cls}}


def sdxl_txt2img():
    return {
        "4": node("CheckpointLoaderSimple", "Load Checkpoint", ckpt_name="sdxl/juggernautXL_v9.safetensors"),
        "10": node("LoraLoader", lora_name="detail_tweaker.safetensors", strength_model=0.8,
                   strength_clip=1.0, model=["4", 0], clip=["4", 1]),
        "6": node("CLIPTextEncode", "Positive Prompt", text="a cinematic portrait of a cat", clip=["10", 1]),
        "7": node("CLIPTextEncode", "Negative Prompt", text="low quality, blurry", clip=["10", 1]),
        "5": node("EmptyLatentImage", width=1024, height=1024, batch_size=1),
        "3": node("KSampler", seed=1, steps=20, cfg=7.0, sampler_name="euler", scheduler="normal",
                  denoise=1.0, model=["10", 0], positive=["6", 0], negative=["7", 0], latent_image=["5", 0]),
        "8": node("VAEDecode", samples=["3", 0], vae=["4", 2]),
        "9": node("SaveImage", filename_prefix="ComfyUI", images=["8", 0]),
    }


def img2img():
    g = sdxl_txt2img()
    g["11"] = node("LoadImage", "Load Image", image="faces/face_02.png [input]")
    g["12"] = node("VAEEncode", pixels=["11", 0], vae=["4", 2])
    g["3"]["inputs"]["latent_image"] = ["12", 0]
    return g


def wan_i2v():
    """Titles deliberately misleading: role must come from graph wiring, not titles."""
    return {
        "37": node("UNETLoader", unet_name="wan2.1_i2v_480p_14B_fp8.safetensors", weight_dtype="default"),
        "38": node("CLIPLoader", clip_name="umt5_xxl_fp8.safetensors", type="wan", device="default"),
        "39": node("VAELoader", vae_name="wan_2.1_vae.safetensors"),
        "49": node("CLIPVisionLoader", clip_name="clip_vision_h.safetensors"),
        "52": node("LoadImage", image="IMG_2034.png"),
        "51": node("CLIPVisionEncode", clip_vision=["49", 0], image=["52", 0], crop="none"),
        "6": node("CLIPTextEncode", "Prompt A", text="a woman walking on the beach", clip=["38", 0]),
        "7": node("CLIPTextEncode", "Prompt B", text="static, blurry, worst quality", clip=["38", 0]),
        "50": node("WanImageToVideo", positive=["6", 0], negative=["7", 0], vae=["39", 0],
                   width=832, height=480, length=81, batch_size=1,
                   clip_vision_output=["51", 0], start_image=["52", 0]),
        "3": node("KSampler", seed=1, steps=20, cfg=6.0, sampler_name="uni_pc", scheduler="simple",
                  denoise=1.0, model=["37", 0], positive=["50", 0], negative=["50", 1], latent_image=["50", 2]),
    }


def flux_guider():
    return {
        "12": node("UNETLoader", unet_name="flux1-dev-fp8.safetensors", weight_dtype="fp8_e4m3fn"),
        "11": node("DualCLIPLoader", clip_name1="t5xxl_fp16.safetensors", clip_name2="clip_l.safetensors",
                   type="flux"),
        "6": node("CLIPTextEncodeFlux", clip=["11", 0], clip_l="neon city", t5xxl="a neon city at night, rain",
                  guidance=3.5),
        "26": node("FluxGuidance", conditioning=["6", 0], guidance=3.5),
        "22": node("BasicGuider", model=["12", 0], conditioning=["26", 0]),
        "13": node("SamplerCustomAdvanced", noise=["25", 0], guider=["22", 0], sampler=["16", 0],
                   sigmas=["17", 0], latent_image=["27", 0]),
    }


def zero_out_negative():
    return {
        "4": node("CheckpointLoaderSimple", ckpt_name="model.safetensors"),
        "6": node("CLIPTextEncode", text="sunset over mountains", clip=["4", 1]),
        "30": node("ConditioningZeroOut", conditioning=["6", 0]),
        "3": node("KSampler", model=["4", 0], positive=["6", 0], negative=["30", 0], latent_image=["5", 0],
                  seed=1, steps=20, cfg=1.0, sampler_name="euler", scheduler="simple", denoise=1.0),
    }


def shared_encoder_and_custom_text():
    return {
        "4": node("CheckpointLoaderSimple", ckpt_name="model.safetensors"),
        "6": node("CLIPTextEncode", text="same text both ways", clip=["4", 1]),
        "3": node("KSampler", model=["4", 0], positive=["6", 0], negative=["6", 0], latent_image=["5", 0]),
        "90": node("MyCustomPromptNode", "Style notes", prompt="unconnected custom prompt text"),
    }


def string_concat_prompt():
    return {
        "4": node("CheckpointLoaderSimple", ckpt_name="model.safetensors"),
        "20": node("StringConcatenate", string_a="masterpiece, ", string_b="a red fox", delimiter=""),
        "6": node("CLIPTextEncode", text=["20", 0], clip=["4", 1]),
        "7": node("CLIPTextEncode", text="ugly", clip=["4", 1]),
        "3": node("KSampler", model=["4", 0], positive=["6", 0], negative=["7", 0], latent_image=["5", 0]),
    }


def minimax_ref2va():
    """Ref2VA with Deno's multi reference loader, plus a stray Load Image (#212)
    that feeds no output - as left behind in a shared workflow template."""
    return {
        "1": node("UNETLoader", unet_name="minimax_h3_ref2va.safetensors", weight_dtype="default"),
        "2": node("CLIPLoader", clip_name="qwen3_vl_8b.safetensors", type="minimax", device="default"),
        "4": node("VAELoader", vae_name="minimax_h3_vae.safetensors"),
        "30": node("DenoMiniMaxH3ReferenceImageLoader", "(Deno) MiniMax H3 Multi Reference Image Loader",
                   image_paths="deno/ref_face.png\nhero.jpg\nunused_pose.png\nD:\\Pictures\\bg.png",
                   disabled_image_paths="unused_pose.png", audio_sources="[]"),
        "31": node("DenoMiniMaxH3ReferenceToVideo", prompt="<Picture 1> walks into <Picture 2>",
                   clip=["2", 0], vae=["4", 0], ref_images=["30", 0], width=1344, height=768, length=124,
                   ref_image_size="match"),
        "7": node("CLIPTextEncode", "Negative", text="blurry", clip=["2", 0]),
        "3": node("KSampler", model=["1", 0], positive=["31", 0], negative=["7", 0], latent_image=["31", 1]),
        "8": node("VAEDecode", samples=["3", 0], vae=["4", 0]),
        "9": node("SaveVideo", video=["8", 0], filename_prefix="ref2va"),
        "212": node("LoadImage", "Load Image", image="file_000000006c8871faaa53e993cd03ebaa.png"),
        "213": node("UNETLoader", unet_name="leftover_model.safetensors", weight_dtype="default"),
    }


def _info(inputs, outputs, names=None, optional=None):
    return {
        "input": {"required": inputs, "optional": optional or {}},
        "output": outputs,
        "output_name": names or outputs,
    }


MULTILINE = ["STRING", {"multiline": True}]
COND = ["CONDITIONING"]

OBJECT_INFO = {
    "CLIPTextEncode": _info({"text": MULTILINE, "clip": ["CLIP"]}, ["CONDITIONING"]),
    "CLIPTextEncodeFlux": _info({"clip": ["CLIP"], "clip_l": MULTILINE, "t5xxl": MULTILINE,
                                 "guidance": ["FLOAT", {}]}, ["CONDITIONING"]),
    "KSampler": _info({"model": ["MODEL"], "positive": COND, "negative": COND, "latent_image": ["LATENT"]},
                      ["LATENT"]),
    "WanImageToVideo": _info({"positive": COND, "negative": COND, "vae": ["VAE"]},
                             ["CONDITIONING", "CONDITIONING", "LATENT"], ["positive", "negative", "latent"],
                             optional={"clip_vision_output": ["CLIP_VISION_OUTPUT"], "start_image": ["IMAGE"]}),
    "FluxGuidance": _info({"conditioning": COND, "guidance": ["FLOAT", {}]}, ["CONDITIONING"]),
    "BasicGuider": _info({"model": ["MODEL"], "conditioning": COND}, ["GUIDER"]),
    "ConditioningZeroOut": _info({"conditioning": COND}, ["CONDITIONING"]),
    "StringConcatenate": _info({"string_a": MULTILINE, "string_b": MULTILINE,
                                "delimiter": ["STRING", {"multiline": False}]}, ["STRING"]),
    "SaveImage": _info({"images": ["IMAGE"], "filename_prefix": ["STRING", {"default": "ComfyUI"}]}, []),
}
