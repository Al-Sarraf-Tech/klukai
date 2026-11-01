"""Cold-session continuity: persisted lines lead a thread Redis no longer holds."""

from __future__ import annotations

from app.conversation_continuity import (
    CONTINUITY_CHARS,
    cached_history,
    merge_recent_replies,
    stitch_continuity,
)


def test_history_drops_a_chunk_instead_of_sliding_one_turn():
    turns = [{"role": "user", "content": str(i)} for i in range(17)]
    kept = cached_history(turns)
    assert kept[0]["content"] == "0"
    assert len(kept) == 17
    longer = [{"role": "user", "content": str(i)} for i in range(24)]
    dropped = cached_history(longer)
    assert dropped[0]["content"] == "8"
    assert dropped[-1]["content"] == "23"
    again = cached_history(dropped)
    assert [turn["content"] for turn in again] == [turn["content"] for turn in dropped]


def test_recent_replies_keep_the_newest_twenty_without_duplicates():
    session = [f"line {i}" for i in range(5)]
    stored = [f"line {i}" for i in range(3, 30)]
    merged = merge_recent_replies(session, stored, limit=20)
    assert merged[0] == "line 0"
    assert len(merged) == 20
    assert len(set(merged)) == 20


def test_warm_session_ignores_the_archive():
    live = [
        {"role": "user", "content": "the hangar"},
        {"role": "assistant", "content": "Clear."},
        {"role": "user", "content": "and now"},
    ]
    stitched = stitch_continuity(live, [{"role": "user", "content": "older"}])
    assert [turn["content"] for turn in stitched] == ["the hangar", "Clear.", "and now"]


def test_cold_session_keeps_the_persisted_thread_and_drops_the_duplicate():
    live = [{"role": "user", "content": "hello"}]
    earlier = [
        {"role": "user", "content": "the hangar"},
        {"role": "assistant", "content": "Clear."},
        {"role": "system", "content": "skip me"},
        {"role": "user", "content": "  hello  "},
    ]
    stitched = stitch_continuity(live, earlier)
    assert [(turn["role"], turn["content"]) for turn in stitched] == [
        ("user", "the hangar"),
        ("assistant", "Clear."),
        ("user", "hello"),
    ]


def test_long_persisted_line_is_clipped_and_the_live_line_is_not():
    live = [{"role": "user", "content": "x" * (CONTINUITY_CHARS + 20)}]
    earlier = [
        {"role": "assistant", "content": "y" * (CONTINUITY_CHARS + 20)},
        {"role": "user", "content": "different"},
    ]
    stitched = stitch_continuity(live, earlier)
    assert stitched[0]["content"].endswith("…")
    assert len(stitched[0]["content"]) <= CONTINUITY_CHARS
    assert stitched[-1]["content"] == "x" * (CONTINUITY_CHARS + 20)
