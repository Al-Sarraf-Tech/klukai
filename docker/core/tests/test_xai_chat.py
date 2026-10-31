"""xAI chat streaming, fallback, and the breaker."""

from __future__ import annotations

import json

import httpx
import pytest

from app import circuit_breakers as cb
from app.circuit_breakers import State, get_breaker
from app.llm_completions import LLMCompletionsMixin
from app.llm_router import FAILURE_SENTINEL, LLMStreamFailed
from app.xai_chat import (
    XaiAuthError,
    XaiCapError,
    XaiChatError,
    complete_text,
    content_delta,
    conversation_messages,
    iter_text,
    message_text,
    usage_counts,
)
from app.xai_spend import CHAT_MODEL, CHAT_URL


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    cb._BREAKERS.clear()
    monkeypatch.setenv("KLUKAI_XAI_LEDGER", str(tmp_path / "spend.sqlite3"))
    monkeypatch.setenv("KLUKAI_XAI_API_KEY", "unit-test-key")
    monkeypatch.setenv("KLUKAI_CHAT_BACKEND", "api")
    yield
    cb._BREAKERS.clear()


def _sse(*events: dict) -> str:
    lines = ["data: " + json.dumps(event) for event in events]
    lines.append("data: [DONE]")
    return "\n".join(lines) + "\n"


def _install(monkeypatch, responses: list[httpx.Response]):
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses.pop(0)

    monkeypatch.setattr(
        httpx, "AsyncHTTPTransport", lambda **_kwargs: httpx.MockTransport(respond)
    )
    return calls


def _ok() -> httpx.Response:
    return httpx.Response(
        200,
        text=_sse(
            {"choices": [{"delta": {"content": "Hi"}}]},
            {"choices": [{"delta": {"reasoning_content": "hidden"}}]},
            {"choices": [{"delta": {"content": " there"}}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "prompt_tokens_details": {"cached_tokens": 80},
                    "completion_tokens_details": {"reasoning_tokens": 4},
                },
            },
        ),
    )


async def test_a_stream_yields_visible_text_and_records_the_price(isolated, monkeypatch, caplog):
    calls = _install(monkeypatch, [_ok()])
    caplog.set_level("INFO")
    pieces = [piece async for piece in iter_text("persona", "hello")]
    assert pieces == ["Hi", " there"]
    assert "hidden" not in "".join(pieces)
    request = calls[0]
    assert str(request.url) == CHAT_URL
    body = json.loads(request.content)
    assert body["model"] == CHAT_MODEL
    assert body["reasoning_effort"] == "low"
    assert body["stream"] is True
    assert body["messages"][0] == {"role": "system", "content": "persona"}
    assert body["messages"][1]["role"] == "user"
    assert request.headers["authorization"] == "Bearer unit-test-key"
    assert "x-grok-conv-id" not in request.headers
    assert "unit-test-key" not in caplog.text
    assert "usd=0.000140" in caplog.text
    assert "cached=80" in caplog.text
    assert "reasoning=4" in caplog.text
    from app import xai_spend

    assert xai_spend.spent_today() == pytest.approx(0.00014)
    assert get_breaker("xai_chat").state is State.CLOSED


async def test_turn_state_is_a_later_message_than_the_persona(isolated, monkeypatch):
    calls = _install(monkeypatch, [_ok()])
    pieces = [
        piece
        async for piece in iter_text(
            "STATIC PERSONA", "hello", volatile="OPERATIONAL CONTEXT: 1727 hours"
        )
    ]
    assert pieces == ["Hi", " there"]
    body = json.loads(calls[0].content)
    assert body["messages"][0] == {"role": "system", "content": "STATIC PERSONA"}
    assert body["messages"][1] == {"role": "user", "content": "hello"}
    assert body["messages"][2] == {
        "role": "system",
        "content": "OPERATIONAL CONTEXT: 1727 hours",
    }


def test_history_drops_blanks_and_merges_a_repeated_role():
    built = conversation_messages(
        "PERSONA",
        [
            {"role": "system", "content": "no"},
            {"role": "user", "content": "  "},
            {"role": "user", "content": 5},
            {"role": "user", "content": "hello"},
            {"role": "user", "content": "again"},
        ],
    )
    assert built == [
        {"role": "system", "content": "PERSONA"},
        {"role": "user", "content": "hello\nagain"},
    ]
    empty = conversation_messages("PERSONA", [], "   ")
    assert empty[1] == {"role": "user", "content": "."}


