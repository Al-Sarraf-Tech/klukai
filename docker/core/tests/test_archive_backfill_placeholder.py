"""The backfill captions placeholder entries too, not only NULL ones."""

from __future__ import annotations

from unittest.mock import patch

from .test_lm_gateway_auth import _client_with, _connection_context, _Gate, _RowsConnection


async def _backfill(rows, caption):
    import app.memory_archive as archive

    select_conn = _RowsConnection(rows)
    update_conn = _RowsConnection()
    with (
        patch("app.memory_archive.get_conn", side_effect=lambda: _connection_context(select_conn)),
        patch(
            "app.memory_archive.get_conn_autocommit",
            side_effect=lambda: _connection_context(update_conn),
        ),
        patch("app.memory_archive._get_http", return_value=_client_with(caption)),
        patch("app.llm_router.get_lm_gate", return_value=_Gate()),
    ):
        result = await archive.backfill_annotations("alice")
    return result, select_conn, update_conn


async def test_placeholder_captions_are_selected_for_this_user():
    from app.memory_archive import UNCAPTIONED

    result, select_conn, update_conn = await _backfill(
        [("mem-1", "his bedroom at night", "The Commander", [])], "He asked me to stay."
    )
    query, params = select_conn.calls[0]
    assert "annotation IS NULL OR annotation IN ('', %s)" in query
    assert params == (UNCAPTIONED, "alice")
    assert result == {"total": 1, "updated": 1}
    assert update_conn.calls[0][1] == ("He asked me to stay.", "mem-1")
