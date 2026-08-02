from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.catalog import Catalog, CatalogError, quantization_bits


def test_preferred_models_schema_and_artifact_sizes() -> None:
    catalog = Catalog.from_document(
        {
            "models": [
                {
                    "id": "publisher/model",
                    "aliases": ["old-name", "short-name"],
                    "router_id": "router-preset",
                    "type": "vlm",
                    "quantization": "Q4_K_M",
                    "max_context_length": 131072,
                    "loaded_context_length": 65536,
                    "artifact_ids": ["weights", "projection"],
                }
            ],
            "artifacts": [
                {"id": "weights", "destination": "model.gguf", "bytes": 100},
                {"id": "projection", "destination": "mmproj.gguf", "bytes": 20},
            ],
        }
    )

    model = catalog.models[0]
    assert model.size_bytes == 120
    assert model.max_context_length == 131072
    assert model.loaded_context_length == 65536
    assert model.v0_type == "vlm"
    assert model.v1_type == "llm"
    capabilities = model.v1_capabilities()
    assert capabilities is not None
    assert capabilities["vision"] is True
    assert catalog.resolve("old-name") == model
    assert catalog.resolve("router-preset") == model


def test_artifacts_schema_fallback() -> None:
    catalog = Catalog.from_document(
        {
            "artifacts": [
                {
                    "id": "embedding-model",
                    "aliases": "embed",
                    "type": "embeddings",
                    "quant": "Q8_0",
                    "context_length": 2048,
                    "destination": "embedding.gguf",
                    "bytes": 1234,
                },
                {
                    "id": "embedding-mmproj",
                    "role": "mmproj",
                    "destination": "mmproj.gguf",
                    "bytes": 12,
                },
            ]
        }
    )

    assert len(catalog.models) == 1
    assert catalog.models[0].v1_type == "embedding"
    assert catalog.models[0].max_context_length == 2048
    assert catalog.resolve("embed") == catalog.models[0]


def test_migration_artifact_schema_uses_runtime_metadata() -> None:
    catalog = Catalog.from_document(
        {
            "artifacts": [
                {
                    "id": "llm-inventory-key",
                    "enabled": True,
                    "kind": "llm",
                    "destination": "llm/example/model.gguf",
                    "size_bytes": 1234,
                    "runtime": {
                        "aliases": ["publisher/model", "legacy-model"],
                        "model_type": "vlm",
                        "quantization": "Q4_K_M",
                        "max_context_length": 131072,
                    },
                },
                {
                    "id": "projection-inventory-key",
                    "enabled": True,
                    "kind": "llm_mmproj",
                    "destination": "llm/example/mmproj.gguf",
                    "size_bytes": 200,
                },
                {
                    "id": "image-inventory-key",
                    "enabled": True,
                    "kind": "comfy_checkpoint",
                    "destination": "comfy/checkpoints/image.safetensors",
                    "size_bytes": 500,
                    "runtime": {"aliases": ["image.safetensors"]},
                },
                {
                    "id": "disabled-model",
                    "enabled": False,
                    "kind": "llm",
                    "runtime": {"aliases": ["disabled"]},
                },
            ]
        }
    )

    assert len(catalog.models) == 1
    model = catalog.models[0]
    assert model.id == "publisher/model"
    assert model.router_id == "publisher/model"
    assert model.aliases == ("legacy-model",)
    assert model.type == "vlm"
    assert model.quantization == "Q4_K_M"
    assert model.max_context_length == 131072
    assert model.size_bytes == 1234
    assert catalog.resolve("llm-inventory-key") is None


def test_checked_in_migration_lock_has_only_runnable_model_artifacts() -> None:
    lock_path = Path(__file__).parents[2] / "models.lock.json"
    raw = json.loads(lock_path.read_text(encoding="utf-8"))
    catalog = Catalog.from_document(raw)
    expected = [
        artifact
        for artifact in raw["artifacts"]
        if artifact.get("enabled", True)
        and artifact.get("kind") in {"llm", "embedding"}
    ]

    assert len(catalog.models) == len(expected)
    assert all("mmproj" not in model.id.lower() for model in catalog.models)
    assert catalog.resolve("cognitivecomputations_dolphin-mistral-24b-venice-edition")


def test_duplicate_aliases_fail_closed() -> None:
    with pytest.raises(CatalogError, match="assigned to both"):
        Catalog.from_document(
            {
                "models": [
                    {"id": "one", "aliases": ["shared"]},
                    {"id": "two", "aliases": ["shared"]},
                ]
            }
        )


@pytest.mark.parametrize(
    ("name", "bits"),
    [("Q4_K_M", 4), ("Q8_0", 8), ("BF16", 16), ("MXFP4", 4), ("unknown", None)],
)
def test_quantization_bits(name: str, bits: int | None) -> None:
    assert quantization_bits(name) == bits
