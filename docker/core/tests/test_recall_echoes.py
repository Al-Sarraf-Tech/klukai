"""Recalled lines that repeat her last replies stay out of the turn."""

from app.personality.memory_blocks import drop_repeated_replies


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
