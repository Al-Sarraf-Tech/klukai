"""Chat stays fast: low effort, parallel nudge, requeued extraction, streamed text."""

import asyncio

import pytest

from app.chat_followups import ChatFollowups
from app.chat_prompt import ChatPromptBuilder
from app.extraction_llm import ExtractionCancelled
from app.grok_chat import EFFORT, background_effort, chat_effort
from app.memory_settings import RECALL_SELECTION_TIMEOUT_S


def test_effort_defaults_are_low_for_chat_and_background(monkeypatch):
    assert chat_effort() == EFFORT == "low"
    assert background_effort() == "low"
    assert RECALL_SELECTION_TIMEOUT_S <= 3
    monkeypatch.setenv("KLUKAI_GROK_CHAT_EFFORT", "minimal")
    monkeypatch.setenv("KLUKAI_GROK_BACKGROUND_EFFORT", "medium")
    assert chat_effort() == "minimal"
    assert background_effort() == "medium"


async def test_nudge_recall_runs_during_the_main_recall():
    started = asyncio.Event()
    release = asyncio.Event()

    class Memory:
        async def get_memory_nudge(self, turn_count, affection_level, user_id="jalsarraf"):
            started.set()
            await release.wait()
            return "nudge"

        async def recall_for_prompt(self, *args, **kwargs):
            await asyncio.wait_for(started.wait(), 1)
            release.set()
            return [], {}, []

    builder = ChatPromptBuilder(
        memory=Memory(),
        affection=None,
        proactive=None,
        router=None,
        ws=None,
        assemble_prompt=lambda **kwargs: "",
        personality=dict,
        wants_thread=lambda content: False,
        needs_image=lambda content: False,
        hours_away=None,
        warm_budget=1,
        turn_budget=1,
    )
    recalled, nudge = await builder.recall_and_nudge(
        "what did I tell you about the lake", False, 5, "jalsarraf", 6
    )
    assert nudge == "nudge"
    assert recalled == ([], {}, [])


