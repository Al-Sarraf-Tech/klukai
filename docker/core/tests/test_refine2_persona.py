"""Short names still reach stored facts, and a told dream is not denied."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.chat_prompt import (
    ChatPromptBuilder,
    OutfitContext,
    already_told_a_dream,
    dream_inquiry_hint,
    short_turn_skips_recall,
)
from app.models import SessionState
from app.personality.system_prompt import PromptText

_DREAM = "I dreamed the hangar lights stayed on and you were still there."
_DENIAL = "I don't... dream"


def test_a_short_greeting_stays_cheap_and_a_name_does_not():
    for greeting in ("hello", "ok", "thanks", "Hi!", "Good morning"):
        assert short_turn_skips_recall(greeting) is True
    assert short_turn_skips_recall("Biscuit?") is False
    assert short_turn_skips_recall("Biscuit") is False
    assert short_turn_skips_recall("biscuit") is False
    assert short_turn_skips_recall("my birthday?") is False
    assert short_turn_skips_recall("what did you dream") is False
    assert short_turn_skips_recall("Tell me about the north ridge") is False


def test_a_told_dream_is_not_the_official_denial():
    assert already_told_a_dream([_DREAM], [], []) is True
    assert already_told_a_dream([], [_DREAM], []) is True
    assert (
        already_told_a_dream([], [], [{"assistant_content": _DREAM}]) is True
    )
    assert already_told_a_dream([], [], [], _DREAM) is True
    denial = "Dreams? I don't... dream. I'm a T-Doll."
    assert already_told_a_dream([denial], [], []) is False
    told = dream_inquiry_hint(already_told=True)
    assert "already told him one" in told
    assert _DENIAL not in told
    assert "DON'T remember your dreams" not in told
    unspoken = dream_inquiry_hint(already_told=False)
    assert _DENIAL in unspoken
    assert "If you already told him a dream" in unspoken
    assert "I'm a T-Doll" in unspoken


def _builder(monkeypatch):
    from app import context, decisions, picture_intent, rituals, tributes

    state = SimpleNamespace(
        level=3,
        score=300,
        first_interaction=None,
        last_interaction_date=None,
        consecutive_days=0,
    )
    memory = SimpleNamespace(
        recall_for_prompt=AsyncMock(return_value=([], {}, [])),
        get_memory_nudge=AsyncMock(return_value=None),
        get_inside_jokes=AsyncMock(return_value=[]),
    )
    captured: dict = {}

    def assemble(**kwargs):
        captured.update(kwargs)
        return PromptText("PERSONA\n\nCONTEXT", "PERSONA")

    builder = ChatPromptBuilder(
        memory=memory,
        affection=SimpleNamespace(get_state=AsyncMock(return_value=state)),
        proactive=SimpleNamespace(
            get_mission_timer=lambda user: None,
            check_anniversaries=AsyncMock(return_value=[]),
            get_comfort_objects=AsyncMock(return_value=[]),
        ),
        router=SimpleNamespace(is_game_active=AsyncMock(return_value=False)),
        ws=None,
        assemble_prompt=assemble,
        personality=dict,
        wants_thread=lambda text: False,
        needs_image=lambda text: False,
        hours_away=AsyncMock(return_value=2),
        warm_budget=1,
        turn_budget=1,
    )
    monkeypatch.setattr(
        builder,
        "wardrobe_turn",
        AsyncMock(return_value=OutfitContext(None, None, None, "")),
    )
    monkeypatch.setattr(
        context,
        "physical",
        SimpleNamespace(get_state=AsyncMock(return_value=("normal", ""))),
    )
    monkeypatch.setattr(tributes, "get_crown_jewel", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.night_watch.night_watch_prompt_block", AsyncMock(return_value="")
    )
    monkeypatch.setattr(decisions, "resolve_prompt_block", AsyncMock(return_value=""))
    monkeypatch.setattr(decisions, "her_world_prompt_block", AsyncMock(return_value=""))
    monkeypatch.setattr(rituals, "birthday_prompt_block", AsyncMock(return_value=""))
    monkeypatch.setattr("app.chat_prompt.pictures_available", lambda user: False)
    monkeypatch.setattr(picture_intent, "state_note", lambda user: "")
    monkeypatch.setattr(
        "app.conversation_continuity.load_recent_assistant_replies",
        AsyncMock(return_value=[]),
    )
    return builder, captured


async def test_biscuit_loads_the_stored_name_and_a_greeting_does_not(monkeypatch):
    builder, captured = _builder(monkeypatch)
    builder.memory.recall_for_prompt = AsyncMock(
        return_value=([], {"name": "Biscuit"}, [])
    )
    session = SessionState(conversation_id="test-conversation")

    prompt = await builder.build("Biscuit?", session, "alice")

    builder.memory.recall_for_prompt.assert_awaited()
    assert builder.memory.recall_for_prompt.await_args.args[0] == "Biscuit?"
    assert captured["relationship_facts"] == {"name": "Biscuit"}
    assert "I remember" not in prompt

    builder.memory.recall_for_prompt.reset_mock()
    captured.clear()
    await builder.build("hello", session, "alice")
    builder.memory.recall_for_prompt.assert_not_called()
    assert captured["relationship_facts"] == {}


async def test_a_delivered_dream_is_not_denied_in_the_same_prompt(monkeypatch):
    builder, captured = _builder(monkeypatch)
    builder.memory.recall_for_prompt = AsyncMock(return_value=([_DREAM], {}, []))
    session = SessionState(conversation_id="test-conversation")

    prompt = await builder.build("what did you dream", session, "alice")

    assert captured["memories"] == [_DREAM]
    assert "already told him one" in prompt
    assert _DENIAL not in prompt
    assert "DON'T remember your dreams" not in prompt

    builder.memory.recall_for_prompt = AsyncMock(return_value=([], {}, []))
    unspoken = await builder.build("did you dream about me", session, "alice")
    assert _DENIAL in unspoken
    assert "I'm a T-Doll" in unspoken
    assert "If you already told him a dream" in unspoken
