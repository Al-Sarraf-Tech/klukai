"""Fail-closed Redis session persistence and single-worker session ownership."""

from __future__ import annotations
import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from datetime import datetime
import redis.asyncio as redis
from .models import SessionState
from .circuit_breakers import CircuitOpen, get_breaker

logger = logging.getLogger(__name__)
REDIS_URL = os.environ.get("REDIS_URL", "redis://companion-redis:6379/0")
SESSION_TTL = 24 * 3600
MAX_SESSION_TURNS = 16


class SessionUnavailable(RuntimeError):
    """Session persistence is unavailable; access must not borrow other state."""


class RedisSessionMixin:
    _redis: redis.Redis | None
    _session_locks: dict[str, tuple[asyncio.Lock, int]]
    _redis_reconnect_lock: asyncio.Lock

    async def _redis_op(
        self,
        op: Callable[..., Awaitable[Any]],
        *args: Any,
        strict: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Execute a Redis operation with auto-reconnect on failure."""
        previous = self._redis
        try:
            return await get_breaker("redis").call(op, *args, **kwargs)
        except CircuitOpen as e:
            if strict:
                raise SessionUnavailable("Session persistence unavailable") from e
            return None
        except (redis.ConnectionError, redis.TimeoutError) as e:
            logger.warning("Redis connection error: %s — reconnecting", e)
            try:
                async with self._redis_reconnect_lock:
                    if self._redis is previous:
                        # Typed boundary for the redis factory's missing stub annotation.
                        factory: Callable[..., redis.Redis] = redis.from_url
                        self._redis = factory(
                            REDIS_URL,
                            decode_responses=True,
                            retry_on_timeout=True,
                            socket_keepalive=True,
                            health_check_interval=30,
                        )
                        if previous is not None:
                            try:
                                async with asyncio.timeout(2):
                                    await previous.aclose()
                            except Exception as close_error:
                                logger.warning(
                                    "Redis replaced-client cleanup failed: %s",
                                    type(close_error).__name__,
                                )
                # Re-bind operation to the new client
                new_op = getattr(self._redis, op.__name__)
                return await get_breaker("redis").call(new_op, *args, **kwargs)
            except Exception as e2:
                logger.error("Redis reconnect failed: %s", e2)
                if strict:
                    raise SessionUnavailable(str(e2)) from e2
                return None

    # ── Tier 1: Session (Redis) ──────────────────────────────────────────

    @asynccontextmanager
    async def session_owner(self, session_id: str) -> AsyncIterator[None]:
        """Serialize this session in the deployed single-worker process.

        Count holders AND waiters before yielding, so cancellation and idle
        reclamation cannot create two locks for the same session.
        """
        lock, users = self._session_locks.get(session_id, (asyncio.Lock(), 0))
        self._session_locks[session_id] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            _, users = self._session_locks[session_id]
            if users == 1:
                del self._session_locks[session_id]
            else:
                self._session_locks[session_id] = (lock, users - 1)

    async def get_session(self, session_id: str) -> SessionState | None:
        if self._redis is None:
            raise SessionUnavailable("Session persistence not initialized")
        raw = await self._redis_op(
            self._redis.get, f"companion:session:{session_id}", strict=True
        )
        if raw is None:
            return None
        try:
            return SessionState.model_validate_json(raw)
        except Exception as e:
            raise SessionUnavailable("Stored session is invalid") from e

    async def save_session(self, session_id: str, state: SessionState) -> None:
        if len(state.turns) > MAX_SESSION_TURNS:
            state.turns = state.turns[-MAX_SESSION_TURNS:]
        state.last_activity = datetime.now()
        if self._redis is None:
            raise SessionUnavailable("Session persistence not initialized")
        saved = await self._redis_op(
            self._redis.set,
            f"companion:session:{session_id}",
            state.model_dump_json(),
            ex=SESSION_TTL,
            strict=True,
        )
        if saved is None or saved is False:
            raise SessionUnavailable("Session write was not acknowledged")

    async def add_turn(
        self, session_id: str, role: str, content: str, state: SessionState
    ) -> SessionState:
        state.turns.append({"role": role, "content": content})
        state.turn_count += 1
        await self.save_session(session_id, state)
        return state
