"""WebSocket handler and message processing.

The ``register_websocket(app)`` function attaches the ``/ws`` endpoint
and all supporting handlers (message, voice, tap-interact).
"""

from __future__ import annotations

import logging
import os

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .context import (
    affection,
    memory,
    proactive,
    session_id,
    ws,
)
from .db import get_conn
from .helpers import (
    create_conversation as _create_conversation,
)
from .models import SessionState, new_id

logger = logging.getLogger(__name__)


# ── WebSocket ────────────────────────────────────────────────────────────────


REFLECTION_MIN_HOURS_AWAY = 8
REFLECTION_MAX_HOURS_AWAY = 72  # Over 3 days = too stale, skip




async def _handle_tap_interact(user_id: str) -> None:
    """Handle tap interaction — deliver a short proactive comment."""
    if proactive and proactive._can_send():
        await proactive.trigger_tap()
    else:
        # Fallback: send a simple acknowledgment if proactive can't send
        await ws.send_proactive(user_id, "Hm? Right here, Commander.", persist=False)




async def _handle_voice(audio_b64: str, session: SessionState, user_id: str = "default") -> None:
    """Process voice: STT -> text -> LLM -> TTS -> audio.

    Failure modes surface a UX signal — the user never gets ghosted.
    """
    voice_url = os.environ.get("VOICE_URL", "http://100.107.121.5:8301")

    try:
        async with httpx.AsyncClient(
            timeout=30.0, trust_env=False, follow_redirects=False
        ) as client:
            await ws.send_thinking(user_id, "Listening...")
            try:
                from .helpers import voice_auth_headers
                r = await client.post(f"{voice_url}/stt", json={"audio": audio_b64}, headers=voice_auth_headers())
                r.raise_for_status()
                transcript = r.json().get("text", "")
            except Exception as stt_err:
                logger.error("STT failed: %s", stt_err)
                await ws.send_proactive(
                    user_id,
                    "...Voice link garbled, Commander — I couldn't make out the transmission. "
                    "Try again or switch to text.",
                    persist=False,
                )
                return

            if not transcript.strip():
                await ws.send_proactive(
                    user_id,
                    "...I heard nothing on the channel, Commander. Try again, closer to the mic.",
                    persist=False,
                )
                return

            # Process as text message (which streams the text response)
            await _handle_message(transcript, session, user_id)

            # Get the last assistant response for TTS
            session = await memory.get_session(session_id(user_id))
            if session and session.turns:
                last_turn = session.turns[-1]
                if last_turn["role"] == "assistant":
                    try:
                        from .voice_client import post_leased_tts

                        r = await post_leased_tts(
                            client,
                            {"text": last_turn["content"]},
                            timeout=30.0,
                        )
                        if r.status_code == 200:
                            import base64
                            audio_out = base64.b64encode(r.content).decode()
                            await ws.send_voice(user_id, audio_out, final=True)
                        else:
                            logger.error("TTS HTTP %s: %s", r.status_code, r.text[:200])
                            await ws.send_proactive(
                                user_id,
                                "...The voice synth is offline, Commander — "
                                "I'm reading you in text instead.",
                                persist=False,
                            )
                    except Exception as tts_err:
                        logger.error("TTS failed: %s", tts_err)
                        await ws.send_proactive(
                            user_id,
                            "...Voice synth dropped, Commander — text reply only.",
                            persist=False,
                        )
    except Exception as e:
        logger.error("Voice processing failed: %s", e, exc_info=True)
        try:
            await ws.send_proactive(
                user_id,
                "...Voice channel broke entirely, Commander. Switching to text.",
                persist=False,
            )
        except Exception:
            pass


