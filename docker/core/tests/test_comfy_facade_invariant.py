"""Static acceptance invariants for the protected ComfyUI loader boundary."""

from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
CORE = REPO / "docker" / "core"
FACADE = "http://100.107.121.5:1234/api/v1/comfy"


def test_runtime_default_has_no_raw_comfyui_tailnet_route() -> None:
    image_gen = (CORE / "app" / "image_gen.py").read_text()
    compose = (REPO / "docker-compose.yml").read_text()
    env_example = (REPO / ".env.example").read_text()

    assert FACADE in image_gen
    assert FACADE in compose
    assert FACADE in env_example
    for source in (image_gen, compose, env_example):
        assert "100.107.121.5:8388" not in source


def test_offline_loaders_reuse_the_protected_production_path() -> None:
    seed = (CORE / "seed_memories.py").read_text()
    regen = (REPO / "scripts" / "regen_images.py").read_text()

    assert "from app.image_gen import" in seed
    assert "free_comfyui_vram" in seed
    assert "from app.image_gen import generate_image" in regen
    for source in (seed, regen):
        for raw_endpoint in ("/prompt", "/history", "/view", "/free", "/interrupt"):
            assert f'"{raw_endpoint}"' not in source
            assert f"'{raw_endpoint}'" not in source


def test_only_protected_module_contains_comfy_job_endpoints() -> None:
    """A new core loader must not silently grow outside the leased module."""
    allowed = CORE / "app" / "image_gen.py"
    endpoints = ("/prompt", "/history", "/view", "/free", "/interrupt")

    offenders: list[str] = []
    for source_path in sorted((CORE / "app").rglob("*.py")):
        if source_path == allowed:
            continue
        source = source_path.read_text()
        if any(endpoint in source for endpoint in endpoints):
            offenders.append(str(source_path.relative_to(REPO)))

    assert offenders == []


def test_every_protected_comfy_call_uses_lease_header_helper() -> None:
    source = (CORE / "app" / "image_gen.py").read_text()

    # queue, interrupt, free, prompt, history, and view each attach both the
    # gateway bearer credential and the active lease token through one helper.
    assert source.count("headers=gpu_lease_auth_headers(lease)") == 6
