"""Fail-closed progress tracking for the standalone memory seeder."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import seed_memories

# A reply is always stored after the message it answers.
REPLY = timedelta(seconds=30)


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
            ("m1", "user", "Keep this memory safe.", newest),
            ("m2", "assistant", "I will, Commander.", newest + REPLY),
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
    monkeypatch.setattr(seed_memories, "_get_watermark", AsyncMock(return_value=None))
    set_watermark = AsyncMock()
    monkeypatch.setattr(seed_memories, "_set_watermark", set_watermark)
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
    set_watermark.assert_awaited_once_with(conn, newest + REPLY, None, "jalsarraf")
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
    fingerprint = "seed:m1"  # seed:<user message id>
    is_duplicate.assert_awaited_once_with(conn, "jalsarraf", fingerprint)
    assert save_image.await_args.args[2] == fingerprint
    assert save_image.await_args.kwargs["user_id"] == "jalsarraf"
    set_watermark.assert_awaited_once_with(conn, newest + REPLY, None, "jalsarraf")


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
    set_watermark.assert_awaited_once_with(conn, newest + REPLY, None, "jalsarraf")


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
        rows.append((f"u{n}", "user", f"message {n}", ts + timedelta(minutes=n)))
        rows.append((f"a{n}", "assistant", f"reply {n}", ts + timedelta(minutes=n) + REPLY))
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
    # The first batch answered (nothing chosen), so its five exchanges are
    # handled and the watermark moves past them; the second batch waits.
    last = seed_memories.BATCH_SIZE - 1
    set_watermark.assert_awaited_once_with(
        conn, ts + timedelta(minutes=last) + REPLY, None, "jalsarraf"
    )
    conn.close.assert_awaited_once()


class TestHardKill:
    """SEED_HARD_KILL_SECONDS arms SIGALRM: the distroless image has no
    coreutils `timeout`, so the in-container hard bound lives in the seeder."""

    def test_unset_arms_nothing(self, monkeypatch):
        import seed_memories

        monkeypatch.delenv("SEED_HARD_KILL_SECONDS", raising=False)
        alarm = []
        monkeypatch.setattr(seed_memories.signal, "alarm", alarm.append)
        assert seed_memories._arm_hard_kill() is None
        assert alarm == []

    @pytest.mark.parametrize("raw", ["abc", "0", "-5"])
    def test_invalid_or_non_positive_arms_nothing(self, monkeypatch, raw):
        import seed_memories

        monkeypatch.setenv("SEED_HARD_KILL_SECONDS", raw)
        alarm = []
        monkeypatch.setattr(seed_memories.signal, "alarm", alarm.append)
        assert seed_memories._arm_hard_kill() is None
        assert alarm == []

    def test_set_arms_sigalrm_with_the_default_action(self, monkeypatch):
        import seed_memories

        monkeypatch.setenv("SEED_HARD_KILL_SECONDS", "6600")
        alarm = []
        handlers = []
        monkeypatch.setattr(seed_memories.signal, "alarm", alarm.append)
        monkeypatch.setattr(
            seed_memories.signal, "signal", lambda sig, h: handlers.append((sig, h))
        )
        assert seed_memories._arm_hard_kill() == 6600
        assert alarm == [6600]
        assert handlers == [(seed_memories.signal.SIGALRM, seed_memories.signal.SIG_DFL)]


class TestProgress:
    """The watermark moves over the handled leading exchanges only."""

    def test_handled_prefix_stops_at_the_first_unfinished_exchange(self):
        assert seed_memories._handled_prefix(5, {0, 1, 2, 3, 4}, set(), set()) == 5
        assert seed_memories._handled_prefix(5, {0, 1, 2, 3, 4}, {1, 3}, {1}) == 3
        assert seed_memories._handled_prefix(5, {0, 1}, set(), set()) == 2
        assert seed_memories._handled_prefix(5, set(), set(), set()) == 0

    @pytest.mark.parametrize(("raw", "expected"), [("", 40), ("7", 7), ("abc", 40), ("0", 40)])
    def test_env_bound(self, monkeypatch, raw, expected):
        monkeypatch.setenv("SEED_MAX_EXCHANGES", raw)
        assert seed_memories._max_exchanges_from_env() == expected


@pytest.mark.asyncio
async def test_a_backlog_advances_one_bounded_prefix_per_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = []
    for h in range(6):
        ts = datetime(2026, 8, 1, h)
        rows += [(f"u{h}", "user", f"u{h}", ts), (f"a{h}", "assistant", f"a{h}", ts + REPLY)]
    conn, set_watermark, _ = _configure_run(monkeypatch, rows)
    monkeypatch.setenv("SEED_MAX_EXCHANGES", "2")
    select = AsyncMock(return_value=[])
    monkeypatch.setattr(seed_memories, "_select_batch", select)

    assert await seed_memories.main() == 0

    # Only the two oldest exchanges were offered, and the watermark moved to
    # the second one's assistant row, so the next run starts at the third.
    offered = select.await_args.args[1]
    assert [ex["user"] for ex in offered] == ["u0", "u1"]
    set_watermark.assert_awaited_once_with(
        conn, datetime(2026, 8, 1, 1) + REPLY, None, "jalsarraf"
    )


@pytest.mark.asyncio
async def test_a_failed_render_keeps_the_watermark_before_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exchanges before the first unfinished one are kept; it and everything
    after it are retried by the next run."""
    rows = []
    for h in range(3):
        ts = datetime(2026, 8, 1, h)
        rows += [(f"u{h}", "user", f"u{h}", ts), (f"a{h}", "assistant", f"a{h}", ts + REPLY)]
    conn, set_watermark, _ = _configure_run(monkeypatch, rows)

    async def pick_all(_client, batch, batch_start):
        return [
            {"exchange": ex, "global_index": batch_start + j, "category": "Quiet Hours",
             "image_tags": ["home"]}
            for j, ex in enumerate(batch)
        ]

    monkeypatch.setattr(seed_memories, "_select_batch", pick_all)
    monkeypatch.setattr(
        seed_memories, "_annotate", AsyncMock(return_value="A memory I mean to keep, Commander.")
    )
    monkeypatch.setattr(seed_memories, "_is_duplicate", AsyncMock(return_value=False))
    generate_image, save_image, _, _ = _patch_pass3(monkeypatch, b"png", "id")
    generate_image.side_effect = [b"png", None, b"png"]

    assert await seed_memories.main() == 1

    assert save_image.await_count == 2
    set_watermark.assert_awaited_once_with(
        conn, datetime(2026, 8, 1, 0) + REPLY, None, "jalsarraf"
    )


