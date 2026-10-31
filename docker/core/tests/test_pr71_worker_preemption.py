"""Worker priority admission is cancellation, not a model outage."""

import httpx
import pytest

from app import circuit_breakers as cb
from app import grok_chat
from app.circuit_breakers import State, get_breaker


@pytest.fixture(autouse=True)
def isolated_breakers():
    cb._BREAKERS.clear()
    yield
    cb._BREAKERS.clear()


@pytest.fixture
def worker(monkeypatch):
    calls = []
    replies = []

    def respond(request):
        calls.append(request)
        return replies.pop(0)

    monkeypatch.setattr(grok_chat, "configured", lambda: True)
    monkeypatch.setattr(grok_chat, "_settings", lambda: ("/synthetic-worker.sock", "synthetic-token"))
    monkeypatch.setattr(
        grok_chat.httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(respond)
    )
    return calls, replies


def _success():
    return httpx.Response(
        200,
        json={
            "text": "Hello.",
            "model": grok_chat.MODEL,
            "effort": grok_chat.chat_effort(),
            "effort_source": "stream",
        },
    )


async def test_repeated_background_preemption_does_not_refuse_chat(worker):
    calls, replies = worker
    replies.extend(httpx.Response(409) for _ in range(5))
    replies.append(_success())
    for _ in range(5):
        with pytest.raises(grok_chat.GrokChatCancelled):
            await grok_chat.complete_turn("persona", "background", role="background")
    breaker = get_breaker("grok_chat")
    assert breaker.state == State.CLOSED
    assert not breaker._errors
    assert get_breaker("grok_background").state == State.CLOSED
    assert (await grok_chat.complete_turn("persona", "hi")).text == "Hello."
    assert len(calls) == 6


@pytest.mark.parametrize("role", ["chat", "background", "recall"])
async def test_preempted_half_open_probe_releases_admission_immediately(worker, role):
    calls, replies = worker
    replies.extend([httpx.Response(409), _success()])
    breaker = get_breaker(grok_chat.breaker_name(role))
    breaker.force_open()
    breaker.cfg.probe_interval_seconds = 0
    with pytest.raises(grok_chat.GrokChatCancelled):
        await grok_chat.complete_turn("persona", "hi", role=role)
    assert breaker.state == State.CLOSED
    assert not breaker._half_open_lock.locked()
    assert (await grok_chat.complete_turn("persona", "hi", role=role)).text == "Hello."
    assert len(calls) == 2


async def test_real_worker_failures_still_trip_the_breaker(worker):
    calls, replies = worker
    replies.extend(httpx.Response(502) for _ in range(5))
    for _ in range(5):
        with pytest.raises(grok_chat.GrokChatError, match="failed"):
            await grok_chat.complete_turn("persona", "hi")
    assert get_breaker("grok_chat").state == State.OPEN
    with pytest.raises(grok_chat.GrokChatError, match="unavailable"):
        await grok_chat.complete_turn("persona", "hi")
    assert len(calls) == 5
