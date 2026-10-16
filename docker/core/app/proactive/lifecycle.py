"""Scheduler task ownership and bounded shutdown, shared by one engine."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from functools import wraps
from inspect import isawaitable
import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .base import _EngineBase


class SchedulerLifecycleMixin(_EngineBase):
    _scheduler: AsyncIOScheduler
    _background_tasks: set[asyncio.Task[Any]]
    _scheduled_tasks: set[asyncio.Task[Any]]

    @property
    def _task_logger(self) -> logging.Logger:
        return logging.getLogger("app.proactive.engine")

    def _track_task(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        self._background_tasks.add(task)

        def finished(done: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                self._task_logger.warning(
                    "Proactive background work failed: %s", done.exception()
                )

        task.add_done_callback(finished)
        return task

    def _spawn(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task[Any] | None:
        if self._closing:
            coroutine.close()
            return None
        try:
            return self._track_task(asyncio.create_task(coroutine))
        except BaseException:
            coroutine.close()
            raise

    def _add_job(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        @wraps(function)
        async def owned(*job_args: Any, **job_kwargs: Any) -> Any:
            if self._closing:
                return
            task = asyncio.current_task()
            if task is None:
                raise RuntimeError("Scheduled work requires an asyncio task")
            self._scheduled_tasks.add(task)
            try:
                result = function(*job_args, **job_kwargs)
                if isawaitable(result):
                    return await result
                return result
            finally:
                self._scheduled_tasks.discard(task)

        return self._scheduler.add_job(owned, *args, **kwargs)

    def stop(self) -> None:
        self._closing = True
        for timer in self._mission_timers.values():
            if timer.active:
                timer.stop()
        self._mission_timers.clear()
        if self._mission_timer and self._mission_timer.active:
            self._mission_timer.stop()
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None  # synchronous compatibility callers outside a loop
        for task in self._background_tasks:
            if task is not current and not task.done() and not task.cancelling():
                task.cancel()
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    async def close(self) -> None:
        """Stop admission and join owned maintenance before clients/DB close."""
        try:
            self.stop()
        finally:
            # AsyncIOScheduler marshals shutdown onto its event loop. Let it
            # cancel jobs once before joining; double cancellation interrupts
            # asynchronous finalizers and can leak connections.
            await asyncio.sleep(0)
            tasks = (self._background_tasks | self._scheduled_tasks) - {
                asyncio.current_task()
            }
            for task in tasks:
                if not task.done() and not task.cancelling():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
