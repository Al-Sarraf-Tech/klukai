"""Tests for compaction helpers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestShouldCompact:
    def test_below_threshold_no_compact(self):
        from app.compaction import should_compact
        assert should_compact(message_count=50) is False

    def test_at_threshold_compacts(self):
        from app.compaction import should_compact
        assert should_compact(message_count=200) is True

    def test_above_threshold_compacts(self):
        from app.compaction import should_compact
        assert should_compact(message_count=500) is True

    def test_custom_threshold(self):
        from app.compaction import should_compact
        assert should_compact(message_count=30, threshold=25) is True


class TestSelectForCompaction:
    def test_short_history_no_compaction(self):
        from app.compaction import select_messages_for_compaction
        msgs = [{"role": "user", "content": f"m{i}"} for i in range(20)]
        to_compact, to_keep = select_messages_for_compaction(msgs, keep_recent=50)
        assert to_compact == []
        assert len(to_keep) == 20

    def test_long_history_splits_at_keep_recent(self):
        from app.compaction import select_messages_for_compaction
        msgs = [{"role": "user", "content": f"m{i}"} for i in range(100)]
        to_compact, to_keep = select_messages_for_compaction(msgs, keep_recent=30)
        assert len(to_compact) == 70
        assert len(to_keep) == 30
        assert to_keep[0]["content"] == "m70"
        assert to_keep[-1]["content"] == "m99"

    def test_exactly_keep_recent_no_compaction(self):
        from app.compaction import select_messages_for_compaction
        msgs = [{"role": "user", "content": f"m{i}"} for i in range(50)]
        to_compact, to_keep = select_messages_for_compaction(msgs, keep_recent=50)
        assert to_compact == []
        assert len(to_keep) == 50


class TestFormatForSummary:
    def test_formats_role_colon_content(self):
        from app.compaction import format_for_summary
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        out = format_for_summary(msgs)
        assert "user: hi" in out
        assert "assistant: hello" in out

    def test_truncates_per_message(self):
        from app.compaction import format_for_summary
        long_text = "a" * 1000
        out = format_for_summary([{"role": "user", "content": long_text}],
                                  limit_per_msg=50)
        # Output line contains truncated content
        assert "a" * 50 in out
        # And doesn't contain the whole long string
        assert "a" * 100 not in out

    def test_handles_missing_fields(self):
        from app.compaction import format_for_summary
        out = format_for_summary([{"role": None, "content": None}])
        # No crash; role defaults to '?', content to ''
        assert "?" in out

    def test_empty_list_returns_empty(self):
        from app.compaction import format_for_summary
        assert format_for_summary([]) == ""
