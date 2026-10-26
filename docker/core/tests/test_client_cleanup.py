"""One client cleanup failure must not leak another client's connection pool."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.llm_router import LLMRouter
from app.memory import MemoryManager


@pytest.mark.parametrize("cancel", [False, True])
async def test_second_client_closes_even_if_first_close_fails(cancel):
    resource = MemoryManager()
    failure = asyncio.CancelledError() if cancel else RuntimeError("first close failed")
    first = SimpleNamespace(aclose=AsyncMock(side_effect=failure))
    second = SimpleNamespace(aclose=AsyncMock())
    resource._redis, resource._http = first, second
    with pytest.raises(type(failure)):
        await resource.close()
    second.aclose.assert_awaited_once()


@pytest.mark.parametrize("cancel", [False, True])
async def test_router_close_awaits_its_only_client_and_surfaces_failure(cancel):
    router = LLMRouter()
    failure = asyncio.CancelledError() if cancel else RuntimeError("close failed")
    client = SimpleNamespace(aclose=AsyncMock(side_effect=failure))
    router._anthropic_http = client
    with pytest.raises(type(failure)):
        await router.close()
    client.aclose.assert_awaited_once()
