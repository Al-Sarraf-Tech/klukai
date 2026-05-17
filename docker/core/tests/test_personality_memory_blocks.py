"""Tests for the personality memory blocks (memory, relationship, conversation recall).

Currently at 53% coverage in the suite — these tests bring it to 100%.
"""

from __future__ import annotations

from app.personality.memory_blocks import (
    build_conversation_recall_block,
    build_memory_block,
    build_relationship_block,
)


class TestMemoryBlock:
    def test_empty_returns_empty_string(self):
        assert build_memory_block([]) == ""

    def test_single_memory(self):
        out = build_memory_block(["Commander likes coffee"])
        assert "OPERATIONAL RECORDS" in out
        assert "Commander likes coffee" in out

    def test_multiple_memories_bulleted(self):
        out = build_memory_block(["first", "second", "third"])
        assert "  - first" in out
        assert "  - second" in out
        assert "  - third" in out

    def test_header_text_intact(self):
        out = build_memory_block(["x"])
        assert "relevant past interactions with the Commander" in out


class TestRelationshipBlock:
    def test_empty_dict_returns_empty(self):
        assert build_relationship_block({}) == ""

    def test_single_fact(self):
        out = build_relationship_block({"birthday": "March 5"})
        assert "COMMANDER DOSSIER" in out
        assert "birthday: March 5" in out

    def test_multiple_facts_each_on_own_line(self):
        out = build_relationship_block({"a": "1", "b": "2"})
        assert "  - a: 1" in out
        assert "  - b: 2" in out


class TestConversationRecallBlock:
    def test_empty_returns_empty(self):
        assert build_conversation_recall_block([]) == ""

    def test_formats_single_exchange(self):
        ex = [{"user_content": "Hi", "assistant_content": "Welcome back."}]
        out = build_conversation_recall_block(ex)
        assert "RECALLED CONVERSATIONS" in out
        assert "Commander: Hi" in out
        assert "Klukai: Welcome back." in out
        assert "[1]" in out

    def test_truncates_long_content_at_200_chars(self):
        long_user = "a" * 250
        long_klukai = "b" * 250
        ex = [{"user_content": long_user, "assistant_content": long_klukai}]
        out = build_conversation_recall_block(ex)
        # Each truncated to first 200 chars
        assert "a" * 200 in out
        assert "a" * 201 not in out
        assert "b" * 200 in out
        assert "b" * 201 not in out

    def test_multiple_exchanges_numbered(self):
        ex = [
            {"user_content": "first u", "assistant_content": "first k"},
            {"user_content": "second u", "assistant_content": "second k"},
        ]
        out = build_conversation_recall_block(ex)
        assert "[1]" in out
        assert "[2]" in out
        assert "first u" in out
        assert "second u" in out
