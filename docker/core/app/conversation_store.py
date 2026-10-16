"""Append-only conversation/message persistence and explicit failure results."""

from __future__ import annotations


async def create_conversation(
    conv_id: str, user_id: str = "jalsarraf", *, strict: bool = False
) -> None:
    """Create a new conversation record scoped to a user."""
    import logging
    from .db import get_conn_autocommit

    logger = logging.getLogger(__name__)
    try:
        async with get_conn_autocommit() as conn:
            await conn.execute(
                "INSERT INTO companion_conversations (id, user_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (conv_id, user_id),
            )
    except Exception as e:
        logger.error("Failed to create conversation: %s", e)
        if strict:
            raise


async def store_message(
    conversation_id: str,
    role: str,
    content: str,
    model: str = "",
    latency_ms: int | None = None,
    user_id: str = "jalsarraf",
) -> bool:
    """Store a message and update conversation turn count atomically.

    Returns True when the message was persisted, False on DB failure so the
    chat path can warn the Commander instead of silently losing the message.
    """
    import logging
    from .db import get_conn

    logger = logging.getLogger(__name__)
    try:
        async with get_conn() as conn:
            await conn.execute(
                "INSERT INTO companion_messages "
                "(conversation_id, role, content, model, latency_ms, user_id) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (conversation_id, role, content, model, latency_ms, user_id),
            )
            await conn.execute(
                "UPDATE companion_conversations SET turn_count = turn_count + 1, "
                "model_used = %s WHERE id = %s",
                (model, conversation_id),
            )
            await conn.commit()
        return True
    except Exception as e:
        logger.error("Failed to store message: %s", e)
        return False
