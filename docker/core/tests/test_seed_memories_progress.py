"""Fail-closed progress tracking for the standalone memory seeder."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import seed_memories


def _configure_run(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[tuple] | None = None,
) -> tuple[MagicMock, AsyncMock, datetime]:
    newest = datetime(2026, 8, 1, 12, 0, 0)
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(
        return_value=rows
        if rows is not None
        else [
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
    monkeypatch.delenv("SEED_DEADLINE_SECONDS", raising=False)
    return conn, set_watermark, newest


def _select_first(batch: list[dict], _batch_start: int) -> list[dict]:
    return [
        {
            "exchange": batch[0],
            "global_index": 0,
            "category": "Precious Memories",
            "image_tags": ["home", "tender"],
        }
    ]


async def _select_batch_first(_client, batch: list[dict], batch_start: int) -> list[dict]:
    return _select_first(batch, batch_start)


def _patch_pass3(
    monkeypatch: pytest.MonkeyPatch,
    image_result: bytes | None,
    save_result: str | None,
) -> tuple[AsyncMock, AsyncMock, AsyncMock, AsyncMock]:
    monkeypatch.setattr(
        seed_memories,
        "build_prompt",
        MagicMock(return_value="bounded image prompt"),
    )
    generate_image = AsyncMock(return_value=image_result)
    monkeypatch.setattr(seed_memories, "generate_image", generate_image)

    from app import db, memory_archive

    init_pool = AsyncMock()
    close_pool = AsyncMock()
    save_image = AsyncMock(return_value=save_result)
    monkeypatch.setattr(db, "init_pool", init_pool)
    monkeypatch.setattr(db, "close_pool", close_pool)
    monkeypatch.setattr(memory_archive, "save_image", save_image)
    return generate_image, save_image, init_pool, close_pool


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
    monkeypatch.setattr(seed_memories, "_select_batch", _select_batch_first)
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
    _, save_image, init_pool, close_pool = _patch_pass3(
        monkeypatch, image_result, save_result
    )

    result = await seed_memories.main()

    assert result == 1
    set_watermark.assert_not_awaited()
    assert save_image.await_count == expected_save_calls
    init_pool.assert_awaited_once_with(min_size=1, max_size=3)
    close_pool.assert_awaited_once()
    conn.close.assert_awaited_once()


# ── Watermark decoding (JSONB arrives already decoded) ──────────────────────


def _conn_returning(row: tuple | None) -> MagicMock:
    cursor = MagicMock()
    cursor.fetchone = AsyncMock(return_value=row)
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=cursor)
    return conn


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    [
        pytest.param("2026-08-01T12:00:00+00:00", id="jsonb-decoded-iso-string"),
        pytest.param('"2026-08-01T12:00:00+00:00"', id="legacy-text-encoded-json"),
    ],
)
async def test_last_seeded_at_decodes_both_stored_forms(stored: str) -> None:
    conn = _conn_returning((stored,))

    got = await seed_memories._get_last_seeded_at(conn, "jalsarraf")

    assert got == datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_last_seeded_at_is_none_when_unset_or_garbage() -> None:
    assert await seed_memories._get_last_seeded_at(_conn_returning(None)) is None
    assert await seed_memories._get_last_seeded_at(_conn_returning(("not a date",))) is None


@pytest.mark.asyncio
async def test_watermark_is_honoured_in_the_message_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored watermark must narrow the run, not silently restart history."""
    watermark = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    seen: dict[str, tuple] = {}

    async def execute(sql: str, params: tuple = ()) -> MagicMock:
        cursor = MagicMock()
        if "companion_relationship" in sql:
            # What psycopg returns for a JSONB string value: already decoded.
            cursor.fetchone = AsyncMock(return_value=(watermark.isoformat(),))
        else:
            seen["sql"], seen["params"] = sql, params
            cursor.fetchall = AsyncMock(return_value=[])
        return cursor

    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=execute)
    conn.close = AsyncMock()
    monkeypatch.setattr(
        seed_memories.psycopg.AsyncConnection, "connect", AsyncMock(return_value=conn)
    )
    monkeypatch.setattr(seed_memories.sys, "argv", ["seed_memories.py", "--user=claude"])
    monkeypatch.delenv("SEED_DEADLINE_SECONDS", raising=False)

    result = await seed_memories.main()

    assert result == 0
    assert "created_at > %s" in seen["sql"]
    assert seen["params"] == ("claude", watermark)


# ── Dedup by exchange fingerprint ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_check_is_exact_fingerprint_scoped_to_user() -> None:
    conn = _conn_returning((1,))

    assert await seed_memories._is_duplicate(conn, "claude", "seed:2026-08-01T12:00:00") is True

    sql, params = conn.execute.await_args.args
    assert "user_id = %s AND conversation_id = %s" in sql
    assert "LIKE" not in sql
    assert params == ("claude", "seed:2026-08-01T12:00:00")

    assert await seed_memories._is_duplicate(_conn_returning(None), "claude", "seed:x") is False


