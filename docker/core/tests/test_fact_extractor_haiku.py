"""Per-turn extraction and compaction on Haiku, with local/default fallbacks."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app import extraction_llm as xl
from app import fact_extractor as fe
from app.circuit_breakers import get_breaker


class _Gate:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return None


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
    monkeypatch.setenv("KLUKAI_EXTRACTION_PROVIDER", "anthropic")
    complete = AsyncMock(return_value=json.dumps(GOOD))
    monkeypatch.setattr(xl, "complete", complete)
    local = AsyncMock(return_value={**GOOD, "mood": "content"})
    monkeypatch.setattr(fe, "call_llm", local)
    local_text = AsyncMock(return_value="Local summary.")
    monkeypatch.setattr(fe, "call_llm_text", local_text)
    breaker = get_breaker("lm_studio")
    breaker.force_close()
    with patch("app.llm_router.get_lm_gate", return_value=_Gate()):
        yield complete, local, local_text, breaker
    breaker.force_close()


# ── extraction ────────────────────────────────────────────────────────────


async def test_local_provider_path_is_unchanged(monkeypatch):
    monkeypatch.delenv("KLUKAI_EXTRACTION_PROVIDER", raising=False)
    complete = AsyncMock()
    monkeypatch.setattr(xl, "complete", complete)
    local = AsyncMock(return_value=GOOD)
    monkeypatch.setattr(fe, "call_llm", local)
    report = xl.CallReport()
    with patch("app.llm_router.get_lm_gate", return_value=_Gate()):
        out = await fe.extract_facts("hi", "hello", affection_level=3, report=report)
    complete.assert_not_awaited()
    local.assert_awaited_once()
    args, kwargs = local.await_args
    assert args[0] == fe.LM_STUDIO_URL and args[1] == fe.EXTRACTION_MODEL
    assert kwargs == {"max_tokens": 2048, "temperature": 0.1}
    assert out["mood"] == "amused"
    assert (report.provider, report.model, report.outcome) == (
        "local",
        fe.EXTRACTION_MODEL,
        "ok",
    )


async def test_haiku_answers_and_local_is_not_called(haiku):
    complete, local, _, _ = haiku
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
    local.assert_not_awaited()
    assert out["mood"] == "amused"
    assert out["interaction"] == {"type": "playful", "intensity": 7}
    assert out["facts"] == GOOD["facts"]
    assert (report.provider, report.model, report.outcome) == (
        "anthropic",
        "claude-haiku-5-5",
        "ok",
    )


async def test_haiku_json_in_a_code_fence_still_parses(haiku):
    complete, _, _, _ = haiku
    complete.return_value = "```json\n" + json.dumps(GOOD) + "\n```"
    out = await fe.extract_facts("hi", "hello")
    assert out["mood"] == "amused"


async def test_haiku_invalid_mood_still_defaults_to_composed(haiku):
    complete, _, _, _ = haiku
    complete.return_value = json.dumps({**GOOD, "mood": "rampaging"})
    out = await fe.extract_facts("hi", "hello")
    assert out["mood"] == "composed"


@pytest.mark.parametrize(
    ("effect", "reason"),
    [
        (xl.ExtractionUnavailable("cap"), "unavailable"),
        (xl.ExtractionLLMError("boom"), "failed"),
        (None, "unparsable"),
    ],
)
async def test_haiku_problem_falls_back_to_local_when_its_circuit_is_closed(
    haiku, effect, reason
):
    complete, local, _, _ = haiku
    if effect is None:
        complete.return_value = "I would rather not answer in JSON."
    else:
        complete.side_effect = effect
    report = xl.CallReport()
    out = await fe.extract_facts("hi", "hello", report=report)
    local.assert_awaited_once()
    assert out["mood"] == "content"
    assert report.provider == "local"
    assert report.model == fe.EXTRACTION_MODEL
    assert report.outcome == f"{reason}->local"


@pytest.mark.parametrize(
    "effect", [xl.ExtractionUnavailable("cap"), xl.ExtractionLLMError("boom")]
)
async def test_haiku_problem_with_local_circuit_open_uses_safe_defaults(haiku, effect):
    complete, local, _, breaker = haiku
    complete.side_effect = effect
    breaker.force_open()
    report = xl.CallReport()
    out = await fe.extract_facts("hi", "hello", report=report)
    local.assert_not_awaited()
    assert out == dict(fe._DEFAULT_RESULT)
    assert report.outcome.endswith("->defaults")


@pytest.mark.parametrize("local_result", [{}, ["not", "a", "dict"]])
async def test_local_fallback_returning_nothing_uses_defaults(haiku, local_result):
    complete, local, _, _ = haiku
    complete.side_effect = xl.ExtractionLLMError("boom")
    local.return_value = local_result
    report = xl.CallReport()
    out = await fe.extract_facts("hi", "hello", report=report)
    assert out == dict(fe._DEFAULT_RESULT)
    assert report.outcome == "failed->defaults"


@pytest.mark.parametrize("local_result", [{}, "a bare string"])
async def test_local_provider_empty_result_reports_defaults(monkeypatch, local_result):
    monkeypatch.delenv("KLUKAI_EXTRACTION_PROVIDER", raising=False)
    monkeypatch.setattr(fe, "call_llm", AsyncMock(return_value=local_result))
    report = xl.CallReport()
    with patch("app.llm_router.get_lm_gate", return_value=_Gate()):
        out = await fe.extract_facts("hi", "hello", report=report)
    assert out == dict(fe._DEFAULT_RESULT)
    assert report.outcome == "defaults"


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
    complete, _, local_text, _ = haiku
    complete.side_effect = None
    complete.return_value = "  The Commander greeted Klukai at dawn.  "
    out = await fe.compact_turns(TURNS)
    assert out == "The Commander greeted Klukai at dawn."
    local_text.assert_not_awaited()
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
    complete, _, _, _ = haiku
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


async def test_compaction_falls_back_to_local_when_circuit_closed(haiku):
    complete, _, local_text, _ = haiku
    complete.side_effect = xl.ExtractionLLMError("boom")
    assert await fe.compact_turns(TURNS) == "Local summary."
    local_text.assert_awaited_once()


async def test_compaction_empty_haiku_reply_falls_back(haiku):
    complete, _, local_text, _ = haiku
    complete.return_value = "   "
    assert await fe.compact_turns(TURNS) == "Local summary."


async def test_compaction_with_circuit_open_keeps_raw_turns(haiku):
    complete, _, local_text, breaker = haiku
    complete.side_effect = xl.ExtractionUnavailable("cap")
    breaker.force_open()
    assert await fe.compact_turns(TURNS) is None
    local_text.assert_not_awaited()


async def test_compaction_local_provider_unchanged(monkeypatch):
    monkeypatch.delenv("KLUKAI_EXTRACTION_PROVIDER", raising=False)
    complete = AsyncMock()
    monkeypatch.setattr(xl, "complete", complete)
    local_text = AsyncMock(return_value="Local summary.")
    monkeypatch.setattr(fe, "call_llm_text", local_text)
    with patch("app.llm_router.get_lm_gate", return_value=_Gate()):
        assert await fe.compact_turns(TURNS) == "Local summary."
    complete.assert_not_awaited()
    args, kwargs = local_text.await_args
    assert args[1] == fe.EXTRACTION_MODEL
    assert "Klukai: [Previous summary: They talked about coffee.]" in args[2]
    assert kwargs == {"max_tokens": 200, "temperature": 0.2}


async def test_compaction_needs_two_turns(haiku):
    complete, _, _, _ = haiku
    assert await fe.compact_turns(TURNS[:1]) is None
    complete.assert_not_awaited()
