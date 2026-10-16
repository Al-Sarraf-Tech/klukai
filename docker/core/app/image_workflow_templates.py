"""Pinned model/LoRA ComfyUI graph templates; no request or rendering state."""

from __future__ import annotations
from typing import Any

KLUKAI_LORA = "Klukai_GFL2_IL-03.safetensors"
KLUKAI_LORA_TRIGGER = "Klukai"

NEGATIVE_TAGS = (
    "lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, "
    "fewer digits, cropped, worst quality, low quality, normal quality, "
    "jpeg artifacts, signature, watermark, username, blurry, artist name, "
    "deformed, ugly, duplicate, morbid, mutilated, extra limbs, "
    "extra arms, extra legs, fused limbs, limbs through body, clipping, "
    "multiple people, multiple boys, multiple girls, clone, twin, "
    "two heads, two faces, extra faces, disfigured, "
    "interlocking limbs, overlapping bodies, merged bodies, "
    "thick thighs, wide hips, chubby, plump, fat, overweight, huge breasts, "
    "androgynous, feminine boy, crossdressing, male in female clothes, "
    "flat chest, child, loli, shota"
)

# Added to the negative prompt for wardrobe-driven renders below the intimacy
# gate (INTIMATE_MIN_LEVEL). Outfit tags that leave a layer unspecified ("open
# coat") let the model fill it with skin; this is the backstop to the tags.
SFW_NEGATIVE_TAGS = (
    "nsfw, nude, naked, nipples, topless, bare breasts, breasts out, no bra, "
    "underwear, panties, lingerie, open clothes, unbuttoned, exposed chest"
)


WORKFLOW_TEMPLATE: dict[str, Any] = {
    "4": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "noobai_xl_v1.safetensors"},
    },
    "10": {
        "class_type": "LoraLoader",
        "inputs": {
            "lora_name": KLUKAI_LORA,
            "strength_model": 0.75,
            "strength_clip": 0.75,
            "model": ["4", 0],
            "clip": ["4", 1],
        },
    },
    "5": {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": 832, "height": 1216, "batch_size": 1},
    },
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "", "clip": ["10", 1]},
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": NEGATIVE_TAGS, "clip": ["10", 1]},
    },
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 0,
            # The NoobAI-XL model card: Euler (not ancestral) at CFG 4-5. An A/B
            # at a fixed seed showed CFG 7 euler_a gives a saturated blue cast
            # and flat skin; CFG 4.5 Euler gives true blacks and shaded skin.
            # (karras schedules fry this checkpoint; keep "normal".)
            "steps": 24,
            "cfg": 4.5,
            "sampler_name": "euler",
            "scheduler": "normal",
            "denoise": 1.0,
            "model": ["10", 0],
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
    },
    "8": {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
    },
    "9": {
        "class_type": "SaveImage",
        "inputs": {"filename_prefix": "klukai_gen", "images": ["8", 0]},
    },
}

# img2img on the same model/LoRA chain: LoadImage -> VAEEncode replaces the
# empty latent, and KSampler.denoise (set per call) controls how far the result
# may drift from the source. Used for Live Portrait expression frames.
IMG2IMG_TEMPLATE: dict[str, Any] = {
    **{k: v for k, v in WORKFLOW_TEMPLATE.items() if k != "5"},
    "11": {"class_type": "LoadImage", "inputs": {"image": ""}},
    "12": {"class_type": "VAEEncode", "inputs": {"pixels": ["11", 0], "vae": ["4", 2]}},
    "3": {
        "class_type": "KSampler",
        "inputs": {
            **WORKFLOW_TEMPLATE["3"]["inputs"],
            "latent_image": ["12", 0],
        },
    },
}

# Masked img2img (inpaint) add-ons: only the white of the uploaded mask is
# re-sampled (DifferentialDiffusion honours its soft edge), and the decoded
# result is composited back onto the untouched source through the same mask,
# so every pixel outside it stays exactly the source. "{mask}" is filled in.
INPAINT_NODES: dict[str, Any] = {
    "13": {
        "class_type": "LoadImageMask",
        "inputs": {"image": "{mask}", "channel": "red"},
    },
    "14": {
        "class_type": "SetLatentNoiseMask",
        "inputs": {"samples": ["12", 0], "mask": ["13", 0]},
    },
    "16": {"class_type": "DifferentialDiffusion", "inputs": {"model": ["10", 0]}},
    "15": {
        "class_type": "ImageCompositeMasked",
        "inputs": {
            "destination": ["11", 0],
            "source": ["8", 0],
            "x": 0,
            "y": 0,
            "resize_source": False,
            "mask": ["13", 0],
        },
    },
}
