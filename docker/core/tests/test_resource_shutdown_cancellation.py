"""Actual HTTP transports must finish closing before lifespan resources retire."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import importlib

import httpx
import pytest

from app import main, runtime_tasks, shared_http
from app.ws_manager import WSManager
from tests.test_application_shutdown import application as application


class BlockingTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.completed = False
        self.calls = 0

    async def aclose(self):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        self.completed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module_name", ["memory_archive", "weather_client"]
)
@pytest.mark.parametrize("startup_failure", [False, True])
async def test_repeated_resource_exit_cancellation_joins_actual_lazy_transport(
    application, monkeypatch, module_name, startup_failure
):
    _, state, _ = application
    module = importlib.import_module("app." + module_name)
    transport = BlockingTransport()
    client = httpx.AsyncClient(transport=transport)
    monkeypatch.setattr(module, "_http", client)
    original = ValueError("partial startup failed")

    async def fail():
        raise original

    if startup_failure:
        monkeypatch.setattr(main.router, "init", fail)

    async def lifetime():
        async with main.lifespan(main.app):
            pass

    exiting = asyncio.create_task(lifetime())
    try:
        await transport.started.wait()
        for _ in range(3):
            exiting.cancel()
            await asyncio.sleep(0)
        assert not exiting.done()
        assert not state["database_closed"] and not transport.completed
        assert runtime_tasks._application is not None
        transport.release.set()
        with pytest.raises(asyncio.CancelledError):
            await exiting
        assert transport.completed and transport.calls == 1
        assert state["database_closed"] and runtime_tasks._application is None
        await module.close()
        assert (
            transport.calls == 1
        )  # Completed close, not HTTPX's early is_closed flag.
    finally:
        transport.release.set()
        await asyncio.gather(exiting, return_exceptions=True)
        if not transport.completed:
            await transport.aclose()  # Dispose of the pre-fix interrupted transport.


@pytest.mark.asyncio
async def test_standalone_lazy_close_keeps_pending_transport_owner_for_other_closers():
    transport = BlockingTransport()
    client = httpx.AsyncClient(transport=transport)
    first = asyncio.create_task(shared_http.close(client))
    second = None
    try:
        await transport.started.wait()
        assert client.is_closed and not transport.completed
        for _ in range(3):
            first.cancel()
            await asyncio.sleep(0)
        second = asyncio.create_task(shared_http.close(client))
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        transport.release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        await shared_http.close(client)
        assert transport.completed and transport.calls == 1
    finally:
        transport.release.set()
        await asyncio.gather(
            first, *([second] if second is not None else []), return_exceptions=True
        )
        if not transport.completed:
            await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_resource_unwind_retains_original_exception_identity_and_context(
    cleanup_failure,
):
    body_error, close_error = ValueError("body failed"), OSError("close failed")
    observed = []

    @asynccontextmanager
    async def resource():
        try:
            yield
        except ValueError as error:
            observed.append(error)
            if cleanup_failure:
                raise close_error from error
            raise

    expected = close_error if cleanup_failure else body_error
    with pytest.raises(type(expected)) as raised:
        async with runtime_tasks.running(WSManager()) as (resources, _):
            await resources.enter_async_context(resource())
            raise body_error
    assert raised.value is expected and observed == [body_error]
    if cleanup_failure:
        assert close_error.__context__ is body_error


@pytest.mark.asyncio
async def test_multiple_natural_cleanup_failures_keep_stdlib_exception_chain():
    from contextlib import AsyncExitStack

    async def outcome(owned):
        body = ValueError("body")
        first, second = OSError("close-a"), RuntimeError("close-b")
        events = []

        async def close_a():
            events.append("a")
            raise first

        async def close_b():
            events.append("b")
            raise second

        try:
            if owned:
                async with runtime_tasks.running(WSManager()) as (stack, _):
                    stack.push_async_callback(close_a)
                    stack.push_async_callback(close_b)
                    raise body
            else:
                async with AsyncExitStack() as stack:
                    stack.push_async_callback(close_a)
                    stack.push_async_callback(close_b)
                    raise body
        except BaseException as error:
            chain = []
            seen = set()
            while error is not None and id(error) not in seen:
                seen.add(id(error))
                chain.append(error)
                error = error.__context__
            return events, chain, [first, second, body]
        pytest.fail("Failing resource callbacks must propagate")

    control_events, control_chain, control_errors = await outcome(False)
    owned_events, owned_chain, owned_errors = await outcome(True)
    assert control_events == owned_events == ["b", "a"]
    assert control_chain == control_errors
    assert [(type(error).__name__, str(error)) for error in control_chain] == [
        ("OSError", "close-a"),
        ("RuntimeError", "close-b"),
        ("ValueError", "body"),
    ]
    assert owned_chain == owned_errors  # Every original exception identity survives.


@pytest.mark.asyncio
@pytest.mark.parametrize("body_present", [False, True])
async def test_nested_actual_http_close_failure_matches_direct_stdlib_context(
    body_present,
):
    from contextlib import AsyncExitStack

    async def outcome(owned, shared):
        body, cleanup = ValueError("body"), OSError("transport-close")
        close_calls = []

        class FailingTransport(httpx.AsyncBaseTransport):
            async def aclose(self):
                close_calls.append("closed")
                await asyncio.sleep(0)
                raise cleanup

        client = httpx.AsyncClient(transport=FailingTransport())
        callback = shared_http.close if shared else lambda _: client.aclose()
        try:
            if owned:
                async with runtime_tasks.running(WSManager()) as (stack, _):
                    stack.push_async_callback(callback, client)
                    if body_present:
                        raise body
            else:
                async with AsyncExitStack() as stack:
                    stack.push_async_callback(callback, client)
                    if body_present:
                        raise body
        except BaseException as error:
            chain = []
            seen = set()
            while error is not None and id(error) not in seen:
                seen.add(id(error))
                chain.append(error)
                error = error.__context__
            return chain, [cleanup, body] if body_present else [cleanup], close_calls
        pytest.fail("A transport close failure must propagate")

    oracle_chain, oracle_identities, oracle_calls = await outcome(False, False)
    assert oracle_chain == oracle_identities and oracle_calls == ["closed"]
    assert [(type(error).__name__, str(error)) for error in oracle_chain] == (
        [("OSError", "transport-close"), ("ValueError", "body")]
        if body_present
        else [("OSError", "transport-close")]
    )
    for owned, shared in [(True, False), (False, True), (True, True)]:
        chain, identities, calls = await outcome(owned, shared)
        assert calls == ["closed"]
        assert chain == identities  # Keep exact original body/transport identities.
