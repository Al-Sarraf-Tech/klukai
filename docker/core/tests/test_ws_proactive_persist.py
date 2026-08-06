"""Proactive messages must land in chat history (princess-upgrade)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ws_manager import WSManager


@pytest.mark.asyncio
async def test_send_proactive_persists_to_messages():
    mgr = WSManager()
    mgr.send = AsyncMock()

    conn = AsyncMock()
    # SELECT conversation
    result_sel = AsyncMock()
    result_sel.fetchone = AsyncMock(return_value=("conv-uuid",))
    # INSERT message, then the turn_count bump
    result_ins = AsyncMock()
    result_turns = AsyncMock()
    conn.execute = AsyncMock(side_effect=[result_sel, result_ins, result_turns])
    conn.commit = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)

    with patch("app.db.get_conn", return_value=cm):
        await mgr.send_proactive("claude", "Morning, Commander.")

    mgr.send.assert_awaited_once()
    assert conn.execute.await_count == 3
    insert_sql = conn.execute.await_args_list[1].args[0]
    assert "companion_messages" in insert_sql
    assert "proactive" in insert_sql.lower() or conn.execute.await_args_list[1].args[1][0] == "conv-uuid"
    # turn_count must track the row count, as helpers.store_message does
    turns_sql = conn.execute.await_args_list[2].args[0]
    assert "turn_count = turn_count + 1" in turns_sql


@pytest.mark.asyncio
async def test_send_proactive_ws_succeeds_even_if_persist_fails():
    mgr = WSManager()
    mgr.send = AsyncMock()
    mgr._persist_proactive = AsyncMock(side_effect=RuntimeError("db down"))
    await mgr.send_proactive("claude", "Still here.")
    mgr.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_transient_toasts_are_not_written_to_history():
    """Chat history is SACRED and append-only.

    ``send_proactive`` doubles as the UX-toast channel ("voice link garbled",
    "rendering pipeline broke"). Persisting those made an infrastructure
    hiccup a permanent turn in history that was then replayed to the model
    as context on the next load — and it could never be cleaned up.
    """
    mgr = WSManager()
    mgr.send = AsyncMock()
    mgr._persist_proactive = AsyncMock()

    await mgr.send_proactive(
        "claude", "...Voice link garbled, Commander.", persist=False
    )

    mgr.send.assert_awaited_once()
    mgr._persist_proactive.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_check_ins_still_persist_by_default():
    mgr = WSManager()
    mgr.send = AsyncMock()
    mgr._persist_proactive = AsyncMock()

    await mgr.send_proactive("claude", "You've been quiet. Everything alright?")

    mgr._persist_proactive.assert_awaited_once()


@pytest.mark.asyncio
async def test_transient_toast_still_reaches_the_client():
    """Not persisting must not mean not delivering."""
    mgr = WSManager()
    mgr.send = AsyncMock()
    mgr._persist_proactive = AsyncMock()

    await mgr.send_proactive("claude", "...Voice synth dropped.", persist=False)

    frame = mgr.send.await_args.args[1]
    assert frame["type"] == "proactive"
    assert frame["message"] == "...Voice synth dropped."
