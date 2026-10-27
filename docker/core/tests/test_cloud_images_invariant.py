"""Static invariants: pictures are cloud-only, with no local ComfyUI loader in core.

Owner decision 2026-10-26: Klukai runs on cloud AI only. The ComfyUI provider,
the Dominus GPU lease and every ComfyUI-only path were removed; these checks
keep them from growing back.
"""

from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
CORE = REPO / "docker" / "core"
RETIRED_MODULES = (
    "image_comfy.py",
    "gpu_lease.py",
    "image_leases.py",
    "image_rendering.py",
    "image_inpaint.py",
    "image_workflows.py",
    "image_workflow_templates.py",
)
IMAGE_MODULES = (
    "image_gen.py",
    "image_gen_constants.py",
    "image_prompts.py",
    "image_provider.py",
    "agy_image_provider.py",
    "account_images.py",
    "background.py",
    "her_pov_pipeline.py",
    "portrait.py",
    "portrait_account.py",
    "portrait_style.py",
    "portrait_geometry.py",
    "portrait_cloud_frames.py",
    "routes_portrait.py",
)


def test_retired_local_renderer_modules_are_gone() -> None:
    assert [n for n in RETIRED_MODULES if (CORE / "app" / n).exists()] == []


def test_no_core_module_contains_comfy_job_endpoints() -> None:
    endpoints = ("/prompt", "/history", "/view", "/free", "/interrupt", "/upload/image")

    offenders: list[str] = []
    for source_path in sorted((CORE / "app").rglob("*.py")):
        source = source_path.read_text()
        if any(endpoint in source for endpoint in endpoints):
            offenders.append(str(source_path.relative_to(REPO)))

    assert offenders == []


def test_image_modules_never_name_the_local_renderer() -> None:
    forbidden = ("comfyui", "gpu_lease", "100.107.121.5", "get_lm_gate", "is_game_active")

    offenders = [
        f"{name}: {needle}"
        for name in IMAGE_MODULES
        for needle in forbidden
        if needle in (CORE / "app" / name).read_text().lower()
    ]
    assert offenders == []


def test_offline_loaders_reuse_the_cloud_dispatch() -> None:
    seed = (CORE / "seed_memories.py").read_text()
    regen = (REPO / "scripts" / "regen_images.py").read_text()

    # The retired scheduled seeder cannot load any renderer.
    assert "from app.image_gen import" not in seed
    assert "from app.image_gen import generate_image" in regen
    for source in (seed, regen):
        for raw_endpoint in ("/prompt", "/history", "/view", "/free", "/interrupt"):
            assert f'"{raw_endpoint}"' not in source
            assert f"'{raw_endpoint}'" not in source
