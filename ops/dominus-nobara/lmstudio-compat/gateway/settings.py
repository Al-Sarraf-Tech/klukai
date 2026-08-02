"""Environment-backed gateway settings with secret-file support."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path


def _read_secret(file_value: str | None, direct_value: str | None) -> str | None:
    if file_value:
        value = Path(file_value).read_text(encoding="utf-8").strip()
    else:
        value = (direct_value or "").strip()
    return value or None


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    upstream_url: str = "http://llama-router:8080"
    comfyui_url: str = "http://comfyui:8188"
    companion_voice_url: str = "http://companion-voice:8301"
    catalog_path: Path = Path("/config/models.lock.json")
    game_marker_path: Path = Path("/run/dominus/game-active")
    gpu_lease_marker_path: Path = Path("/run/dominus/non-llm-lease.json")
    gpu_lease_ack_path: Path = Path("/run/dominus/non-llm-lease-vllm-ack.json")
    require_native_vllm_ack: bool = True
    gateway_token: str | None = field(default=None, repr=False)
    upstream_token: str | None = field(default=None, repr=False)
    companion_voice_token: str | None = field(default=None, repr=False)
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 1800.0
    health_timeout_seconds: float = 2.0

    @classmethod
    def from_env(cls) -> "Settings":
        gateway_token_file = os.getenv("GATEWAY_BEARER_TOKEN_FILE") or os.getenv(
            "API_BEARER_TOKEN_FILE"
        )
        gateway_token = os.getenv("GATEWAY_BEARER_TOKEN") or os.getenv(
            "API_BEARER_TOKEN"
        )
        upstream_token_file = os.getenv("UPSTREAM_BEARER_TOKEN_FILE") or os.getenv(
            "UPSTREAM_API_KEY_FILE"
        )
        upstream_token = os.getenv("UPSTREAM_BEARER_TOKEN") or os.getenv(
            "UPSTREAM_API_KEY"
        )
        companion_voice_token_file = os.getenv(
            "COMPANION_VOICE_BEARER_TOKEN_FILE"
        ) or os.getenv("VOICE_API_TOKEN_FILE")
        companion_voice_token = os.getenv(
            "COMPANION_VOICE_BEARER_TOKEN"
        ) or os.getenv("VOICE_API_TOKEN")
        resolved_gateway_token = _read_secret(gateway_token_file, gateway_token)
        if resolved_gateway_token is None:
            raise ValueError(
                "GATEWAY_BEARER_TOKEN_FILE or GATEWAY_BEARER_TOKEN is required"
            )
        resolved_voice_token = _read_secret(
            companion_voice_token_file, companion_voice_token
        )
        if resolved_voice_token is None:
            raise ValueError(
                "COMPANION_VOICE_BEARER_TOKEN_FILE or "
                "COMPANION_VOICE_BEARER_TOKEN is required"
            )
        return cls(
            upstream_url=os.getenv("LLAMA_CPP_URL", "http://llama-router:8080").rstrip(
                "/"
            ),
            comfyui_url=os.getenv("COMFYUI_URL", "http://comfyui:8188").rstrip("/"),
            companion_voice_url=os.getenv(
                "COMPANION_VOICE_URL", "http://companion-voice:8301"
            ).rstrip("/"),
            catalog_path=Path(os.getenv("MODEL_LOCK_PATH", "/config/models.lock.json")),
            game_marker_path=Path(
                os.getenv("GAME_ACTIVE_MARKER", "/run/dominus/game-active")
            ),
            gpu_lease_marker_path=Path(
                os.getenv(
                    "GPU_LEASE_MARKER", "/run/dominus/non-llm-lease.json"
                )
            ),
            gpu_lease_ack_path=Path(
                os.getenv(
                    "GPU_LEASE_VLLM_ACK",
                    "/run/dominus/non-llm-lease-vllm-ack.json",
                )
            ),
            # Production can never bypass native-vLLM quiescence. The field is
            # injectable only so isolated unit tests need no host systemd.
            require_native_vllm_ack=True,
            gateway_token=resolved_gateway_token,
            upstream_token=_read_secret(upstream_token_file, upstream_token),
            companion_voice_token=resolved_voice_token,
            connect_timeout_seconds=_float_env("UPSTREAM_CONNECT_TIMEOUT", 10.0),
            read_timeout_seconds=_float_env("UPSTREAM_READ_TIMEOUT", 1800.0),
            health_timeout_seconds=_float_env("UPSTREAM_HEALTH_TIMEOUT", 2.0),
        )
