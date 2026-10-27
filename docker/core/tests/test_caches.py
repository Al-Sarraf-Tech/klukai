"""Tests for app/caches.py — affection cache."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestAffectionCache:
    @pytest.mark.asyncio
    async def test_get_hit(self):
        from app.caches import get_affection
        snap = {"score": 500, "level": 5}
        fake = AsyncMock()
        fake.get = AsyncMock(return_value=json.dumps(snap))
        with patch("app.caches._get", return_value=fake):
            result = await get_affection("alice")
        assert result == snap

    @pytest.mark.asyncio
    async def test_get_miss(self):
        from app.caches import get_affection
        fake = AsyncMock()
        fake.get = AsyncMock(return_value=None)
        with patch("app.caches._get", return_value=fake):
            result = await get_affection("nobody")
        assert result is None

    @pytest.mark.asyncio
    async def test_put_short_ttl(self):
        from app.caches import put_affection, AFFECTION_TTL
        fake = AsyncMock()
        fake.set = AsyncMock()
        with patch("app.caches._get", return_value=fake):
            await put_affection("alice", {"score": 100})
        assert fake.set.call_args[1]["ex"] == AFFECTION_TTL

    @pytest.mark.asyncio
    async def test_invalidate_calls_delete(self):
        from app.caches import invalidate_affection
        fake = AsyncMock()
        fake.delete = AsyncMock()
        with patch("app.caches._get", return_value=fake):
            await invalidate_affection("alice")
        fake.delete.assert_awaited_once()
        assert "alice" in fake.delete.call_args[0][0]
