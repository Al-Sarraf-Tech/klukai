"""Drain/cancellation/admission contracts of the application's task owner."""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_shutdown_drains_completed_work_and_joins_cancelled_finalizers():
    from app.runtime_tasks import TaskSupervisor

    owner = TaskSupervisor()
    started, finalizing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    events = []

    async def work():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalizing.set()
            await release.wait()
            events.append("finalizer_finished")

    task = owner.spawn(work())
    await started.wait()
    closing = asyncio.create_task(owner.close(drain_seconds=0))
    await finalizing.wait()
    assert not closing.done()
    release.set()
    await closing
    assert task.done() and events == ["finalizer_finished"]


@pytest.mark.asyncio
async def test_no_new_background_work_is_admitted_from_a_shutdown_finalizer():
    from app.runtime_tasks import ApplicationClosing, TaskSupervisor

    owner = TaskSupervisor()
    started = asyncio.Event()
    events = []

    async def late_work():
        events.append("late_work_ran")

    async def work():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            with pytest.raises(ApplicationClosing):
                owner.spawn(late_work())
            events.append("rejected")
            await asyncio.sleep(0)
            events.append("finalizer_finished")

    owner.spawn(work())
    await started.wait()
    await owner.close(drain_seconds=0)
    assert events == ["rejected", "finalizer_finished"]


@pytest.mark.asyncio
async def test_cancelling_shutdown_does_not_interrupt_an_owned_finalizer():
    from app.runtime_tasks import TaskSupervisor

    owner = TaskSupervisor()
    started, finalizing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    events = []

    async def work():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalizing.set()
            await release.wait()
            events.append("settled")

    task = owner.spawn(work())
    await started.wait()
    closing = asyncio.create_task(owner.close(drain_seconds=0))
    await finalizing.wait()
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert task.done() and events == ["settled"]


@pytest.mark.asyncio
async def test_short_work_finishes_during_grace_period_without_cancellation():
    from app.runtime_tasks import TaskSupervisor

    owner = TaskSupervisor()

    async def work():
        await asyncio.sleep(0)
        return "completed"

    task = owner.spawn(work())
    await owner.close(drain_seconds=1)
    assert await task == "completed"
    await owner.close(drain_seconds=0)
