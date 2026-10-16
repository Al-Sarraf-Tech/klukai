"""Application task ownership; disconnect survival never means shutdown survival."""

from __future__ import annotations

import asyncio
from builtins import BaseExceptionGroup
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from types import TracebackType
import logging
from typing import Any, TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from .ws_manager import WSManager

logger = logging.getLogger(__name__)
SHUTDOWN_DRAIN_SECONDS = 5.0
T = TypeVar("T")


class ApplicationClosing(RuntimeError):
    """New background work cannot enter a retiring application lifetime."""


async def complete(future: asyncio.Future[T]) -> T:
    """Join cleanup even if its caller is cancelled; never double-cancel children."""
    cancelled = False
    while not future.done():
        try:
            # wait() leaves the physical child untouched on caller cancellation
            # and defers error retrieval until after cancellation is resolved.
            await asyncio.wait((future,))
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        if not future.cancelled():
            future.exception()  # Observe cleanup failures even when cancellation wins.
        raise asyncio.CancelledError
    error = None if future.cancelled() else future.exception()
    context = error.__context__ if error is not None else None
    try:
        return future.result()
    except BaseException:
        if error is not None and context is not None:
            error.__context__ = context  # Preserve constructed chains; allow new ones.
        raise


async def join(tasks: Iterable[asyncio.Task[Any]]) -> list[Any]:
    """Settle every child, including asynchronous finalizers or thread writes."""
    return await complete(asyncio.gather(*tasks, return_exceptions=True))


class TaskSupervisor:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if any(not task.done() for task in self._tasks):
            raise RuntimeError("Previous application work has not settled")
        self._tasks.clear()
        self._closing = False
        self._close_task = None

    def stop_admission(self) -> None:
        self._closing = True

    def track(self, task: asyncio.Task[Any]) -> None:
        if task in self._tasks:
            return
        self._tasks.add(task)

        def finished(done: asyncio.Task[Any]) -> None:
            self._tasks.discard(done)
            if not done.cancelled():
                done.exception()  # Retrieve errors; the callsite owns reporting.

        task.add_done_callback(finished)
        if self._closing and not task.done() and not task.cancelling():
            task.cancel()

    def spawn(self, coroutine: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        if self._closing:
            coroutine.close()
            raise ApplicationClosing("Application is shutting down")
        try:
            task = asyncio.create_task(coroutine)
        except BaseException:
            coroutine.close()
            raise
        self.track(task)
        return task

    async def _finish(self, drain_seconds: float) -> None:
        pending = {task for task in self._tasks if not task.done()}
        if pending and drain_seconds > 0:
            await asyncio.wait(pending, timeout=drain_seconds)
        while pending := {task for task in self._tasks if not task.done()}:
            for task in pending:
                if not task.cancelling():
                    task.cancel()
            await join(pending)

    async def close(self, *, drain_seconds: float | None = None) -> None:
        self.stop_admission()
        if self._close_task is None:
            grace = SHUTDOWN_DRAIN_SECONDS if drain_seconds is None else drain_seconds
            self._close_task = asyncio.create_task(
                self._finish(max(0, grace)), name="application-work-shutdown"
            )
        await complete(self._close_task)


_application: TaskSupervisor | None = None
_standalone = TaskSupervisor()


def spawn(coroutine: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
    """Own served background work. Standalone callers retain the returned task."""
    task = (_application or _standalone).spawn(coroutine)

    def report(done: asyncio.Task[Any]) -> None:
        if not done.cancelled() and (error := done.exception()) is not None:
            logger.warning(
                "Application background task failed (%s)", type(error).__name__
            )

    task.add_done_callback(report)
    return task


async def settle(awaitables: Iterable[Awaitable[Any]]) -> None:
    results = await complete(asyncio.gather(*awaitables, return_exceptions=True))
    errors = [result for result in results if isinstance(result, BaseException)]
    if errors:
        raise BaseExceptionGroup("Application work shutdown failed", errors)


class _ResourceStack(AsyncExitStack):
    """One owned unwind protects all callbacks, including partial startup."""

    async def _unwind(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        if exc is None:
            return await super().__aexit__(exc_type, exc, traceback)
        # A new task does not inherit the parent's active exception state.
        # AsyncExitStack needs that state to chain natural callback failures.
        context = exc.__context__
        try:
            raise exc
        except BaseException:
            exc.__context__ = context
            exc.__traceback__ = traceback
            return await super().__aexit__(exc_type, exc, traceback)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        closing = asyncio.create_task(
            self._unwind(exc_type, exc, traceback),
            name="application-resource-shutdown",
        )
        result: bool | None = await complete(closing)
        return bool(result)


class ProducerOwners:
    """Compose existing single owners concurrently, before closing any resources."""

    def __init__(self) -> None:
        self._stacks: list[AsyncExitStack] = []

    async def __aenter__(self) -> ProducerOwners:
        return self

    def push_async_callback(
        self, callback: Callable[..., Awaitable[None]], *args: Any, **kwargs: Any
    ) -> None:
        stack = AsyncExitStack()
        stack.push_async_callback(callback, *args, **kwargs)
        self._stacks.append(stack)

    async def enter_async_context(self, manager: AbstractAsyncContextManager[T]) -> T:
        stack = AsyncExitStack()
        self._stacks.append(stack)
        return await stack.enter_async_context(manager)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await settle(
            stack.__aexit__(exc_type, exc, traceback) for stack in self._stacks
        )


async def _settle(ws: WSManager, owner: TaskSupervisor) -> None:
    await settle((ws.close(), owner.close()))


@asynccontextmanager
async def running(
    ws: WSManager,
) -> AsyncIterator[tuple[AsyncExitStack, ProducerOwners]]:
    """Close task owners before resources, retaining admission refusal throughout."""
    global _application
    if _application is not None:
        raise RuntimeError("Application task lifetime already active")
    owner = TaskSupervisor()
    _application = owner
    try:
        async with _ResourceStack() as resources:
            async with ProducerOwners() as owners:
                ws.start()
                owners.push_async_callback(_settle, ws, owner)
                try:
                    yield resources, owners
                finally:
                    ws.stop_admission()
                    owner.stop_admission()
    finally:
        _application = None