@pytest.mark.asyncio
async def test_a_repeated_pick_is_rendered_once(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, set_watermark, newest = _configure_run(monkeypatch)

    async def pick_twice(_client, batch, batch_start):
        return [
            {"exchange": batch[0], "global_index": batch_start, "image_tags": ["home"]},
            {"exchange": batch[0], "global_index": batch_start, "image_tags": ["home"]},
        ]

    monkeypatch.setattr(seed_memories, "_select_batch", pick_twice)
    monkeypatch.setattr(
        seed_memories, "_annotate", AsyncMock(return_value="A memory I mean to keep, Commander.")
    )
    monkeypatch.setattr(seed_memories, "_is_duplicate", AsyncMock(return_value=False))
    generate_image, save_image, _, _ = _patch_pass3(monkeypatch, b"png", "id")

    assert await seed_memories.main() == 0
    assert save_image.await_count == 1
    set_watermark.assert_awaited_once_with(conn, newest + REPLY, None, "jalsarraf")


@pytest.mark.asyncio
async def test_a_carried_message_is_read_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user message at the watermark's timestamp still waiting for its
    reply is carried: the next run reads everything after the timestamp
    plus that one message."""
    watermark = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    seen: dict[str, tuple] = {}

    async def execute(sql: str, params: tuple = ()) -> MagicMock:
        cursor = MagicMock()
        if "companion_relationship" in sql:
            value = (
                {"created_at": watermark.isoformat(), "carry": "u7"}
                if params and str(params[0]).startswith("last_seeded_carry:")
                else watermark.isoformat()
            )
            cursor.fetchone = AsyncMock(return_value=(value,))
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

    assert await seed_memories.main() == 0
    assert "(created_at > %s OR id = %s::uuid)" in seen["sql"]
    assert "ORDER BY created_at ASC, id ASC" in seen["sql"]
    assert seen["params"] == ("claude", watermark, "u7")


@pytest.mark.asyncio
async def test_a_stale_id_marker_falls_back_to_the_timestamp() -> None:
    """An older seeder may move last_seeded_at without the id key; an id whose
    timestamp no longer matches is ignored."""
    async def execute(sql: str, params: tuple = ()) -> MagicMock:
        cursor = MagicMock()
        if str(params[0]).startswith("last_seeded_carry:"):
            value = {"created_at": "2026-07-01T00:00:00+00:00", "carry": "old"}
        else:
            value = "2026-08-01T12:00:00+00:00"
        cursor.fetchone = AsyncMock(return_value=(value,))
        return cursor

    conn = MagicMock()
    conn.execute = AsyncMock(side_effect=execute)
    mark = await seed_memories._get_watermark(conn, "claude")
    assert mark == (datetime(2026, 8, 1, 12, tzinfo=timezone.utc), None)


@pytest.mark.asyncio
async def test_set_watermark_writes_both_keys_in_one_commit() -> None:
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.commit = AsyncMock()
    ts = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    await seed_memories._set_watermark(conn, ts, "u7", "claude")
    keys = [c.args[1][0] for c in conn.execute.await_args_list]
    assert keys == ["last_seeded_at:claude", "last_seeded_carry:claude"]
    assert '"carry": "u7"' in conn.execute.await_args_list[1].args[1][1]
    assert conn.execute.await_args_list[0].args[1][1] == '"2026-08-01T12:00:00+00:00"'
    assert conn.commit.await_count == 1


@pytest.mark.asyncio
async def test_slow_annotation_cannot_starve_the_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex's starvation case: 59 s selector batches and 119 s annotations
    against the 6,000 s budget. The phase budgets stop annotating at 60%, the
    annotated leading exchanges are rendered and saved, and the watermark moves
    over them instead of every run repeating the same work."""
    clock = {"now": 1_000.0}
    monkeypatch.setattr(seed_memories.time, "monotonic", lambda: clock["now"])
    rows = []
    for n in range(40):
        ts = datetime(2026, 8, 1, 0, n)
        rows += [(f"u{n}", "user", f"u{n}", ts), (f"a{n}", "assistant", f"a{n}", ts + REPLY)]
    conn, set_watermark, _ = _configure_run(monkeypatch, rows)
    monkeypatch.setenv("SEED_DEADLINE_SECONDS", "6000")

    async def pick_all(_client, batch, batch_start):
        clock["now"] += 59
        return [
            {"exchange": ex, "global_index": batch_start + j, "category": "Quiet Hours",
             "image_tags": ["home"]}
            for j, ex in enumerate(batch)
        ]

    async def slow_annotate(_client, _ex, _category):
        clock["now"] += 119
        return "A memory I mean to keep, Commander."

    monkeypatch.setattr(seed_memories, "_select_batch", pick_all)
    monkeypatch.setattr(seed_memories, "_annotate", slow_annotate)
    monkeypatch.setattr(seed_memories, "_is_duplicate", AsyncMock(return_value=False))
    generate_image, save_image, _, _ = _patch_pass3(monkeypatch, b"png", "id")

    async def render(_prompt):
        clock["now"] += 60
        return b"png"

    generate_image.side_effect = render

    assert await seed_memories.main() == 1  # incomplete: the rest waits

    saved = save_image.await_count
    assert saved > 0
    assert set_watermark.await_count == 1
    _, ts, carry, _ = set_watermark.await_args.args
    assert carry is None
    assert ts == datetime(2026, 8, 1, 0, saved - 1) + REPLY
    # Nothing past the annotation budget was rendered and thrown away.
    assert generate_image.await_count == saved
