"""Load and normalize the immutable Dominus model catalog."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


class CatalogError(ValueError):
    """Raised when ``models.lock.json`` cannot describe a safe catalog."""


def _first(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _as_aliases(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise CatalogError("model aliases must be a string or an array")

    aliases: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise CatalogError("every model alias must be a non-empty string")
        alias = item.strip()
        if alias not in aliases:
            aliases.append(alias)
    return tuple(aliases)


def _quantization_name(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("name")
    if value is None:
        return "unknown"
    return str(value)


def quantization_bits(quantization: str) -> int | None:
    """Best-effort bits-per-weight value for LM Studio's v1 response."""

    upper = quantization.upper()
    if upper in {"BF16", "F16", "FP16"}:
        return 16
    if upper in {"F32", "FP32"}:
        return 32
    match = re.search(r"(?:^|[^0-9])Q?([2-8])(?:[^0-9]|$)", upper)
    if match:
        return int(match.group(1))
    if "MXFP4" in upper:
        return 4
    return None


def _normalized_state(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("value")
    return "loaded" if str(value).lower() == "loaded" else "not-loaded"


@dataclass(frozen=True, slots=True)
class Model:
    id: str
    aliases: tuple[str, ...]
    router_id: str
    type: str
    quantization: str
    max_context_length: int
    loaded_context_length: int | None
    publisher: str
    architecture: str | None
    compatibility_type: str
    size_bytes: int
    display_name: str
    params_string: str | None
    capabilities: dict[str, Any] | list[str]
    state_hint: str

    @property
    def v0_type(self) -> str:
        if self.type in {"embedding", "embeddings"}:
            return "embeddings"
        return self.type

    @property
    def v1_type(self) -> str:
        return "embedding" if self.type in {"embedding", "embeddings"} else "llm"

    def v0_capabilities(self) -> list[str]:
        if isinstance(self.capabilities, list):
            result = list(self.capabilities)
        else:
            result = [
                key for key, enabled in self.capabilities.items() if enabled is True
            ]
        if self.type == "vlm" and "vision" not in result:
            result.append("vision")
        return result

    def v1_capabilities(self) -> dict[str, Any] | None:
        if self.v1_type == "embedding":
            return None
        if isinstance(self.capabilities, dict):
            result = dict(self.capabilities)
        else:
            result = {
                "vision": "vision" in self.capabilities,
                "trained_for_tool_use": "tool_use" in self.capabilities,
            }
        result.setdefault("vision", self.type == "vlm")
        return result


class Catalog:
    """Validated model catalog plus alias and llama-router mappings."""

    def __init__(self, models: list[Model]) -> None:
        self.models = tuple(models)
        self._by_name: dict[str, Model] = {}
        for model in self.models:
            for name in (model.id, model.router_id, *model.aliases):
                existing = self._by_name.get(name)
                if existing is not None and existing != model:
                    raise CatalogError(
                        f"model name {name!r} is assigned to both "
                        f"{existing.id!r} and {model.id!r}"
                    )
                self._by_name[name] = model

    def resolve(self, name: str) -> Model | None:
        return self._by_name.get(name)

    @classmethod
    def from_path(cls, path: str | Path) -> "Catalog":
        source = Path(path)
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CatalogError(f"cannot read model catalog {source}: {exc}") from exc
        return cls.from_document(raw)

    @classmethod
    def from_document(cls, document: Any) -> "Catalog":
        if not isinstance(document, dict):
            raise CatalogError("model catalog must be a JSON object")

        catalog_document = document.get("catalog", document)
        if not isinstance(catalog_document, dict):
            raise CatalogError("catalog field must be a JSON object")

        artifacts = document.get("artifacts", catalog_document.get("artifacts", []))
        if not isinstance(artifacts, list):
            raise CatalogError("artifacts must be an array")
        artifact_by_id = {
            str(item["id"]): item
            for item in artifacts
            if isinstance(item, dict) and item.get("id") is not None
        }

        entries = catalog_document.get("models")
        using_artifact_fallback = not isinstance(entries, list)
        if using_artifact_fallback:
            entries = artifacts
        if not isinstance(entries, list):
            raise CatalogError("models must be an array")

        models: list[Model] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise CatalogError("every model must be a JSON object")
            if entry.get("enabled") is False:
                continue

            runtime = entry.get("runtime", {})
            if runtime is None:
                runtime = {}
            if not isinstance(runtime, dict):
                raise CatalogError("artifact runtime metadata must be a JSON object")
            metadata = {**entry, **runtime}

            artifact_kind = str(
                _first(entry, "role", "kind", "artifact_type", default="")
            ).lower()
            if (
                using_artifact_fallback
                and artifact_kind
                and artifact_kind
                not in {
                    "llm",
                    "vlm",
                    "embedding",
                    "embeddings",
                }
            ):
                continue

            entry_aliases = _as_aliases(entry.get("aliases", entry.get("alias")))
            runtime_aliases = _as_aliases(runtime.get("aliases", runtime.get("alias")))
            all_aliases = tuple(dict.fromkeys((*entry_aliases, *runtime_aliases)))

            if using_artifact_fallback:
                # Artifact ids are immutable transfer-inventory keys, not the
                # historical IDs clients used with LM Studio. The first runtime
                # alias is the API and llama-router preset name.
                model_id_value = _first(runtime, "model_id", "api_id", "key", "name")
                if model_id_value is None and runtime_aliases:
                    model_id_value = runtime_aliases[0]
                if model_id_value is None:
                    model_id_value = _first(entry, "model_id", "id", "key", "name")
            else:
                model_id_value = _first(metadata, "model_id", "id", "key", "name")
            if not isinstance(model_id_value, str) or not model_id_value.strip():
                raise CatalogError("every model must have a non-empty id")
            model_id = model_id_value.strip()

            aliases = tuple(alias for alias in all_aliases if alias != model_id)
            router_id_value = _first(
                metadata, "router_id", "llama_id", "preset", default=model_id
            )
            if not isinstance(router_id_value, str) or not router_id_value.strip():
                raise CatalogError(f"model {model_id!r} has an invalid router id")
            router_id = router_id_value.strip()

            artifact_ids = metadata.get("artifact_ids", [])
            if isinstance(artifact_ids, str):
                artifact_ids = [artifact_ids]
            if not isinstance(artifact_ids, list):
                raise CatalogError(f"model {model_id!r} artifact_ids must be an array")
            related_artifacts = [
                artifact_by_id[str(artifact_id)]
                for artifact_id in artifact_ids
                if str(artifact_id) in artifact_by_id
            ]
            explicit_size = _first(metadata, "size_bytes", "bytes")
            if explicit_size is None:
                explicit_size = sum(
                    int(item.get("bytes", item.get("size_bytes", 0)) or 0)
                    for item in related_artifacts
                )
            try:
                size_bytes = max(0, int(explicit_size or 0))
            except (TypeError, ValueError) as exc:
                raise CatalogError(f"model {model_id!r} has invalid bytes") from exc

            context_value = _first(
                metadata,
                "max_context_length",
                "context_length",
                "context",
                default=0,
            )
            try:
                max_context_length = max(0, int(context_value or 0))
            except (TypeError, ValueError) as exc:
                raise CatalogError(
                    f"model {model_id!r} has invalid max_context_length"
                ) from exc

            loaded_context_value = _first(
                metadata,
                "loaded_context_length",
                "preset_context_length",
                "load_context_length",
            )
            try:
                loaded_context_length = (
                    max(0, int(loaded_context_value))
                    if loaded_context_value is not None
                    else None
                )
            except (TypeError, ValueError) as exc:
                raise CatalogError(
                    f"model {model_id!r} has invalid loaded_context_length"
                ) from exc

            default_model_type = (
                artifact_kind
                if artifact_kind in {"vlm", "embedding", "embeddings"}
                else "llm"
            )
            model_type = str(
                _first(metadata, "type", "model_type", default=default_model_type)
            ).lower()
            if model_type not in {"llm", "vlm", "embedding", "embeddings"}:
                raise CatalogError(
                    f"model {model_id!r} has invalid type {model_type!r}"
                )

            capabilities = metadata.get("capabilities", {})
            if not isinstance(capabilities, (dict, list)):
                raise CatalogError(
                    f"model {model_id!r} capabilities must be an object or array"
                )
            if isinstance(capabilities, list) and not all(
                isinstance(item, str) for item in capabilities
            ):
                raise CatalogError(
                    f"model {model_id!r} capabilities must contain strings"
                )

            publisher = str(
                metadata.get("publisher")
                or (model_id.split("/", 1)[0] if "/" in model_id else "local")
            )
            display_name = str(
                metadata.get("display_name") or model_id.rsplit("/", 1)[-1]
            )
            architecture_value = _first(metadata, "architecture", "arch")
            architecture = (
                str(architecture_value) if architecture_value is not None else None
            )
            params_value = metadata.get("params_string")

            models.append(
                Model(
                    id=model_id,
                    aliases=aliases,
                    router_id=router_id,
                    type=model_type,
                    quantization=_quantization_name(
                        _first(metadata, "quantization", "quant", default="unknown")
                    ),
                    max_context_length=max_context_length,
                    loaded_context_length=loaded_context_length,
                    publisher=publisher,
                    architecture=architecture,
                    compatibility_type=str(
                        _first(
                            metadata,
                            "compatibility_type",
                            "format",
                            default="gguf",
                        )
                    ),
                    size_bytes=size_bytes,
                    display_name=display_name,
                    params_string=str(params_value)
                    if params_value is not None
                    else None,
                    capabilities=capabilities,
                    state_hint=_normalized_state(metadata.get("state", "not-loaded")),
                )
            )

        if not models:
            raise CatalogError("model catalog contains no models")
        return cls(models)


def router_state(value: Any) -> str:
    """Translate a llama.cpp router status into LM Studio v0 state."""

    return _normalized_state(value)
