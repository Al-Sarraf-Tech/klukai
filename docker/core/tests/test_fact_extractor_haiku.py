"""Per-turn extraction and compaction on Haiku, with safe defaults on failure."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app import extraction_llm as xl
from app import fact_extractor as fe


GOOD = {
    "mood": "amused",
    "interaction": {"type": "playful", "intensity": 7},
    "facts": [{"key": "favorite_drink", "value": "black coffee"}],
    "topics": ["coffee"],
    "should_remember": False,
    "commander_details": {},
    "gift_item": None,
    "inside_joke": None,
}


@pytest.fixture
def haiku(monkeypatch):
    """Haiku mocked; every other HTTP request is recorded (there must be none)."""
    monkeypatch.delenv("KLUKAI_EXTRACTION_MODEL", raising=False)
    complete = AsyncMock(return_value=json.dumps(GOOD))
    monkeypatch.setattr(xl, "complete", complete)
    sent: list[httpx.Request] = []

    async def send(self, request, *args, **kwargs):
        sent.append(request)
        raise httpx.ConnectError("no network in unit tests", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    yield complete
    assert sent == [], [str(r.url) for r in sent]


def test_no_local_extraction_route_remains():
    for name in ("_local_extraction", "LM_STUDIO_URL", "EXTRACTION_MODEL", "call_llm"):
        assert not hasattr(fe, name), name


# ── extraction ────────────────────────────────────────────────────────────


async def test_haiku_answers(haiku):
    complete = haiku
    report = xl.CallReport()
    out = await fe.extract_facts("hi", "hello", affection_level=6, report=report)
    complete.assert_awaited_once()
    prompt = complete.await_args.args[0]
    assert "Commander: hi" in prompt and "Klukai: hello" in prompt
    assert "level 6/9" in prompt
    assert complete.await_args.kwargs == {
        "max_tokens": xl.EXTRACTION_MAX_TOKENS,
        "route": "extraction",
    }
    assert out["mood"] == "amused"
    assert out["interaction"] == {"type": "playful", "intensity": 7}
    assert out["facts"] == GOOD["facts"]
    assert (report.provider, report.model, report.outcome) == (
        "grok",
        "grok-4.7",
        "ok",
    )


async def test_haiku_json_in_a_code_fence_still_parses(haiku):
    complete = haiku
    complete.return_value = "```json\n" + json.dumps(GOOD) + "\n```"
    out = await fe.extract_facts("hi", "hello")
    assert out["mood"] == "amused"


async def test_haiku_invalid_mood_still_defaults_to_composed(haiku):
    complete = haiku
    complete.return_value = json.dumps({**GOOD, "mood": "rampaging"})
    out = await fe.extract_facts("hi", "hello")
    assert out["mood"] == "composed"


@pytest.mark.parametrize("mood", [["tender", "protective"], {"primary": "tender"}])
async def test_haiku_mood_that_is_not_a_string_defaults_to_composed(haiku, mood):
    """An unhashable mood used to raise out of extract_facts, losing the whole
    turn: mood, heart rate, affection, facts and the exchange record."""
    complete = haiku
    complete.return_value = json.dumps({**GOOD, "mood": mood})
    out = await fe.extract_facts("hi", "hello")
    assert out["mood"] == "composed"
    assert out["facts"] == GOOD["facts"]


@pytest.mark.parametrize(
    ("effect", "reason"),
    [
        (xl.ExtractionUnavailable("cap"), "unavailable"),
        (xl.ExtractionLLMError("boom"), "failed"),
        (RuntimeError("unexpected"), "failed"),
        ("I would rather not answer in JSON.", "unparsable"),
        ("{}", "unparsable"),
        ('["not", "a", "dict"]', "unparsable"),
    ],
)
async def test_haiku_problem_uses_safe_defaults_without_a_local_request(
    haiku, effect, reason
):
    complete = haiku
    if isinstance(effect, Exception):
        complete.side_effect = effect
    else:
        complete.return_value = effect
    report = xl.CallReport()
    out = await fe.extract_facts("hi", "hello", report=report)
    complete.assert_awaited_once()
    assert out == dict(fe._DEFAULT_RESULT)
    assert (report.provider, report.model, report.outcome) == (
        "grok",
        "grok-4.7",
        f"{reason}->defaults",
    )


async def test_report_is_optional(haiku):
    out = await fe.extract_facts("hi", "hello")
    assert out["mood"] == "amused"


async def test_prompt_lists_every_mood_and_keeps_the_distress_rule():
    from app.models import Mood

    for mood in Mood:
        assert mood.value in fe.EXTRACTION_PROMPT, mood.value
    assert "Distress is not hostility" in fe.EXTRACTION_PROMPT
    for forbidden in ("sulking", "stern", "heartbroken", "irritated"):
        assert forbidden in fe.EXTRACTION_PROMPT.split("Distress is not hostility")[1]
    # Still a valid format template.
    fe.EXTRACTION_PROMPT.format(
        user_message="u", assistant_message="a", affection_level=1
    )


def test_valid_moods_are_exactly_the_enum():
    from app.models import Mood

    assert fe.VALID_MOODS == {m.value for m in Mood}
    assert len(fe.VALID_MOODS) == 65


# ── compaction ────────────────────────────────────────────────────────────

TURNS = [
    {"role": "system", "content": "[Previous summary: They talked about coffee.]"},
    {"role": "user", "content": "Morning."},
    {"role": "assistant", "content": "Good morning, Commander."},
]


async def test_compaction_on_haiku(haiku):
    complete = haiku
    complete.return_value = "  The Commander greeted Klukai at dawn.  "
    out = await fe.compact_turns(TURNS)
    assert out == "The Commander greeted Klukai at dawn."
    prompt = complete.await_args.args[0]
    assert "Earlier summary: [Previous summary: They talked about coffee.]" in prompt
    assert (
        "Commander: Morning." in prompt and "Klukai: Good morning, Commander." in prompt
    )
    assert complete.await_args.kwargs == {
        "max_tokens": xl.COMPACTION_MAX_TOKENS,
        "route": "compaction",
    }


async def test_compaction_input_and_output_are_bounded(haiku):
    complete = haiku
    complete.return_value = "s" * 5000
    turns = [{"role": "system", "content": "p" * 9000}] + [
        {"role": "user" if i % 2 else "assistant", "content": f"{i:03d}" + "x" * 900}
        for i in range(60)
    ]
    out = await fe.compact_turns(turns)
    assert len(out) == fe.MAX_COMPACT_SUMMARY_CHARS
    prompt = complete.await_args.args[0]
    assert len(prompt) < xl.MAX_PROMPT_CHARS
    assert "p" * fe.MAX_PRIOR_SUMMARY_CHARS in prompt
    assert "p" * (fe.MAX_PRIOR_SUMMARY_CHARS + 1) not in prompt
    assert "059" in prompt and "000" not in prompt  # oldest turns dropped first


@pytest.mark.parametrize(
    "effect",
    [
        xl.ExtractionUnavailable("cap"),
        xl.ExtractionLLMError("boom"),
        RuntimeError("unexpected"),
        "",
        "   ",
    ],
)
async def test_compaction_without_haiku_keeps_the_raw_turns(haiku, effect):
    complete = haiku
    if isinstance(effect, Exception):
        complete.side_effect = effect
    else:
        complete.return_value = effect
    assert await fe.compact_turns(TURNS) is None
    complete.assert_awaited_once()


async def test_compaction_needs_two_turns(haiku):
    complete = haiku
    assert await fe.compact_turns(TURNS[:1]) is None
    complete.assert_not_awaited()