async def test_a_cancelled_extraction_is_requeued():
    calls = {"n": 0}

    class WS:
        def __init__(self):
            self.tasks = []

        def track_task(self, user_id, task, survive_disconnect=False):
            self.tasks.append(task)

    async def extract(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ExtractionCancelled("cancelled")

    followups = ChatFollowups(
        ws=WS(),
        last_memory_id=lambda user_id: None,
        wants_recall=lambda content: False,
        needs_image=lambda content: False,
        keep_memory=extract,
        recall=extract,
        image=extract,
        extract=extract,
        compact=extract,
        save_keywords=(),
        discard_keywords=(),
        trivial_patterns=(),
        compact_threshold=99,
        extraction_idle_s=0.01,
    )
    class Session:
        turns: list = []

        def model_copy(self, deep=False):
            return self

    await followups.admit("I will fix the door tomorrow", "I heard you.", Session(), "jalsarraf")
    await asyncio.wait_for(asyncio.gather(*followups.ws.tasks), 1)
    assert calls["n"] == 2


async def test_a_newer_turn_replaces_a_waiting_extraction():
    calls = []

    class WS:
        def __init__(self):
            self.tasks = []

        def track_task(self, user_id, task, survive_disconnect=False):
            self.tasks.append(task)

    class Session:
        turns: list = []

        def model_copy(self, deep=False):
            return self

    async def extract(*args, **kwargs):
        calls.append(args[0])

    followups = ChatFollowups(
        ws=WS(),
        last_memory_id=lambda user_id: None,
        wants_recall=lambda content: False,
        needs_image=lambda content: False,
        keep_memory=extract,
        recall=extract,
        image=extract,
        extract=extract,
        compact=extract,
        save_keywords=(),
        discard_keywords=(),
        trivial_patterns=(),
        compact_threshold=99,
        extraction_idle_s=0.05,
    )
    await followups.admit("first private fact", "noted", Session(), "jalsarraf")
    await followups.admit("second private fact", "noted too", Session(), "jalsarraf")
    await asyncio.wait_for(asyncio.gather(*followups.ws.tasks), 1)
    assert calls == ["first private fact", "second private fact"]


async def test_streamed_chunks_arrive_before_the_turn_is_done(monkeypatch):
    from app import grok_chat

    finished = asyncio.Event()

    class Response:
        status_code = 200

        async def aiter_bytes(self):
            yield b'{"type":"delta","text":"Hi"}\n'
            await asyncio.sleep(0.05)
            yield (
                b'{"type":"done","text":"Hi there","model":"grok-4.7",'
                b'"effort":"low","effort_source":"stream"}\n'
            )
            finished.set()

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(grok_chat, "configured", lambda: True)
    monkeypatch.setattr(grok_chat, "_settings", lambda: ("/tmp/sock", "token"))

    async def http_stream(*args, **kwargs):
        yield Response()

    # http_stream is an async context manager in production. Provide one.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def stream_cm(*args, **kwargs):
        yield Response()

    monkeypatch.setattr(grok_chat, "http_stream", stream_cm)
    monkeypatch.setattr(grok_chat.httpx, "AsyncHTTPTransport", lambda **kwargs: object())
    monkeypatch.setattr(grok_chat.httpx, "AsyncClient", Client)

    pieces = []
    async for piece in grok_chat.iter_text("persona", "hi", timeout=2):
        pieces.append(piece)
        if piece == "Hi":
            assert not finished.is_set()
            break
    assert pieces == ["Hi"]


async def test_a_cancelled_or_empty_stream_does_not_invent_a_reply(monkeypatch):
    from app import grok_chat
    from app.grok_chat import GrokChatCancelled, GrokChatError
    from app.llm_router import LLMRouter, LLMStreamFailed

    monkeypatch.setattr(grok_chat, "configured", lambda: False)
    with pytest.raises(GrokChatError, match="not configured"):
        await grok_chat.complete_turn("persona", "hi")

    class Response:
        def __init__(self, status, body):
            self.status_code = status
            self._body = body

        async def aiter_bytes(self):
            yield self._body

    from contextlib import asynccontextmanager

    bodies = {}

    @asynccontextmanager
    async def stream_cm(*args, **kwargs):
        yield Response(bodies["status"], bodies["body"])

    monkeypatch.setattr(grok_chat, "configured", lambda: True)
    monkeypatch.setattr(grok_chat, "_settings", lambda: ("/tmp/sock", "token"))
    monkeypatch.setattr(grok_chat, "http_stream", stream_cm)
    monkeypatch.setattr(grok_chat.httpx, "AsyncHTTPTransport", lambda **kwargs: object())

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(grok_chat.httpx, "AsyncClient", Client)
    bodies["status"] = 409
    bodies["body"] = b'{"cancelled": true}\n'
    with pytest.raises(GrokChatCancelled):
        await grok_chat.complete_turn("persona", "hi")
    bodies["status"] = 200
    bodies["body"] = b'{"nope": true}\n'
    with pytest.raises(GrokChatError, match="malformed"):
        await grok_chat.complete_turn("persona", "hi")

    router = LLMRouter()

    async def empty(system, prompt, *, timeout=120, role="chat"):
        if False:
            yield ""

    async def die_after(system, prompt, *, timeout=120, role="chat"):
        yield "Hi"
        raise GrokChatError("down")

    async def parts(system, prompt, *, timeout=120, role="chat"):
        yield "Hi"

    monkeypatch.setattr("app.grok_chat.iter_text", empty)
    assert [x async for x in router._stream_grok("sys", [{"role": "user", "content": [{"text": "hi"}, "there"]}])] == [
        router_failure()
    ]
    monkeypatch.setattr("app.grok_chat.iter_text", die_after)
    with pytest.raises(LLMStreamFailed):
        _ = [x async for x in router._stream_grok("sys", [{"role": "user", "content": "hi"}])]
    monkeypatch.setattr("app.grok_chat.iter_text", parts)
    assert [x async for x in router._stream_grok("sys", [{"role": "user", "content": "hi"}])] == ["Hi"]


def router_failure():
    from app.llm_router import FAILURE_SENTINEL
    return FAILURE_SENTINEL
