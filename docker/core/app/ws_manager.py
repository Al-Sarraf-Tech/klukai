"""WebSocket connection manager for companion — multi-device support."""

from __future__ import annotations
from typing import Any


import asyncio
import json
import logging

from fastapi import WebSocket
from .runtime_tasks import TaskSupervisor, settle

logger = logging.getLogger(__name__)

SEND_TIMEOUT_SECONDS = 10.0
CLOSE_TIMEOUT_SECONDS = 2.0
SESSION_CHECK_TIMEOUT_SECONDS = 5.0
HANDSHAKE_TIMEOUT_SECONDS = 5.0


class WSManager:
    """Manages WebSocket connections per user. Supports multiple devices.

    Outbound only: sends fan out to every device of a user. Inbound frames
    are read by each endpoint coroutine from its own socket (chat.py) — a
    shared cross-socket receive let one device's disconnect tear down another.
    """

    def __init__(self, *, max_connections: int = 64, max_per_user: int = 8) -> None:
        if max_connections < 1 or max_per_user < 1:
            raise ValueError("WebSocket capacity must be positive")
        self._max_connections = max_connections
        self._max_per_user = max_per_user
        self._admissions: dict[WebSocket, str] = {}
        self._connections: dict[str, set[WebSocket]] = {}
        self._disconnect_events: dict[WebSocket, asyncio.Event] = {}
        # Database identities only, never bearer tokens. The endpoint supplies
        # a hash for every authenticated connection.
        self._session_hashes: dict[WebSocket, str] = {}
        # Per-user background tasks (reflection, decompression, warmup timers).
        # Cancelled on full-user disconnect so an old session can't write to a
        # stale conn_id or send to a closed WS. Accepted account-image jobs are
        # kept in _durable_tasks so a closed tab does not abandon the journal.
        self._user_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._durable_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._task_owner = TaskSupervisor()
        self._closing = False

    def start(self) -> None:
        self._task_owner.start()
        self._closing = False

    def stop_admission(self) -> None:
        self._closing = True
        self._task_owner.stop_admission()

    async def close(self) -> None:
        """Retire sockets and settle both ordinary and accepted durable work."""
        self.stop_admission()
        users = tuple(self._connections)
        await settle(
            (
                *(
                    self.disconnect(
                        user, close=True, code=1001, reason="Service shutting down"
                    )
                    for user in users
                ),
                self._task_owner.close(),
            )
        )
        self._user_tasks.clear()
        self._durable_tasks.clear()

    def track_task(
        self, user_id: str, task: asyncio.Task[Any], *, survive_disconnect: bool = False
    ) -> None:
        """Register a per-user background task.

        The default is cancelled when the user's last device disconnects.
        survive_disconnect keeps an accepted account-image job until it finishes.
        Completed tasks remove themselves.
        """
        self._task_owner.track(task)
        store = self._durable_tasks if survive_disconnect else self._user_tasks
        bucket = store.setdefault(user_id, set())
        bucket.add(task)

        def _discard(t: asyncio.Task[Any], _b: set[asyncio.Task[Any]] = bucket) -> None:
            _b.discard(t)
            if not t.cancelled():
                error = t.exception()
                if error is not None:
                    logger.error("Background task failed for %s: %s", user_id, error)

        task.add_done_callback(_discard)

    @property
    def connected(self) -> bool:
        return any(conns for conns in self._connections.values())

    def is_connected(self, user_id: str = "default") -> bool:
        return bool(self._connections.get(user_id))

    def has_connection(self, user_id: str, ws: WebSocket) -> bool:
        """Whether this particular endpoint may still admit inbound turns."""
        return ws in self._connections.get(user_id, ())

    async def validate_connection(
        self, user_id: str, ws: WebSocket, *, refresh: bool = False
    ) -> bool:
        """Fail closed on expired, revoked, deactivated or unavailable auth."""
        if not self.has_connection(user_id, ws):
            return False
        session_hash = self._session_hashes.get(ws)
        if session_hash is None:
            # Internal manager users can still connect without token auth;
            # the public endpoint always binds its authenticated session.
            return True
        from .auth import get_user_from_session_hash

        try:
            owner = await asyncio.wait_for(
                get_user_from_session_hash(session_hash, refresh=refresh),
                SESSION_CHECK_TIMEOUT_SECONDS,
            )
        except Exception:
            owner = None
        if owner != user_id:
            await self.disconnect(
                user_id, ws, close=True, code=4001, reason="Invalid or expired token"
            )
            return False
        # Password reset / disconnect can retire membership during the lookup.
        return self.has_connection(user_id, ws)

    async def revoke_user(self, user_id: str) -> None:
        """Retire all devices before waiting on transport or task cleanup."""
        await self.disconnect(
            user_id, close=True, code=4001, reason="Invalid or expired token"
        )

    async def connect(
        self,
        ws: WebSocket,
        user_id: str = "default",
        *,
        disconnected: asyncio.Event | None = None,
        session_hash: str | None = None,
    ) -> None:
        if self._closing:
            await asyncio.wait_for(
                ws.close(code=1013, reason="Service shutting down"),
                CLOSE_TIMEOUT_SECONDS,
            )
            raise ConnectionError("WebSocket manager is shutting down")
        total = sum(map(len, self._connections.values())) + len(self._admissions)
        owned = len(self._connections.get(user_id, ())) + sum(
            u == user_id for u in self._admissions.values()
        )
        if total >= self._max_connections:
            try:
                # No room even for a rejection handshake. Deny before accept
                # (HTTP403 under Uvicorn); clients back off without logging out.
                await asyncio.wait_for(
                    ws.close(code=1013, reason="Connection capacity reached"),
                    CLOSE_TIMEOUT_SECONDS,
                )
            except Exception:
                pass
            raise ConnectionError("WebSocket capacity reached")
        self._admissions[ws] = user_id
        if owned >= self._max_per_user:
            try:
                # Complete the handshake so clients receive a retryable close,
                # rather than confusing capacity with invalid authentication.
                await asyncio.wait_for(ws.accept(), HANDSHAKE_TIMEOUT_SECONDS)
                await asyncio.wait_for(
                    ws.close(
                        code=1013, reason="Connection capacity reached; retry later"
                    ),
                    CLOSE_TIMEOUT_SECONDS,
                )
            except Exception:
                pass
            finally:
                self._admissions.pop(ws, None)
            raise ConnectionError("WebSocket capacity reached")
        # Reserve synchronously before the first await. Concurrent handshakes
        # count against both bounds until promoted or cleaned up.
        try:
            await asyncio.wait_for(ws.accept(), HANDSHAKE_TIMEOUT_SECONDS)
            if self._closing:
                raise ConnectionError("WebSocket manager is shutting down")
            self._connections.setdefault(user_id, set()).add(ws)
            self._admissions.pop(ws, None)
            if disconnected is not None:
                self._disconnect_events[ws] = disconnected
            if session_hash is not None:
                self._session_hashes[ws] = session_hash
            count = len(self._connections[user_id])
            logger.info("WebSocket connected: user=%s (devices=%d)", user_id, count)
            if not await self.validate_connection(user_id, ws):
                raise ConnectionError("WebSocket session is no longer valid")
            if not await self._send_one(ws, {"type": "connected", "status": "ok"}):
                raise ConnectionError("Initial WebSocket send failed")
        except asyncio.CancelledError:
            await self.disconnect(
                user_id,
                ws,
                close=self.has_connection(user_id, ws) or ws in self._admissions,
            )
            raise
        except Exception as exc:
            await self.disconnect(
                user_id,
                ws,
                close=self.has_connection(user_id, ws) or ws in self._admissions,
            )
            raise ConnectionError("WebSocket admission failed") from exc
        finally:
            self._admissions.pop(ws, None)

    async def disconnect(
        self,
        user_id: str = "default",
        ws: WebSocket | None = None,
        *,
        close: bool = False,
        code: int = 1011,
        reason: str = "Connection send failed; reconnect",
    ) -> None:
        conns = self._connections.get(user_id)
        retired = tuple(conns or ()) if ws is None else (ws,)
        for socket in retired:
            self._session_hashes.pop(socket, None)
            disconnected = self._disconnect_events.pop(socket, None)
            if disconnected is not None:
                disconnected.set()
        if conns is not None:
            if ws is None:
                conns.clear()
            else:
                conns.discard(ws)
        tasks: set[asyncio.Task[Any]] = set()
        if not conns:
            self._connections.pop(user_id, None)
            # Detach before any await. Reconnects receive a new task bucket.
            tasks = self._user_tasks.pop(user_id, set())
        current = asyncio.current_task()
        pending = [task for task in tasks if task is not current]
        for task in pending:
            if not task.done() and not task.cancelling():
                task.cancel()

        async def close_socket(socket: WebSocket) -> None:
            try:
                await asyncio.wait_for(
                    socket.close(code=code, reason=reason),
                    CLOSE_TIMEOUT_SECONDS,
                )
            except Exception:
                # Membership is already revoked, so this endpoint cannot
                # process another frame even if the transport cannot close.
                logger.debug("Could not close retired WebSocket for %s", user_id)

        if close:
            await asyncio.gather(*(close_socket(socket) for socket in retired))
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _send_one(self, ws: WebSocket, data: dict[str, Any]) -> bool:
        """Send to a single WebSocket. Returns False if failed."""
        try:
            await asyncio.wait_for(ws.send_text(json.dumps(data)), SEND_TIMEOUT_SECONDS)
            return True
        except Exception:
            return False

    async def send(self, user_id: str, data: dict[str, Any]) -> None:
        """Broadcast to ALL connected devices for this user."""
        conns = self._connections.get(user_id)
        if not conns:
            return
        for socket in tuple(conns):
            if not await self.validate_connection(user_id, socket):
                continue
            if not await self._send_one(socket, data):
                await self.disconnect(user_id, socket, close=True)

    async def send_token(self, user_id: str, text: str) -> None:
        await self.send(user_id, {"type": "token", "text": text})

    async def send_done(
        self, user_id: str, message_id: str, model: str, final_text: str | None = None
    ) -> None:
        msg = {"type": "done", "message_id": message_id, "model": model}
        if final_text is not None:
            msg["text"] = final_text
        await self.send(user_id, msg)

    async def send_thinking(self, user_id: str, text: str) -> None:
        await self.send(user_id, {"type": "thinking", "text": text})

    async def send_tool_use(self, user_id: str, tool: str, status: str) -> None:
        await self.send(user_id, {"type": "tool_use", "tool": tool, "status": status})

    async def send_mood(self, user_id: str, mood: str) -> None:
        await self.send(user_id, {"type": "mood", "mood": mood})

    async def send_heartbeat(
        self, user_id: str, bpm: int, mood: str, intensity: int
    ) -> None:
        """Her live heart rate for this turn (app/heartbeat.py), after the mood frame."""
        await self.send(
            user_id,
            {"type": "heartbeat", "bpm": bpm, "mood": mood, "intensity": intensity},
        )

    async def send_outfit(self, user_id: str, outfit: dict[str, Any]) -> None:
        """She changed (his request was granted) — the PWA updates its chip."""
        await self.send(user_id, {"type": "outfit", "outfit": outfit})

    async def send_proactive(
        self, user_id: str, message: str, *, persist: bool = True
    ) -> None:
        """Deliver a proactive line over WS, optionally persisting it to history.

        Persistence is best-effort and fail-soft: a DB blip must never prevent
        the live push. Stored as role=assistant / model=proactive so Commanders
        still see check-ins, dreams, and level-up lines in history.

        Pass ``persist=False`` for transient UX toasts — "voice link garbled",
        "rendering pipeline broke", and friends. Chat history is SACRED and
        append-only, so an infrastructure hiccup written into it is permanent
        and would also be replayed back to the model as context.
        """
        await self.send(user_id, {"type": "proactive", "message": message})
        if not persist:
            return
        try:
            await self._persist_proactive(user_id, message)
        except Exception as e:
            import logging

            logging.getLogger(__name__).warning(
                "Proactive persist failed for %s: %s", user_id, e
            )

    async def _persist_proactive(self, user_id: str, message: str) -> None:
        from .db import get_conn

        async with get_conn() as conn:
            row = await (
                await conn.execute(
                    "SELECT id FROM companion_conversations "
                    "WHERE user_id = %s ORDER BY started_at DESC LIMIT 1",
                    (user_id,),
                )
            ).fetchone()
            if not row:
                # No conversation yet — create a lightweight shell so the
                # message is not lost (SACRED: she spoke; it must land).
                row = await (
                    await conn.execute(
                        "INSERT INTO companion_conversations (user_id, summary, model_used) "
                        "VALUES (%s, %s, %s) RETURNING id",
                        (user_id, "proactive", "proactive"),
                    )
                ).fetchone()
            if row is None:
                raise RuntimeError("Conversation insert returned no identity")
            conv_id = row[0]
            await conn.execute(
                "INSERT INTO companion_messages "
                "(conversation_id, role, content, model, user_id, content_type) "
                "VALUES (%s, 'assistant', %s, 'proactive', %s, 'proactive')",
                (conv_id, message, user_id),
            )
            # Mirror helpers.store_message so turn_count keeps matching the
            # actual row count — anything reading it would otherwise drift.
            await conn.execute(
                "UPDATE companion_conversations "
                "SET turn_count = turn_count + 1 WHERE id = %s",
                (conv_id,),
            )
            await conn.commit()

    async def send_voice(
        self, user_id: str, audio_b64: str, final: bool = False
    ) -> None:
        await self.send(
            user_id, {"type": "voice_audio", "audio": audio_b64, "final": final}
        )

    async def send_affection(
        self, user_id: str, score: int, level: int, level_name: str, delta: int
    ) -> None:
        await self.send(
            user_id,
            {
                "type": "affection",
                "score": score,
                "level": level,
                "level_name": level_name,
                "delta": delta,
            },
        )

    async def send_affection_level_change(
        self, user_id: str, new_level: int, new_level_name: str, direction: str
    ) -> None:
        await self.send(
            user_id,
            {
                "type": "affection_level_change",
                "level": new_level,
                "level_name": new_level_name,
                "direction": direction,
            },
        )

    async def send_heartbeat_spike(self, user_id: str, bpm: int, mood: str) -> None:
        """Send a heartbeat spike alert for high-intensity emotional moments."""
        await self.send(
            user_id,
            {
                "type": "heartbeat_spike",
                "bpm": bpm,
                "mood": mood,
            },
        )
