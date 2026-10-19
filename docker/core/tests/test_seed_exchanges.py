"""Pairing chat rows into seed exchanges, whatever order a timestamp tie sorts in."""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import permutations

import pytest

from app.seed_exchanges import SEED_FINGERPRINT_PREFIX, fingerprint, pair_exchanges

T0 = datetime(2026, 8, 1, 12, 0, 0)


def _t(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _sorted(rows):
    """What the query returns: (created_at, id) order. Ids stand in for
    random UUIDs, so tie order is whatever the ids say."""
    return sorted(rows, key=lambda r: (r[3], r[0]))


def _pairs(rows):
    return [(e["user"], e["assistant"]) for e in pair_exchanges(_sorted(rows))]


class TestTiesNeverMispair:
    @pytest.mark.parametrize(("a0_id", "u1_id"), [("b", "c"), ("c", "b")])
    def test_codex_case_in_either_tie_order(self, a0_id, u1_id):
        """u0@0, a0@1, u1@1, a1@2: a0 answers u0 and a1 answers u1, however
        the tie between a0 and u1 happens to sort."""
        rows = [
            ("a", "user", "u0", _t(0)),
            (a0_id, "assistant", "a0", _t(1)),
            (u1_id, "user", "u1", _t(1)),
            ("d", "assistant", "a1", _t(2)),
        ]
        assert _pairs(rows) == [("u0", "a0"), ("u1", "a1")]

    def test_a_reply_never_answers_a_question_at_its_own_timestamp(self):
        rows = [("a", "user", "q", _t(0)), ("b", "assistant", "r", _t(0))]
        assert _pairs(rows) == []

    def test_every_order_of_a_tied_chain_pairs_the_same(self):
        rows = [
            ("u0", "user", "u0", _t(0)),
            ("a0", "assistant", "a0", _t(1)),
            ("u1", "user", "u1", _t(1)),
            ("a1", "assistant", "a1", _t(2)),
            ("u2", "user", "u2", _t(2)),
            ("a2", "assistant", "a2", _t(3)),
        ]
        expected = [("u0", "a0"), ("u1", "a1"), ("u2", "a2")]
        ids = [r[0] for r in rows]
        for order in permutations(range(len(rows))):
            renamed = [(f"{order[i]:02d}", r[1], r[2], r[3]) for i, r in enumerate(rows)]
            assert _pairs(renamed) == expected, (order, ids)


class TestPassOvers:
    def test_a_reply_with_no_question_waiting_is_passed_over(self):
        rows = [
            ("a", "assistant", "orphan", _t(0)),
            ("b", "user", "q", _t(1)),
            ("c", "assistant", "r", _t(2)),
        ]
        assert _pairs(rows) == [("q", "r")]

    def test_a_question_followed_by_another_is_left_unanswered(self):
        rows = [
            ("a", "user", "first", _t(0)),
            ("b", "user", "second", _t(1)),
            ("c", "assistant", "reply", _t(2)),
        ]
        assert _pairs(rows) == [("second", "reply")]

    def test_extra_replies_in_a_group_answer_once(self):
        rows = [
            ("a", "user", "q", _t(0)),
            ("b", "assistant", "r1", _t(1)),
            ("c", "assistant", "r2", _t(1)),
        ]
        assert _pairs(rows) == [("q", "r1")]

    def test_system_rows_are_ignored(self):
        rows = [
            ("a", "user", "q", _t(0)),
            ("b", "system", "note", _t(1)),
            ("c", "assistant", "r", _t(2)),
        ]
        assert _pairs(rows) == [("q", "r")]

    def test_empty_content_and_length_bound(self):
        rows = [("a", "user", None, _t(0)), ("b", "assistant", "x" * 900, _t(1))]
        [ex] = pair_exchanges(_sorted(rows))
        assert ex["user"] == ""
        assert len(ex["assistant"]) == 500


class TestFingerprintsAndMarks:
    def test_fingerprint_is_the_user_message_id(self):
        assert fingerprint("u-42") == f"{SEED_FINGERPRINT_PREFIX}u-42"
        rows = [("u-42", "user", "q", _t(0)), ("r-1", "assistant", "r", _t(1))]
        [ex] = pair_exchanges(rows)
        assert ex["fingerprint"] == "seed:u-42"
        assert ex["created_at"] == _t(0).isoformat()

    def test_tied_exchanges_get_distinct_fingerprints(self):
        """Exchanges whose questions share a timestamp are not duplicates."""
        rows = []
        for n in range(41):
            rows += [(f"q{n:02d}", "user", f"q{n}", _t(0)), (f"r{n:02d}", "assistant", "r", _t(1 + n))]
        exchanges = pair_exchanges(_sorted(rows))
        assert len({e["fingerprint"] for e in exchanges}) == len(exchanges)

    def test_mark_carries_the_question_waiting_at_the_reply_timestamp(self):
        rows = [
            ("a", "user", "u0", _t(0)),
            ("b", "assistant", "a0", _t(1)),
            ("c", "user", "u1", _t(1)),
            ("d", "assistant", "a1", _t(2)),
        ]
        first, second = pair_exchanges(_sorted(rows))
        assert first["mark"] == (_t(1), "c")
        assert second["mark"] == (_t(2), None)


class TestRunsResumeExactly:
    """Replay successive runs that take one exchange each, reading what a
    carry watermark lets through, as the seeder's query does."""

    @staticmethod
    def _run_all(rows):
        mark = None
        seen = []
        for _ in range(10):
            if mark is None:
                visible = rows
            else:
                ts, carry = mark
                visible = [r for r in rows if r[3] > ts or r[0] == carry]
            exchanges = pair_exchanges(_sorted(visible))[:1]
            if not exchanges:
                break
            seen.append((exchanges[0]["user"], exchanges[0]["assistant"]))
            mark = exchanges[0]["mark"]
        return seen

    @pytest.mark.parametrize(("a0_id", "u1_id"), [("b", "c"), ("c", "b")])
    def test_one_exchange_per_run_neither_skips_nor_repeats(self, a0_id, u1_id):
        rows = [
            ("a", "user", "u0", _t(0)),
            (a0_id, "assistant", "a0", _t(1)),
            (u1_id, "user", "u1", _t(1)),
            ("d", "assistant", "a1", _t(2)),
            ("e", "user", "u2", _t(3)),
            ("f", "assistant", "a2", _t(4)),
        ]
        assert self._run_all(rows) == [("u0", "a0"), ("u1", "a1"), ("u2", "a2")]