def test_later_turns_keep_history_as_a_message_prefix():
    first = conversation_messages(
        "PERSONA",
        [{"role": "user", "content": "hello"}],
        "clock one",
    )
    second = conversation_messages(
        "PERSONA",
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Ready."},
            {"role": "user", "content": "again"},
        ],
        "clock two",
    )
    assert first[0] == second[0] == {"role": "system", "content": "PERSONA"}
    assert second[:4] == [
        {"role": "system", "content": "PERSONA"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Ready."},
        {"role": "user", "content": "again"},
    ]
    assert second[-1] == {"role": "system", "content": "clock two"}
    assert "clock" not in json.dumps(second[:-1])
    assert "prompt_cache_key" not in json.dumps(second)


async def test_a_conversation_header_and_api_ticks_set_the_charge(isolated, monkeypatch):
    body = _sse(
        {"choices": [{"delta": {"content": "Hi"}}]},
        {"choices": [{"delta": {"content": " there"}}]},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "cost_in_usd_ticks": 5_000_000_000,
            },
        },
    )
    calls = _install(monkeypatch, [httpx.Response(200, text=body)])
    pieces = [
        piece
        async for piece in iter_text("persona", "hello", conversation_id=" conv-7 ")
    ]
    assert pieces == ["Hi", " there"]
    assert calls[0].headers["x-grok-conv-id"] == "conv-7"
    from app import xai_spend

    assert xai_spend.spent_today() == pytest.approx(0.5)


async def test_a_streamed_reply_closes_a_half_open_breaker(isolated, monkeypatch):
    _install(monkeypatch, [_ok()])
    breaker = get_breaker("xai_chat")
    breaker.force_open()
    breaker.cfg.probe_interval_seconds = 0
    pieces = [piece async for piece in iter_text("persona", "hello")]
    assert pieces == ["Hi", " there"]
    assert breaker.state is State.CLOSED
    assert not breaker._half_open_lock.locked()


async def test_auth_failure_does_not_open_the_breaker(isolated, monkeypatch):
    calls = _install(monkeypatch, [httpx.Response(401) for _ in range(5)])
    for _ in range(5):
        with pytest.raises(XaiAuthError):
            async for _piece in iter_text("persona", "hello"):
                pass
    assert get_breaker("xai_chat").state is State.CLOSED
    assert len(calls) == 5


async def test_server_errors_open_the_breaker(isolated, monkeypatch):
    calls = _install(monkeypatch, [httpx.Response(502) for _ in range(5)])
    for _ in range(5):
        with pytest.raises(XaiChatError, match="failed"):
            async for _piece in iter_text("persona", "hello"):
                pass
    assert get_breaker("xai_chat").state is State.OPEN
    with pytest.raises(XaiChatError, match="unavailable"):
        async for _piece in iter_text("persona", "hello"):
            pass
    assert len(calls) == 5


async def test_the_daily_cap_skips_the_network(isolated, monkeypatch):
    monkeypatch.setenv("KLUKAI_API_DAILY_USD_CAP", "0")
    calls = _install(monkeypatch, [])
    with pytest.raises(XaiCapError):
        async for _piece in iter_text("persona", "hello"):
            pass
    assert calls == []


async def test_a_missing_key_does_not_call_the_network(isolated, monkeypatch):
    monkeypatch.delenv("KLUKAI_XAI_API_KEY")
    calls = _install(monkeypatch, [])
    with pytest.raises(XaiChatError, match="not configured"):
        async for _piece in iter_text("persona", "hello"):
            pass
    assert calls == []


async def test_malformed_json_is_an_error(isolated, monkeypatch):
    _install(monkeypatch, [httpx.Response(200, text="data: not-json\n")])
    with pytest.raises(XaiChatError, match="malformed"):
        async for _piece in iter_text("persona", "hello"):
            pass


