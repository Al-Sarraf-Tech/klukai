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
    # INSERT message
    result_ins = AsyncMock()
    conn.execute = AsyncMock(side_effect=[result_sel, result_ins])
    conn.commit = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)

    with patch("app.db.get_conn", return_value=cm):
        await mgr.send_proactive("claude", "Morning, Commander.")

    mgr.send.assert_awaited_once()
    assert conn.execute.await_count == 2
    insert_sql = conn.execute.await_args_list[1].args[0]
    assert "companion_messages" in insert_sql
    assert "proactive" in insert_sql.lower() or conn.execute.await_args_list[1].args[1][0] == "conv-uuid"


@pytest.mark.asyncio
async def test_send_proactive_ws_succeeds_even_if_persist_fails():
    mgr = WSManager()
    mgr.send = AsyncMock()
    mgr._persist_proactive = AsyncMock(side_effect=RuntimeError("db down"))
    await mgr.send_proactive("claude", "Still here.")
    mgr.send.assert_awaited_once()
