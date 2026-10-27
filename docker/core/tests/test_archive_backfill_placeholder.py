"""The backfill captions placeholder entries too, not only NULL ones."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from .test_memory_archive_coverage import _conn_ctx, _RecordingConn


async def _backfill(rows, caption):
    import app.memory_archive as archive

    select_conn = _RecordingConn(rows=rows)
    update_conn = _RecordingConn()
    with (
        patch(
            "app.memory_archive.get_conn", side_effect=lambda: _conn_ctx(select_conn)()
        ),
        patch(
            "app.memory_archive.get_conn_autocommit",
            side_effect=lambda: _conn_ctx(update_conn)(),
        ),
        patch("app.extraction_llm.complete", AsyncMock(return_value=caption)),
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