async def test_router_uses_the_api_and_falls_back_only_before_text(isolated, monkeypatch):
    seen = {"cli": 0}

    async def api_ok(system, prompt, *, timeout=120.0, conversation_id=None, volatile="", turns=None):
        assert system == "persona"
        yield "Hi"

    async def cli(*_args, **_kwargs):
        seen["cli"] += 1
        yield "from cli"

    monkeypatch.setattr("app.xai_chat.iter_text", api_ok)
    monkeypatch.setattr("app.grok_chat.iter_text", cli)
    box = object()
    assert [
        piece
        async for piece in LLMCompletionsMixin._stream_grok(
            box, "persona", [{"role": "user", "content": "hello"}]
        )
    ] == ["Hi"]
    assert seen["cli"] == 0

    async def api_down(system, prompt, *, timeout=120.0, conversation_id=None, volatile="", turns=None):
        raise XaiChatError("boom")
        yield "no"  # pragma: no cover

    monkeypatch.setattr("app.xai_chat.iter_text", api_down)
    assert [
        piece
        async for piece in LLMCompletionsMixin._stream_grok(
            box, "persona", [{"role": "user", "content": [{"text": "hello"}]}]
        )
    ] == ["from cli"]
    assert seen["cli"] == 1

    async def api_partial(system, prompt, *, timeout=120.0, conversation_id=None, volatile="", turns=None):
        yield "Hi"
        raise XaiChatError("cut")

    monkeypatch.setattr("app.grok_chat.iter_text", cli)
    monkeypatch.setattr("app.xai_chat.iter_text", api_partial)
    with pytest.raises(LLMStreamFailed):
        _ = [
            piece
            async for piece in LLMCompletionsMixin._stream_grok(
                box, "persona", [{"role": "user", "content": "hello"}]
            )
        ]
    assert seen["cli"] == 1


async def test_an_explicit_cli_backend_skips_the_api(isolated, monkeypatch):
    monkeypatch.setenv("KLUKAI_CHAT_BACKEND", "cli")

    async def api(*_args, **_kwargs):
        raise AssertionError("api")
        yield "no"  # pragma: no cover

    async def cli(*_args, **_kwargs):
        yield "cli"

    monkeypatch.setattr("app.xai_chat.iter_text", api)
    monkeypatch.setattr("app.grok_chat.iter_text", cli)
    assert [
        piece
        async for piece in LLMCompletionsMixin._stream_grok(
            object(), "persona", [{"role": "user", "content": "hello"}]
        )
    ] == ["cli"]


def test_usage_and_delta_parsers_ignore_unusable_shapes():
    assert content_delta({"choices": [{"delta": "nope"}]}) is None
    assert content_delta({"choices": [{"delta": {"content": ""}}]}) is None
    assert usage_counts({"usage": []}) is None
    counted = usage_counts(
        {
            "usage": {
                "prompt_tokens": True,
                "completion_tokens": "4",
                "cached_tokens": 3,
                "reasoning_tokens": 2,
            }
        }
    )
    assert counted == (0, 3, 0, 2)
    counted = usage_counts(
        {
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": False},
                "reasoning_tokens": 1,
            }
        }
    )
    assert counted == (5, 0, 1, 1)


async def test_blank_and_non_object_events_and_a_body_cap(isolated, monkeypatch):
    monkeypatch.setattr("app.xai_chat._MAX_BODY", 30)
    _install(
        monkeypatch,
        [httpx.Response(200, text=": ping\ndata: [1]\ndata: " + ("x" * 40) + "\n")],
    )
    with pytest.raises(XaiChatError, match="malformed"):
        async for _piece in iter_text("persona", "hello"):
            pass
    _install(monkeypatch, [httpx.Response(200, text="data: " + ("y" * 80) + "\n")])
    with pytest.raises(XaiChatError, match="too large"):
        async for _piece in iter_text("persona", "hello"):
            pass


async def test_an_empty_200_and_a_transport_error(isolated, monkeypatch):
    _install(monkeypatch, [httpx.Response(200, text="")])
    with pytest.raises(XaiChatError, match="no text"):
        async for _piece in iter_text("persona", "hello"):
            pass
    _install(monkeypatch, [httpx.Response(200, text="data: [DONE]\n")])
    with pytest.raises(XaiChatError, match="no text"):
        async for _piece in iter_text("persona", "hello"):
            pass

    def boom(_request):
        raise httpx.ConnectError("unit-test-key")

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_kwargs: httpx.MockTransport(boom))
    with pytest.raises(XaiChatError, match="unavailable") as caught:
        async for _piece in iter_text("persona", "hello"):
            pass
    assert "unit-test-key" not in str(caught.value)


