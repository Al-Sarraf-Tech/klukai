"""Rejected scheduling must close work that never acquired an owner."""

import inspect

from app import warmup


def test_failed_warmup_admission_closes_coroutine(monkeypatch):
    captured = []

    def reject(coroutine):
        captured.append(coroutine)
        raise RuntimeError("no event loop")

    monkeypatch.setattr(warmup.asyncio, "create_task", reject)
    try:
        assert warmup.warm_in_background() is None
        assert inspect.getcoroutinestate(captured[0]) == inspect.CORO_CLOSED
    finally:
        captured[0].close()
