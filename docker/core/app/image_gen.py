"""Image generation via ComfyUI API on dominus."""

from __future__ import annotations

import json
import logging
import os
import uuid

import httpx

logger = logging.getLogger(__name__)

COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://host.docker.internal:8388")

# Simple txt2img workflow for ComfyUI
WORKFLOW_TEMPLATE = {
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 0,
            "steps": 20,
            "cfg": 7.0,
            "sampler_name": "euler",
            "scheduler": "normal",
            "denoise": 1.0,
            "model": ["4", 0],
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
    },
    "4": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {"ckpt_name": "sd_xl_base_1.0.safetensors"},
    },
    "5": {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": 512, "height": 512, "batch_size": 1},
    },
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "", "clip": ["4", 1]},
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "bad quality, blurry, ugly", "clip": ["4", 1]},
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


async def generate_image(prompt: str, width: int = 512, height: int = 512) -> bytes | None:
    """Generate an image via ComfyUI and return PNG bytes."""
    workflow = json.loads(json.dumps(WORKFLOW_TEMPLATE))

    # Set prompt and dimensions
    workflow["6"]["inputs"]["text"] = prompt
    workflow["5"]["inputs"]["width"] = width
    workflow["5"]["inputs"]["height"] = height
    workflow["3"]["inputs"]["seed"] = int(uuid.uuid4().int % (2**32))

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Queue the prompt
            r = await client.post(
                f"{COMFYUI_URL}/prompt",
                json={"prompt": workflow},
            )
            if r.status_code != 200:
                logger.error("ComfyUI queue failed: %s", r.text[:200])
                return None

            prompt_id = r.json().get("prompt_id")
            if not prompt_id:
                return None

            # Poll for completion
            import asyncio
            for _ in range(60):  # Max 60s
                await asyncio.sleep(1)
                r = await client.get(f"{COMFYUI_URL}/history/{prompt_id}")
                if r.status_code == 200:
                    history = r.json()
                    if prompt_id in history:
                        outputs = history[prompt_id].get("outputs", {})
                        # Find the SaveImage node output
                        for node_id, output in outputs.items():
                            images = output.get("images", [])
                            if images:
                                img = images[0]
                                # Download the image
                                r2 = await client.get(
                                    f"{COMFYUI_URL}/view",
                                    params={
                                        "filename": img["filename"],
                                        "subfolder": img.get("subfolder", ""),
                                        "type": img.get("type", "output"),
                                    },
                                )
                                if r2.status_code == 200:
                                    logger.info("Image generated: %s (%d bytes)", img["filename"], len(r2.content))
                                    return r2.content
                        return None

            logger.warning("Image generation timed out")
            return None
    except Exception as e:
        logger.error("Image generation failed: %s", e)
        return None