async def test_text_without_usage_still_logs_and_a_ledger_miss_does_not_drop_it(
    isolated, monkeypatch, caplog
):
    caplog.set_level("INFO")
    _install(
        monkeypatch,
        [httpx.Response(200, text=_sse({"choices": [{"delta": {"content": "Hi"}}]}))],
    )
    assert [piece async for piece in iter_text("persona", "hello")] == ["Hi"]
    assert "input=-" in caplog.text

    def locked(*_args, **_kwargs):
        import sqlite3

        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr("app.xai_chat.record", locked)
    caplog.clear()
    _install(monkeypatch, [_ok()])
    assert [piece async for piece in iter_text("persona", "hello")] == ["Hi", " there"]
    assert "daily_usd=-" in caplog.text


async def test_forbidden_is_authorization_not_an_outage(isolated, monkeypatch):
    _install(monkeypatch, [httpx.Response(403)])
    with pytest.raises(XaiAuthError):
        async for _piece in iter_text("persona", "hello"):
            pass
    assert get_breaker("xai_chat").state is State.CLOSED


def test_an_empty_cli_turn_is_the_failure_sentinel(isolated, monkeypatch):
    monkeypatch.setenv("KLUKAI_CHAT_BACKEND", "cli")

    async def cli(*_args, **_kwargs):
        if False:
            yield ""  # pragma: no cover

    monkeypatch.setattr("app.grok_chat.iter_text", cli)

    async def run():
        return [
            piece
            async for piece in LLMCompletionsMixin._stream_grok(
                object(), "persona", [{"role": "user", "content": ""}]
            )
        ]

    import asyncio

    assert asyncio.run(run()) == [FAILURE_SENTINEL]