@pytest.mark.asyncio
async def test_saved_memory_carries_its_exchange_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, set_watermark, newest = _configure_run(monkeypatch)
    monkeypatch.setattr(seed_memories, "_select_batch", _select_batch_first)
    monkeypatch.setattr(
        seed_memories,
        "_annotate",
        AsyncMock(return_value="I chose to keep this memory with the Commander."),
    )
    is_duplicate = AsyncMock(return_value=False)
    monkeypatch.setattr(seed_memories, "_is_duplicate", is_duplicate)
    _, save_image, _, _ = _patch_pass3(monkeypatch, b"png", "0123456789abcdef")

    result = await seed_memories.main()

    assert result == 0
    fingerprint = f"seed:{newest.isoformat()}"
    is_duplicate.assert_awaited_once_with(conn, "jalsarraf", fingerprint)
    assert save_image.await_args.args[2] == fingerprint
    assert save_image.await_args.kwargs["user_id"] == "jalsarraf"
    set_watermark.assert_awaited_once_with(conn, newest, "jalsarraf")


@pytest.mark.asyncio
async def test_confirmed_duplicate_is_skipped_without_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, set_watermark, newest = _configure_run(monkeypatch)
    monkeypatch.setattr(seed_memories, "_select_batch", _select_batch_first)
    annotate = AsyncMock(return_value="unused")
    monkeypatch.setattr(seed_memories, "_annotate", annotate)
    monkeypatch.setattr(seed_memories, "_is_duplicate", AsyncMock(return_value=True))
    generate_image, save_image, _, _ = _patch_pass3(monkeypatch, b"png", "id")

    result = await seed_memories.main()

    assert result == 0
    annotate.assert_not_awaited()
    generate_image.assert_not_awaited()
    save_image.assert_not_awaited()
    set_watermark.assert_awaited_once_with(conn, newest, "jalsarraf")


# ── Unusable annotation fails closed ────────────────────────────────────────


@pytest.mark.asyncio
async def test_unusable_annotation_fails_closed_without_placeholder_or_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, set_watermark, _ = _configure_run(monkeypatch)
    monkeypatch.setattr(seed_memories, "_select_batch", _select_batch_first)
    monkeypatch.setattr(seed_memories, "_annotate", AsyncMock(return_value=None))
    monkeypatch.setattr(seed_memories, "_is_duplicate", AsyncMock(return_value=False))
    generate_image, save_image, _, _ = _patch_pass3(monkeypatch, b"png", "id")

    result = await seed_memories.main()

    assert result == 1
    generate_image.assert_not_awaited()
    save_image.assert_not_awaited()
    set_watermark.assert_not_awaited()
    conn.close.assert_awaited_once()


# ── Deadline ────────────────────────────────────────────────────────────────


def test_deadline_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEED_DEADLINE_SECONDS", raising=False)
    assert seed_memories._deadline_from_env() is None
    monkeypatch.setenv("SEED_DEADLINE_SECONDS", "soon")
    assert seed_memories._deadline_from_env() is None
    monkeypatch.setenv("SEED_DEADLINE_SECONDS", "0")
    assert seed_memories._deadline_from_env() is None
    monkeypatch.setenv("SEED_DEADLINE_SECONDS", "600")
    deadline = seed_memories._deadline_from_env()
    assert deadline is not None
    assert 590 < deadline - time.monotonic() <= 600
    assert seed_memories._past_deadline(None) is False
    assert seed_memories._past_deadline(time.monotonic() - 1) is True


@pytest.mark.asyncio
async def test_deadline_stops_the_run_and_keeps_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ts = datetime(2026, 8, 1, 12, 0, 0)
    rows = []
    for n in range(2 * seed_memories.BATCH_SIZE):  # two selector batches
        rows.append(("user", f"message {n}", ts))
        rows.append(("assistant", f"reply {n}", ts))
    conn, set_watermark, _ = _configure_run(monkeypatch, rows=rows)
    monkeypatch.setenv("SEED_DEADLINE_SECONDS", "0.2")

    async def slow_select(_client, _batch, _batch_start):
        time.sleep(0.25)  # the budget is spent inside the first batch
        return []

    select_batch = AsyncMock(side_effect=slow_select)
    monkeypatch.setattr(seed_memories, "_select_batch", select_batch)

    result = await seed_memories.main()

    assert result == 1
    assert select_batch.await_count == 1
    set_watermark.assert_not_awaited()
    conn.close.assert_awaited_once()
