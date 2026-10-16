"""Pure ComfyUI graph construction and immutable sampling parameters."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from .image_gen_constants import WORKFLOW_TEMPLATE
from .image_prompts import negative_prompt

_TEMPLATE_KSAMPLER = WORKFLOW_TEMPLATE["3"]["inputs"]


@dataclass(frozen=True)
class Sampling:
    """KSampler settings; the defaults are WORKFLOW_TEMPLATE's (the model's own)."""

    sampler: str = _TEMPLATE_KSAMPLER["sampler_name"]
    scheduler: str = _TEMPLATE_KSAMPLER["scheduler"]
    steps: int = _TEMPLATE_KSAMPLER["steps"]
    cfg: float = _TEMPLATE_KSAMPLER["cfg"]


@dataclass(frozen=True)
class InpaintBranch:
    """One masked re-sample of the shared source (mask: white = repaint)."""

    name: str
    prompt: str
    denoise: float
    mask_png: bytes
    negative: str = ""  # extra negatives for this branch only


def _txt2img_workflow(
    prompt: str,
    width: int,
    height: int,
    sfw: bool,
    seed: int | None,
    negative_extra: str | None,
    sampling: Sampling | None = None,
) -> dict[str, Any]:
    workflow: dict[str, Any] = json.loads(json.dumps(WORKFLOW_TEMPLATE))
    workflow["6"]["inputs"]["text"] = prompt
    workflow["7"]["inputs"]["text"] = negative_prompt(sfw, negative_extra)
    workflow["5"]["inputs"]["width"] = width
    workflow["5"]["inputs"]["height"] = height
    workflow["3"]["inputs"]["seed"] = (
        int(seed) % (2**32) if seed is not None else int(uuid.uuid4().int % (2**32))
    )
    if sampling is not None:
        workflow["3"]["inputs"].update(
            sampler_name=sampling.sampler,
            scheduler=sampling.scheduler,
            steps=sampling.steps,
            cfg=sampling.cfg,
        )
    return workflow


def _inpaint_graph(
    source: str,
    branches: list[InpaintBranch],
    masks: dict[str, str],
    seed: int,
    sfw: bool,
    negative_extra: str | None,
    sampling: Sampling,
) -> tuple[dict[str, Any], dict[str, str]]:
    """One ComfyUI graph: a shared checkpoint/LoRA load and source encode, and
    one masked KSampler -> VAEDecode -> SaveImage branch per expression.
    Returns the graph and {branch name: SaveImage node id}."""
    graph: dict[str, Any] = {
        "4": json.loads(json.dumps(WORKFLOW_TEMPLATE["4"])),
        "10": json.loads(json.dumps(WORKFLOW_TEMPLATE["10"])),
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_prompt(sfw, negative_extra), "clip": ["10", 1]},
        },
        "11": {"class_type": "LoadImage", "inputs": {"image": source}},
        "12": {
            "class_type": "VAEEncode",
            "inputs": {"pixels": ["11", 0], "vae": ["4", 2]},
        },
        "16": {"class_type": "DifferentialDiffusion", "inputs": {"model": ["10", 0]}},
    }
    save_ids: dict[str, str] = {}
    for i, branch in enumerate(branches):
        n = 100 + 10 * i
        ids = [str(n + k) for k in range(6)]
        graph[ids[0]] = {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": branch.prompt, "clip": ["10", 1]},
        }
        negative = ["7", 0]
        if branch.negative:
            neg_id = str(n + 9)
            graph[neg_id] = {
                "class_type": "CLIPTextEncode",
                "inputs": {
                    "text": f"{negative_prompt(sfw, negative_extra)}, {branch.negative}",
                    "clip": ["10", 1],
                },
            }
            negative = [neg_id, 0]
        graph[ids[1]] = {
            "class_type": "LoadImageMask",
            "inputs": {"image": masks[branch.name], "channel": "red"},
        }
        graph[ids[2]] = {
            "class_type": "SetLatentNoiseMask",
            "inputs": {"samples": ["12", 0], "mask": [ids[1], 0]},
        }
        graph[ids[3]] = {
            "class_type": "KSampler",
            "inputs": {
                "seed": int(seed) % (2**32),
                "steps": sampling.steps,
                "cfg": sampling.cfg,
                "sampler_name": sampling.sampler,
                "scheduler": sampling.scheduler,
                "denoise": max(0.0, min(1.0, float(branch.denoise))),
                "model": ["16", 0],
                "positive": [ids[0], 0],
                "negative": negative,
                "latent_image": [ids[2], 0],
            },
        }
        graph[ids[4]] = {
            "class_type": "VAEDecode",
            "inputs": {"samples": [ids[3], 0], "vae": ["4", 2]},
        }
        graph[ids[5]] = {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": f"klukai_set_{branch.name}",
                "images": [ids[4], 0],
            },
        }
        save_ids[branch.name] = ids[5]
    return graph, save_ids


DEFAULT_SAMPLING = Sampling()
