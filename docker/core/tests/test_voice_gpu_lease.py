"""GPU arbitration contract for every core -> XTTS request."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.gpu_lease import GPULease
from app.voice_client import post_leased_tts


@pytest.mark.asyncio
async def test_tts_holds_local_gate_and_remote_lease_with_separate_tokens(
    monkeypatch,
):
    events: list[str] = []

    class Gate:
        async def __aenter__(self):
            events.append("gate-enter")

        async def __aexit__(self, *_args):
            events.append("gate-exit")

    @asynccontextmanager
    async def lease(workload: str):
        assert workload == "companion-voice"
        events.append("lease-enter")
        try:
            yield GPULease(ttl_seconds=600, token="opaque-gpu-capability")
        finally:
            events.append("lease-exit")

    client = MagicMock()

    async def post(url, **kwargs):
        events.append("tts-post")
        assert url == "http://100.107.121.5:8301/tts"
        assert kwargs["headers"] == {
            "Authorization": "Bearer voice-secret",
            "X-GPU-Lease-Token": "opaque-gpu-capability",
        }
        assert "gateway-secret" not in kwargs["headers"].values()
        return MagicMock(status_code=200, content=b"wav")

    client.post = AsyncMock(side_effect=post)
    monkeypatch.setenv("VOICE_URL", "http://100.107.121.5:8301/")
    with patch("app.llm_router.get_lm_gate", return_value=Gate()), patch(
        "app.voice_client.gpu_lease", lease
    ), patch(
        "app.helpers.voice_auth_headers",
        return_value={"Authorization": "Bearer voice-secret"},
    ):
        response = await post_leased_tts(
            client, {"text": "hello", "language": "en"}, timeout=30.0
        )

    assert response.status_code == 200
    assert events == [
        "gate-enter",
        "lease-enter",
        "tts-post",
        "lease-exit",
        "gate-exit",
    ]


def test_no_core_module_bypasses_the_leased_tts_client() -> None:
    app_root = Path(__file__).parents[1] / "app"
    allowed = app_root / "voice_client.py"
    offenders = []
    for source_path in app_root.rglob("*.py"):
        if source_path == allowed:
            continue
        source = source_path.read_text(encoding="utf-8")
        if 'f"{voice_url}/tts"' in source or 'f"{_voice_url()}/tts"' in source:
            offenders.append(source_path.name)

    assert offenders == []
