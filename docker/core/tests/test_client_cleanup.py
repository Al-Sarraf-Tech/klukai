"""One client cleanup failure must not leak another client's connection pool."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.llm_router import LLMRouter
from app.memory import MemoryManager


@pytest.mark.parametrize("kind", ["memory", "router"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_second_client_closes_even_if_first_close_fails(kind, cancel):
    resource = MemoryManager() if kind == "memory" else LLMRouter()
    failure = asyncio.CancelledError() if cancel else RuntimeError("first close failed")
    first = SimpleNamespace(aclose=AsyncMock(side_effect=failure))
    second = SimpleNamespace(aclose=AsyncMock())
    if kind == "memory":
        resource._redis, resource._http = first, second
    else:
        resource._http, resource._anthropic_http = first, second
    with pytest.raises(type(failure)):
        await resource.close()
    second.aclose.assert_awaited_once()