def register_websocket(app: FastAPI) -> None:
    """Attach the WebSocket endpoint to *app*."""

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        # Authenticate via token query param
        token = websocket.query_params.get("token", "")
        if token:
            from .auth import get_user_from_token
            user_id = await get_user_from_token(token)
            if not user_id:
                await websocket.close(code=4001, reason="Invalid or expired token")
                return
        else:
            # Legacy fallback — reject unauthenticated connections
            await websocket.close(code=4001, reason="Authentication required")
            return

        await ws.connect(websocket, user_id)

        # Ensure session exists — always restore mood from PostgreSQL (source of truth)
        session_key = session_id(user_id)
        session = await memory.get_session(session_key)

        # Always restore mood from persistent state (PostgreSQL is source of truth)
        restored_mood = "composed"
        try:
            async with get_conn() as conn:
                row = await (await conn.execute(
                    "SELECT mood FROM companion_persistent_state WHERE user_id = %s",
                    (user_id,),
                )).fetchone()
                if row and row[0]:
                    restored_mood = row[0]
        except Exception as e:
            logger.warning("Failed to restore persistent mood: %s", e)

        if session is None:
            conv_id = new_id()
            session = SessionState(conversation_id=conv_id, mood=restored_mood)
            await memory.save_session(session_key, session)
            await _create_conversation(conv_id, user_id=user_id)
            logger.info("New session created with restored mood '%s' for user %s", restored_mood, user_id)
        elif session.mood != restored_mood and restored_mood != "composed":
            # Existing session but mood drifted — restore from DB
            session.mood = restored_mood
            await memory.save_session(session_key, session)
            logger.info("Session mood corrected to '%s' from persistent state", restored_mood)

        # Send restored mood to frontend immediately on connect
        if restored_mood != "composed":
            await ws.send_mood(user_id, restored_mood)

        # Restore mission timer from session if it was active before disconnect
        if session.mission_description and session.mission_interval and not proactive.mission_active:
            aff_state = await affection.get_state(user_id)
            proactive.set_affection_level(aff_state.level)
            proactive.start_mission(session.mission_description, session.mission_interval)
            logger.info(
                "Mission timer restored from session: every %d min",
                session.mission_interval,
            )

        # Reflection-on-return: if user was away >8h, greet them referencing
        # the last topic. Runs in background so it never blocks the connect.
        import asyncio as _asyncio
        ws.track_task(user_id, _asyncio.create_task(_maybe_reflect_on_return(user_id)))
        # Level-9 Oath capstone for an already-maxed Commander who reached lv9
        # before this feature existed — fires once ever, self-guarded.
        ws.track_task(user_id, _asyncio.create_task(_maybe_oath_on_connect(user_id)))

        try:
            while True:
                data = await ws.receive(user_id)
                if data is None:
                    break

                msg_type = data.get("type")

                try:
                    if msg_type == "message":
                        content = data.get("content", "")
                        if isinstance(content, str):
                            content = content[:4000]  # Input length limit
                        await _handle_message(content, session, user_id)
                    elif msg_type == "typing":
                        pass
                    elif msg_type == "voice_end":
                        audio = data.get("audio")
                        if audio:
                            await _handle_voice(audio, session, user_id)
                    elif msg_type == "tap_interact":
                        await _handle_tap_interact(user_id)
                except WebSocketDisconnect:
                    # A real disconnect during handling exits via the normal
                    # disconnect path — it is not an application error.
                    raise
                except Exception as e:
                    # A handler crash must not tear down the socket: the user
                    # message was already persisted before streaming, so tell
                    # the client and keep receiving.
                    logger.error(
                        "Message handler crashed (type=%s): %s", msg_type, e,
                        exc_info=True,
                    )
                    try:
                        await ws.send(user_id, {
                            "type": "error",
                            "message": "...Something glitched on my end, Commander. "
                                       "Your message is safe — try again.",
                        })
                    except Exception:
                        logger.warning("Could not deliver error event; closing loop")
                        break
        except WebSocketDisconnect:
            pass
        finally:
            await ws.disconnect(user_id, websocket)

# Handlers moved to chat_handlers.py (S+ Phase 2 §6.1).
from app.chat_handlers import (  # noqa: E402,F401
    _maybe_reflect_on_return,
    _maybe_oath_on_connect,
    _handle_message,
)
