"""Fail-closed progress tracking for the standalone memory seeder."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import seed_memories


def _configure_run(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MagicMock, AsyncMock, datetime]:
    newest = datetime(2026, 8, 1, 12, 0, 0)
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(
        return_value=[
            ("user", "Keep this memory safe.", newest),
            ("assistant", "I will, Commander.", newest),
        ]
    )
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=cursor)
    conn.close = AsyncMock()

    monkeypatch.setattr(
        seed_memories.psycopg.AsyncConnection,
        "connect",
        AsyncMock(return_value=conn),
    )
    monkeypatch.setattr(
        seed_memories,
        "_get_last_seeded_at",
        AsyncMock(return_value=None),
    )
    set_watermark = AsyncMock()
    monkeypatch.setattr(seed_memories, "_set_last_seeded_at", set_watermark)
    monkeypatch.setattr(
        seed_memories,
        "free_comfyui_vram",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(seed_memories.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(seed_memories.sys, "argv", ["seed_memories.py"])
    return conn, set_watermark, newest


@pytest.mark.asyncio
async def test_selector_retry_failure_keeps_watermark_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, set_watermark, _ = _configure_run(monkeypatch)
    monkeypatch.setattr(
        seed_memories,
        "_select_batch",
        AsyncMock(side_effect=[RuntimeError("target down"), RuntimeError("still down")]),
    )

    result = await seed_memories.main()

    assert result == 1
    set_watermark.assert_not_awaited()
    conn.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_successful_zero_selection_advances_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, set_watermark, newest = _configure_run(monkeypatch)
    monkeypatch.setattr(
        seed_memories,
        "_select_batch",
        AsyncMock(return_value=[]),
    )

    result = await seed_memories.main()

    assert result == 0
    set_watermark.assert_awaited_once_with(conn, newest, "jalsarraf")
    conn.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("image_result", "save_result", "expected_save_calls"),
    [
        pytest.param(None, "unused", 0, id="image-failure"),
        pytest.param(b"png", None, 1, id="save-failure"),
    ],
)
async def test_image_or_save_failure_keeps_watermark_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    image_result: bytes | None,
    save_result: str | None,
    expected_save_calls: int,
) -> None:
    conn, set_watermark, _ = _configure_run(monkeypatch)

    def selected(batch: list[dict], _batch_start: int) -> list[dict]:
        return [
            {
                "exchange": batch[0],
                "global_index": 0,
                "category": "Precious Memories",
                "image_tags": ["home", "tender"],
            }
        ]

    async def select_batch(_client, batch: list[dict], batch_start: int) -> list[dict]:
        return selected(batch, batch_start)

    monkeypatch.setattr(seed_memories, "_select_batch", select_batch)
    monkeypatch.setattr(
        seed_memories,
        "_annotate",
        AsyncMock(return_value="I chose to keep this memory with the Commander."),
    )
    monkeypatch.setattr(
        seed_memories,
        "_is_duplicate",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        seed_memories,
        "build_prompt",
        MagicMock(return_value="bounded image prompt"),
    )
    monkeypatch.setattr(
        seed_memories,
        "generate_image",
        AsyncMock(return_value=image_result),
    )

    from app import db, memory_archive

    init_pool = AsyncMock()
    close_pool = AsyncMock()
    save_image = AsyncMock(return_value=save_result)
    monkeypatch.setattr(db, "init_pool", init_pool)
    monkeypatch.setattr(db, "close_pool", close_pool)
    monkeypatch.setattr(memory_archive, "save_image", save_image)

    result = await seed_memories.main()

    assert result == 1
    set_watermark.assert_not_awaited()
    assert save_image.await_count == expected_save_calls
    init_pool.assert_awaited_once_with(min_size=1, max_size=3)
    close_pool.assert_awaited_once()
    conn.close.assert_awaited_once()
