"""Klukai runs on cloud AI only (owner decision, 2026-10-26).

No inference of any kind may run on Dominus-Nobara or Amarillo for her: no
LM Studio / llama.cpp gateway, no local embeddings or Qdrant, no Dominus
voice, no ComfyUI or GPU lease. These checks fail if any of those endpoints,
settings or modules comes back into the core or its compose service.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[1]
APP = CORE / "app"
REPO = CORE.parents[1]

FORBIDDEN = {
    "Dominus Tailscale address": re.compile(r"100\.107\.121\.5"),
    "LM Studio gateway": re.compile(r"LM_STUDIO|lm_studio|lmstudio", re.IGNORECASE),
    "local embeddings service": re.compile(r"INFERENCE_URL|aichat-inference|/v1/embeddings"),
    "Qdrant vector store": re.compile(r"QDRANT|qdrant", re.IGNORECASE),
    "Dominus voice": re.compile(r"VOICE_STT_URL|VOICE_URL|VOICE_BACKEND|VOICE_API_TOKEN"),
    "ComfyUI": re.compile(r"COMFYUI_URL|comfyui", re.IGNORECASE),
    "GPU lease": re.compile(r"\bgpu_lease\b|GPULease"),
}

REMOVED_MODULES = (
    "lm_gateway.py",
    "warmup.py",
    "gpu_lease.py",
    "image_comfy.py",
    "image_leases.py",
)


def _sources() -> list[Path]:
    return sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)


@pytest.mark.parametrize("label", sorted(FORBIDDEN))
def test_core_has_no_local_ai_reference(label: str) -> None:
    pattern = FORBIDDEN[label]
    hits = [
        f"{path.relative_to(CORE)}:{number}"
        for path in _sources()
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if pattern.search(line)
    ]
    assert not hits, f"{label} referenced in the core: {hits}"


@pytest.mark.parametrize("name", REMOVED_MODULES)
def test_removed_local_modules_stay_removed(name: str) -> None:
    assert not (APP / name).exists()


def _core_service_block() -> str:
    compose = (REPO / "docker-compose.yml").read_text()
    start = compose.index("  companion-core:")
    following = re.search(r"\n  [a-z0-9-]+:\n", compose[start + 1 :])
    end = start + 1 + following.start() if following else len(compose)
    return compose[start:end]


@pytest.mark.parametrize("label", sorted(FORBIDDEN))
def test_core_service_has_no_local_ai_setting(label: str) -> None:
    pattern = FORBIDDEN[label]
    block = _core_service_block()
    hits = [line.strip() for line in block.splitlines() if pattern.search(line)]
    assert not hits, f"{label} configured for companion-core: {hits}"
