"""Unit coverage for app.memory_recall — Postgres candidates + Claude Haiku pick.

Owner decision 2026-10-26: no embeddings. These tests pin the search
expression, the candidate query choice, the bounded selection prompt, the
validation of Haiku's ids (unknown dropped, order kept, deduped, capped) and
every "use the Postgres ranking" fallback (None, unusable reply, timeout,
unexpected error).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import extraction_llm, memory_recall
from app.memory_recall import (
    clip,
    fetch_candidates,
    ordered,
    parse_selection,
    rank_score,
    search_terms,
    select,
    selection_prompt,
)
from app.memory_settings import RECALL_CANDIDATES


# ── search_terms ─────────────────────────────────────────────────────────────


class TestSearchTerms:
    def test_distinct_lowercase_words_joined_by_or(self):
        assert search_terms("Remember the BEACH? The beach!") == "remember or the or beach"

    def test_punctuation_and_operators_never_reach_postgres(self):
        # Quotes, minus signs, colons and underscores are websearch/tsquery
        # syntax; only letters and digits survive.
        assert search_terms('"rain" -storm a:b foo_bar x\'s') == "rain or storm or foo or bar"

    def test_single_letters_and_the_word_or_are_dropped(self):
        assert search_terms("a or b or ok") == "ok"

    def test_unicode_words_and_digits_kept(self):
        assert search_terms("東京 café 3am") == "東京 or café or 3am"

    def test_no_searchable_word(self):
        assert search_terms("?! ... -- ''") == ""

    def test_term_count_and_length_are_bounded(self):
        words = " ".join(f"w{i}" for i in range(100))
        assert search_terms(words).count(" or ") == memory_recall.MAX_SEARCH_TERMS - 1
        assert search_terms("x" * 65 + " ok") == "ok"


# ── fetch_candidates ─────────────────────────────────────────────────────────


class _Conn:
    def __init__(self, rows):
        self.calls = []
        self._rows = rows

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))
        cursor = MagicMock()
        cursor.fetchall = AsyncMock(return_value=self._rows)
        return cursor


def _ctx(conn):
    @asynccontextmanager
    async def _get():
        yield conn

    return _get


class TestFetchCandidates:
    @pytest.mark.asyncio
    async def test_ranked_query_gets_terms_user_and_cap(self):
        conn = _Conn([("r1",)])
        with patch("app.db.get_conn", _ctx(conn)):
            rows = await fetch_candidates("RANKED", "RECENT", "the storm", "alice")
        assert rows == [("r1",)]
        assert conn.calls == [("RANKED", ("the or storm", "alice", RECALL_CANDIDATES))]
        assert RECALL_CANDIDATES == 40

    @pytest.mark.asyncio
    async def test_query_without_words_uses_importance_recency_query(self):
        conn = _Conn([])
        with patch("app.db.get_conn", _ctx(conn)):
            assert await fetch_candidates("RANKED", "RECENT", "?!", "bob") == []
        assert conn.calls == [("RECENT", ("bob", RECALL_CANDIDATES))]


# ── prompt ───────────────────────────────────────────────────────────────────


class TestSelectionPrompt:
    def test_lists_candidates_as_json_lines_with_numeric_ids(self):
        prompt = selection_prompt("past things", 'what "storm"?', ["first", "second"], 3)
        lines = [json.loads(line) for line in prompt.splitlines() if line.startswith('{"id"')]
        assert lines == [{"id": "1", "text": "first"}, {"id": "2", "text": "second"}]
        assert 'Search: "what \\"storm\\"?"' in prompt
        assert "at most 3 relevant memories" in prompt
        assert '{"ids": []}' in prompt
        assert prompt.startswith("Below are past things")

    def test_texts_and_query_are_truncated(self):
        prompt = selection_prompt("m", "Q" * 5000, ["T" * 5000], 1)
        assert "Q" * memory_recall.QUERY_CHARS not in prompt
        assert "Q" * (memory_recall.QUERY_CHARS - 1) in prompt
        assert "T" * memory_recall.TEXT_CHARS not in prompt
        assert "T" * (memory_recall.TEXT_CHARS - 1) in prompt

    def test_forty_full_candidates_fit_the_haiku_prompt_limit(self):
        texts = ["word " * 400 for _ in range(RECALL_CANDIDATES)]
        prompt = selection_prompt("memories", "q " * 2000, texts, 10)
        assert len(prompt) <= memory_recall.PROMPT_BUDGET_CHARS
        assert memory_recall.PROMPT_BUDGET_CHARS < extraction_llm.MAX_PROMPT_CHARS
        assert prompt.count('{"id"') == RECALL_CANDIDATES

    def test_escaping_heavy_text_drops_lowest_ranked_candidates_to_fit(self):
        texts = ['"\\' * 400 for _ in range(RECALL_CANDIDATES)]
        prompt = selection_prompt("memories", '"' * 2000, texts, 10)
        assert len(prompt) <= memory_recall.PROMPT_BUDGET_CHARS
        kept = [json.loads(line)["id"] for line in prompt.splitlines() if line.startswith('{"id"')]
        assert kept == [str(n) for n in range(1, len(kept) + 1)]  # a prefix, in rank order
        assert len(kept) < RECALL_CANDIDATES

    def test_clip_flattens_whitespace(self):
        assert clip("  a\n\n b\tc  ", 10) == "a b c"
        assert clip("abcdef", 4) == "abc…"


# ── parse_selection ──────────────────────────────────────────────────────────


class TestParseSelection:
    def test_valid_ids_in_haiku_order(self):
        assert parse_selection({"ids": ["3", "1", "2"]}, 5, 5) == [2, 0, 1]

    def test_unknown_ids_dropped(self):
        assert parse_selection({"ids": ["9", "2", "0", "x", "-1", "2.5", "1"]}, 3, 5) == [1, 0]

    def test_duplicates_dropped_and_order_kept(self):
        assert parse_selection({"ids": ["2", 2, " 2 ", "1"]}, 3, 5) == [1, 0]

    def test_capped_to_limit(self):
        assert parse_selection({"ids": ["1", "2", "3", "4"]}, 4, 2) == [0, 1]

    def test_integer_ids_accepted_booleans_rejected(self):
        assert parse_selection({"ids": [True, 2, False, 1]}, 3, 5) == [1, 0]

    def test_explicit_empty_list_means_nothing_relevant(self):
        assert parse_selection({"ids": []}, 3, 5) == []

    @pytest.mark.parametrize(
        "result",
        [None, "ids", ["1"], {}, {"ids": "1"}, {"ids": None}, {"other": ["1"]},
         {"ids": ["7", "nope"]}, {"ids": [None, 3.0]}],
    )
    def test_unusable_replies_mean_postgres_order(self, result):
        assert parse_selection(result, 3, 5) is None


# ── select ───────────────────────────────────────────────────────────────────


class TestSelect:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("reply,expected", [({"ids": ["40"]}, None), ({"ids": ["40", "2"]}, [1])])
    async def test_ids_trimmed_out_of_the_prompt_are_not_accepted(self, reply, expected):
        texts = ["\\" * memory_recall.TEXT_CHARS] * RECALL_CANDIDATES
        pick = AsyncMock(return_value=reply)
        with patch("app.background_llm.json_object", pick):
            out = await select("memory_recall_test", "things", "q", texts, 3)
        prompt = pick.await_args.args[1]
        shown = [json.loads(line)["id"] for line in prompt.splitlines() if line.startswith('{"id"')]
        assert "2" in shown and "40" not in shown
        assert out == expected

    @pytest.mark.asyncio
    async def test_calls_haiku_with_caller_bounded_tokens_and_returns_pick(self):
        pick = AsyncMock(return_value={"ids": ["2"]})
        with patch("app.background_llm.json_object", pick):
            out = await select("memory_recall_test", "things", "q", ["a", "b"], 3)
        assert out == [1]
        args, kwargs = pick.await_args
        assert args[0] == "memory_recall_test"
        assert kwargs == {
            "max_tokens": memory_recall.SELECTION_MAX_TOKENS,
            "role": "recall",
        }
        assert '{"id": "2", "text": "b"}' in args[1]

    @pytest.mark.asyncio
    async def test_haiku_none_means_postgres_order(self, caplog):
        with patch("app.background_llm.json_object", AsyncMock(return_value=None)), \
             caplog.at_level("WARNING", logger="app.memory_recall"):
            assert await select("c", "s", "q", ["a"], 1) is None
        assert not caplog.records  # background_llm already logged the outcome

    @pytest.mark.asyncio
    async def test_unusable_reply_is_logged_without_content(self, caplog):
        with patch("app.background_llm.json_object",
                   AsyncMock(return_value={"ids": ["secret text"]})), \
             caplog.at_level("WARNING", logger="app.memory_recall"):
            assert await select("c", "s", "q", ["private memory"], 1) is None
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "unusable selection" in messages
        assert "secret" not in messages and "private" not in messages

    @pytest.mark.asyncio
    async def test_slow_haiku_is_abandoned_at_the_deadline(self, caplog):
        started = asyncio.Event()

        async def hang(*_a, **_k):
            started.set()
            await asyncio.sleep(30)

        with patch("app.background_llm.json_object", hang), \
             patch.object(memory_recall, "RECALL_SELECTION_TIMEOUT_S", 0.01), \
             caplog.at_level("WARNING", logger="app.memory_recall"):
            assert await select("c", "s", "q", ["a"], 1) is None
        assert started.is_set()
        assert any("timed out" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_unexpected_error_falls_back(self):
        with patch("app.background_llm.json_object",
                   AsyncMock(side_effect=RuntimeError("boom"))):
            assert await select("c", "s", "q", ["a"], 1) is None

    @pytest.mark.asyncio
    async def test_cancellation_is_not_swallowed(self):
        with patch("app.background_llm.json_object",
                   AsyncMock(side_effect=asyncio.CancelledError())):
            with pytest.raises(asyncio.CancelledError):
                await select("c", "s", "q", ["a"], 1)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("texts,limit", [([], 3), (["a"], 0)])
    async def test_nothing_to_select_skips_haiku(self, texts, limit):
        pick = AsyncMock()
        with patch("app.background_llm.json_object", pick):
            assert await select("c", "s", "q", texts, limit) is None
        pick.assert_not_awaited()


# ── ordering helpers ─────────────────────────────────────────────────────────


def test_a_precomputed_pick_is_read_once():
    memory_recall.store_pinned("pin-user", ["  the harbour  ", ""])
    assert memory_recall.take_pinned("pin-user") == ["the harbour"]
    assert memory_recall.take_pinned("pin-user") == []
    memory_recall.store_pinned("pin-user", ["kept"])
    memory_recall.store_pinned("pin-user", ["", " "])
    assert memory_recall.take_pinned("pin-user") == []


@pytest.mark.asyncio
async def test_an_explicit_selection_timeout_is_forwarded():
    pick = AsyncMock(return_value={"ids": ["1"]})
    with patch("app.background_llm.json_object", pick):
        assert await select("c", "s", "q", ["a"], 1, timeout=4) == [0]
    assert pick.await_args.kwargs["timeout"] == 4
    assert pick.await_args.kwargs["role"] == "recall"


@pytest.mark.asyncio
async def test_pin_next_recall_stores_only_a_real_pick(monkeypatch):
    from app.memory import MemoryManager

    memory = MemoryManager.__new__(MemoryManager)

    async def fetch(*_args, **_kwargs):
        return [("id", "harbour log", [], [], 0.4, None)]

    async def picked(*_args, **kwargs):
        assert kwargs["timeout"] == memory_recall.RECALL_SELECTION_TIMEOUT_S or kwargs["timeout"] > 2
        return [0]

    monkeypatch.setattr(memory_recall, "fetch_candidates", fetch)
    monkeypatch.setattr(memory_recall, "select", picked)
    await memory.pin_next_recall("lighthouse", "pin-next")
    assert memory_recall.take_pinned("pin-next") == ["harbour log"]

    async def empty(*_args, **_kwargs):
        return []

    monkeypatch.setattr(memory_recall, "fetch_candidates", empty)
    await memory.pin_next_recall("lighthouse", "pin-next")
    assert memory_recall.take_pinned("pin-next") == []

    async def none_pick(*_args, **_kwargs):
        return None

    monkeypatch.setattr(memory_recall, "fetch_candidates", fetch)
    monkeypatch.setattr(memory_recall, "select", none_pick)
    await memory.pin_next_recall("lighthouse", "pin-next")
    assert memory_recall.take_pinned("pin-next") == []

    async def boom(*_args, **_kwargs):
        raise RuntimeError("db")

    monkeypatch.setattr(memory_recall, "fetch_candidates", boom)
    await memory.pin_next_recall("lighthouse", "pin-next")


@pytest.mark.asyncio
async def test_recall_budget_reaches_the_background_call(monkeypatch):
    from app import background_llm, extraction_llm

    seen = {}

    async def complete(prompt, **kwargs):
        seen.update(kwargs)
        return '{"ids": ["1"]}'

    monkeypatch.setattr(extraction_llm, "complete", complete)
    await background_llm.json_object(
        "memory_recall_episodes", "prompt", max_tokens=8, role="recall", timeout=12
    )
    assert seen["role"] == "recall"
    assert seen["timeout"] == 12


def test_ordered_follows_pick_or_postgres_prefix():
    rows = ["r0", "r1", "r2", "r3"]
    assert ordered(rows, [2, 0], 3) == ["r2", "r0"]
    assert ordered(rows, [], 3) == []
    assert ordered(rows, None, 3) == ["r0", "r1", "r2"]


def test_rank_score_first_is_one_and_strictly_decreasing():
    assert [rank_score(i, 4) for i in range(4)] == [1.0, 0.75, 0.5, 0.25]
    assert rank_score(0, 1) == 1.0
