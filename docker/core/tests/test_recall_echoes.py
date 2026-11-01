"""Recalled lines that repeat her last replies stay out of the turn."""

from app.personality.memory_blocks import (
    drop_repeated_memories,
    drop_repeated_replies,
)


def test_a_near_copy_of_her_last_reply_is_dropped():
    recent = ["(I look away) The harbour was quiet, Commander."]
    exchanges = [
        {
            "user_content": "the harbour",
            "assistant_content": "(I look away) The harbour was quiet, Commander.",
        },
        {
            "user_content": "the rifle",
            "assistant_content": "The rifle stays by the door.",
        },
    ]
    kept = drop_repeated_replies(exchanges, recent)
    assert [item["user_content"] for item in kept] == ["the rifle"]


def test_a_memory_that_retells_an_older_reply_is_dropped():
    recent = ["The free day was ours, Commander. I kept the afternoon."] * 1
    memories = [
        "The free day was ours, Commander. I kept the afternoon.",
        "He asked about the rifle on the bench.",
        {"summary": "The free day was ours, Commander. I kept the afternoon."},
    ]
    kept = drop_repeated_memories(memories, recent)
    assert kept == ["He asked about the rifle on the bench."]
