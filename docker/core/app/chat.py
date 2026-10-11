"""WebSocket handler and message processing.

The ``register_websocket(app)`` function attaches the ``/ws`` endpoint
and all supporting handlers (message, voice, tap-interact).
"""

from __future__ import annotations

import json
import logging

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

# A mission still in the session on reconnect is restored only if it started
# within this window. Field reports resuming half a day after "keep me posted
# every 30 min" are about an operation that is long over.
MISSION_RESTORE_MAX_HOURS = 12


def _mission_is_stale(started_at: str | None) -> bool:
    """True when the session's mission start (an isoformat stamp from the
    container clock) is older than MISSION_RESTORE_MAX_HOURS. A missing or
    unparseable stamp is not stale: older sessions never recorded one."""
    if not started_at:
        return False
    try:
        from datetime import datetime, timedelta
        started = datetime.fromisoformat(started_at)
        if started.tzinfo is not None:
            started = started.replace(tzinfo=None)
        return datetime.now() - started > timedelta(hours=MISSION_RESTORE_MAX_HOURS)
    except (TypeError, ValueError):
        return False




async def _handle_tap_interact(user_id: str) -> None:
    """Handle tap interaction — deliver a short proactive comment."""
    if proactive and proactive._can_send():
        await proactive.trigger_tap()
    else:
        # Fallback: send a simple acknowledgment if proactive can't send
        await ws.send_proactive(user_id, "Hm? Right here, Commander.", persist=False)




async def _handle_voice(audio_b64: str, session: SessionState, user_id: str = "default") -> None:
    """Process voice: STT -> text -> LLM -> TTS -> audio.

    Failure modes surface a UX signal — the user never gets ghosted. STT
    problems go out as ``error`` frames (never persisted) because that is what
    clears the client's "Listening..." indicator; a proactive toast leaves it
    spinning.
    """
    from .voice_client import voice_url

    try:
        async with httpx.AsyncClient(
            timeout=30.0, trust_env=False, follow_redirects=False
        ) as client:
            await ws.send_thinking(user_id, "Listening...")
            try:
                from .helpers import voice_auth_headers
                r = await client.post(f"{voice_url()}/stt", json={"audio": audio_b64}, headers=voice_auth_headers())
                r.raise_for_status()
                transcript = r.json().get("text", "")
            except Exception as stt_err:
                logger.error("STT failed: %s", stt_err)
                await ws.send(user_id, {
                    "type": "error",
                    "message": "...Voice link garbled, Commander — I couldn't make out "
                               "the transmission. Try again or switch to text.",
                })
                return

            if not transcript.strip():
                await ws.send(user_id, {
                    "type": "error",
                    "message": "...I heard nothing on the channel, Commander. "
                               "Try again, closer to the mic.",
                })
                return

            # Process as text message (which streams the text response). None
            # means she had no answer this turn — her core is down — so there
            # is nothing of hers to voice. The sentinel must never reach XTTS:
            # that spends a GPU lease reading an outage notice in her voice.
            reply = await _handle_message(transcript, session, user_id)
            if reply is None:
                return

            # Stage directions — "(she smiles)", "*nods*" — are not read aloud,
            # and XTTS gets at most one clip's worth, cut at a sentence end.
            from .helpers import strip_actions_for_tts, tts_clip
            speech = tts_clip(strip_actions_for_tts(reply))
            if not speech:
                return

            try:
                from .voice_client import post_leased_tts

                r = await post_leased_tts(client, {"text": speech}, timeout=30.0)
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
        # Authenticate via token query param. A rejection is accept() THEN
        # close(4001): closing before the handshake completes is an HTTP 403
        # under uvicorn, the close code never reaches the client, and the
        # Flutter app reconnects forever instead of returning to login.
        token = websocket.query_params.get("token", "")
        if token:
            from .auth import get_user_from_token
            user_id = await get_user_from_token(token)
            if not user_id:
                await websocket.accept()
                await websocket.close(code=4001, reason="Invalid or expired token")
                return
        else:
            # Legacy fallback — reject unauthenticated connections
            await websocket.accept()
            await websocket.close(code=4001, reason="Authentication required")
            return

        await ws.connect(websocket, user_id)

        # He just opened the app. Start loading the model now so it is resident
        # by the time he has finished typing, instead of making him watch a
        # loading bar after he hits send. Fire-and-forget: this must never delay
        # the connect, and a failure only costs the slow first reply he would
        # have had anyway.
        try:
            from .warmup import warm_in_background
            warm_task = warm_in_background()
            if warm_task is not None:
                ws.track_task(user_id, warm_task)
        except Exception as e:
            logger.debug("Warm-up on connect skipped: %s", e)

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
            if _mission_is_stale(session.mission_started_at):
                # A mission from half a day ago is not one he still wants
                # field reports on — restoring it would resume updates on a
                # reconnect long after the operation it described was over.
                logger.info(
                    "Mission from %s not restored (older than %dh); clearing it",
                    session.mission_started_at, MISSION_RESTORE_MAX_HOURS,
                )
                session.mission_description = None
                session.mission_interval = None
                session.mission_started_at = None
                await memory.save_session(session_id(user_id), session)
            else:
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
        # Recurring rituals: his birthday greeting (once a year) or her monthly
        # fee settlement (once a month) — self-guarded per period.
        from . import rituals
        ws.track_task(user_id, _asyncio.create_task(rituals.on_connect(user_id)))
        # Her Day: pick today's outfit (DB + weather) now, while he types —
        # the chat turn only ever reads the cached row, so it gets the same mood.
        from . import wardrobe
        ws.track_task(user_id, _asyncio.create_task(wardrobe.warm_today(user_id, mood=session.mood)))

        try:
            while True:
                # Read THIS device's socket only. Fanning in across every
                # socket of the user let device A's frames — and its
                # disconnect — be consumed by device B's loop and tear B down.
                try:
                    raw = await websocket.receive_text()
                except WebSocketDisconnect:
                    break
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue  # malformed frame — skip it, keep the connection
                if not isinstance(data, dict):
                    continue

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