def _story(text="Kept.", usage=None):
    body = {"choices": [{"message": {"role": "assistant", "content": text}}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


async def test_a_story_stays_on_grok_4_7_and_records_ticks(isolated, monkeypatch, caplog):
    monkeypatch.setenv("KLUKAI_CHAT_MODEL", "grok-4.20-0309-non-reasoning")
    calls = _install(
        monkeypatch,
        [
            _story(
                "  Kept.  ",
                {
                    "prompt_tokens": 20,
                    "completion_tokens": 4,
                    "prompt_tokens_details": {"cached_tokens": 8},
                    "completion_tokens_details": {"reasoning_tokens": 2},
                    "cost_in_usd_ticks": 20_000_000,
                },
            )
        ],
    )
    caplog.set_level("INFO")
    assert await complete_text("system", "prompt") == "Kept."
    body = json.loads(calls[0].content)
    assert body["model"] == CHAT_MODEL
    assert body["stream"] is False
    assert body["reasoning_effort"] == "low"
    assert body["max_completion_tokens"] == 8192
    assert calls[0].headers["authorization"] == "Bearer unit-test-key"
    assert "unit-test-key" not in caplog.text
    from app import xai_spend

    assert xai_spend.spent_today() == pytest.approx(0.002)


async def test_the_chat_model_env_selects_a_documented_slug(isolated, monkeypatch):
    monkeypatch.setenv("KLUKAI_CHAT_MODEL", "grok-4.3")
    calls = _install(monkeypatch, [_ok()])
    assert [piece async for piece in iter_text("persona", "hello")] == ["Hi", " there"]
    assert json.loads(calls[0].content)["model"] == "grok-4.3"


async def test_an_unknown_chat_model_stays_on_the_default(isolated, monkeypatch, caplog):
    monkeypatch.setenv("KLUKAI_CHAT_MODEL", "not-a-chat-model")
    calls = _install(monkeypatch, [_ok()])
    caplog.set_level("WARNING")
    assert [piece async for piece in iter_text("persona", "hello")]
    assert json.loads(calls[0].content)["model"] == CHAT_MODEL
    assert "not a documented chat model" in caplog.text


@pytest.mark.parametrize("budget", [0, 8193, "8"])
async def test_a_bad_story_budget_skips_the_network(isolated, monkeypatch, budget):
    calls = _install(monkeypatch, [])
    with pytest.raises(XaiChatError, match="budget"):
        await complete_text("s", "p", max_tokens=budget)
    assert calls == []


async def test_story_cap_and_missing_key_skip_the_network(isolated, monkeypatch):
    monkeypatch.setenv("KLUKAI_API_DAILY_USD_CAP", "0")
    calls = _install(monkeypatch, [])
    with pytest.raises(XaiCapError):
        await complete_text("s", "p")
    monkeypatch.delenv("KLUKAI_XAI_API_KEY")
    monkeypatch.delenv("KLUKAI_API_DAILY_USD_CAP")
    with pytest.raises(XaiChatError, match="not configured"):
        await complete_text("s", "p")
    assert calls == []


async def test_story_auth_failure_does_not_open_the_breaker(isolated, monkeypatch):
    _install(monkeypatch, [httpx.Response(401)])
    with pytest.raises(XaiAuthError):
        await complete_text("s", "p")
    assert get_breaker("xai_chat").state is State.CLOSED


async def test_story_server_errors_open_the_breaker(isolated, monkeypatch):
    calls = _install(monkeypatch, [httpx.Response(502) for _ in range(5)])
    for _ in range(5):
        with pytest.raises(XaiChatError, match="failed"):
            await complete_text("s", "p")
    assert get_breaker("xai_chat").state is State.OPEN
    with pytest.raises(XaiChatError, match="unavailable"):
        await complete_text("s", "p")
    assert len(calls) == 5


async def test_a_blank_story_is_still_charged(isolated, monkeypatch):
    _install(
        monkeypatch,
        [
            _story(
                "   ",
                {"prompt_tokens": 10, "completion_tokens": 1, "cost_in_usd_ticks": 10_000_000_000},
            )
        ],
    )
    with pytest.raises(XaiChatError, match="no text"):
        await complete_text("s", "p")
    from app import xai_spend

    assert xai_spend.spent_today() == pytest.approx(1.0)


async def test_story_malformed_error_and_oversize_bodies(isolated, monkeypatch):
    _install(monkeypatch, [httpx.Response(200, text="not-json")])
    with pytest.raises(XaiChatError, match="malformed"):
        await complete_text("s", "p")
    _install(monkeypatch, [httpx.Response(200, json=["nope"])])
    with pytest.raises(XaiChatError, match="malformed"):
        await complete_text("s", "p")
    _install(monkeypatch, [httpx.Response(200, json={"error": {"message": "no"}})])
    with pytest.raises(XaiChatError, match="malformed"):
        await complete_text("s", "p")
    monkeypatch.setattr("app.xai_chat._MAX_BODY", 8)
    _install(monkeypatch, [_story("this is longer than eight bytes")])
    with pytest.raises(XaiChatError, match="too large"):
        await complete_text("s", "p")


async def test_a_story_transport_error_hides_the_key(isolated, monkeypatch):
    def boom(_request):
        raise httpx.ConnectError("unit-test-key")

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_kwargs: httpx.MockTransport(boom))
    with pytest.raises(XaiChatError, match="unavailable") as caught:
        await complete_text("s", "p")
    assert "unit-test-key" not in str(caught.value)


async def test_a_story_without_usage_still_returns_and_a_ledger_miss_is_kept(
    isolated, monkeypatch, caplog
):
    caplog.set_level("INFO")
    _install(monkeypatch, [_story("Hi")])
    assert await complete_text("s", "p") == "Hi"
    assert "input=-" in caplog.text

    def locked(*_args, **_kwargs):
        import sqlite3

        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr("app.xai_chat.record", locked)
    caplog.clear()
    _install(monkeypatch, [_story("Hi", {"prompt_tokens": 3, "completion_tokens": 1})])
    assert await complete_text("s", "p") == "Hi"
    assert "daily_usd=-" in caplog.text


def test_message_text_ignores_unusable_shapes():
    assert message_text({}) == ""
    assert message_text({"choices": []}) == ""
    assert message_text({"choices": [{"message": "no"}]}) == ""
    assert message_text({"choices": [{"message": {"content": 5}}]}) == ""
    assert message_text({"choices": [{"message": {"content": "  hi  "}}]}) == "hi"
