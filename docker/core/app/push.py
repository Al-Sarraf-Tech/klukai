"""Web Push notification dispatch via VAPID — persisted to DB, context-aware."""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_CLAIMS = {"sub": "mailto:companion@localhost"}

# In-memory cache loaded from DB on startup
_subscriptions: dict[str, list[dict]] = {}  # user_id → list of subscriptions
_initialized = False


def get_vapid_public_key() -> str:
    return VAPID_PUBLIC_KEY


async def init_subscriptions() -> None:
    """Load subscriptions from DB on startup."""
    global _initialized
    if _initialized:
        return
    try:
        from .db import get_conn_autocommit
        async with get_conn_autocommit() as conn:
            # Create table if not exists
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS companion_push_subscriptions (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL UNIQUE,
                    subscription JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            cur = await conn.execute("SELECT user_id, subscription FROM companion_push_subscriptions")
            rows = await cur.fetchall()
            for row in rows:
                uid = row[0]
                sub = row[1] if isinstance(row[1], dict) else json.loads(row[1])
                if uid not in _subscriptions:
                    _subscriptions[uid] = []
                _subscriptions[uid].append(sub)
            _initialized = True
            total = sum(len(v) for v in _subscriptions.values())
            if total:
                logger.info("Loaded %d push subscriptions from DB", total)
    except Exception as e:
        logger.warning("Failed to load push subscriptions: %s", e)
        _initialized = True


async def add_subscription(user_id: str, sub: dict) -> None:
    """Add push subscription — persisted to DB."""
    endpoint = sub.get("endpoint", "")
    if not endpoint:
        return

    # Memory
    if user_id not in _subscriptions:
        _subscriptions[user_id] = []
    for existing in _subscriptions[user_id]:
        if existing.get("endpoint") == endpoint:
            return
    _subscriptions[user_id].append(sub)

    # DB
    try:
        from .db import get_conn_autocommit
        async with get_conn_autocommit() as conn:
            await conn.execute(
                "INSERT INTO companion_push_subscriptions (user_id, endpoint, subscription) "
                "VALUES (%s, %s, %s) ON CONFLICT (endpoint) DO UPDATE SET subscription = EXCLUDED.subscription",
                (user_id, endpoint, json.dumps(sub)),
            )
    except Exception as e:
        logger.warning("Failed to persist push subscription: %s", e)

    logger.info("Push subscription added for %s (total: %d)", user_id, len(_subscriptions[user_id]))


def remove_subscription(endpoint: str) -> None:
    """Remove a subscription from memory and DB."""
    for uid in _subscriptions:
        _subscriptions[uid] = [s for s in _subscriptions[uid] if s.get("endpoint") != endpoint]

    try:
        import asyncio
        from .db import get_conn_autocommit

        async def _delete():
            async with get_conn_autocommit() as conn:
                await conn.execute(
                    "DELETE FROM companion_push_subscriptions WHERE endpoint = %s", (endpoint,)
                )

        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(_delete())
        else:
            loop.run_until_complete(_delete())
    except Exception:
        pass


async def send_push(
    title: str,
    body: str,
    data: dict | None = None,
    user_id: str | None = None,
) -> int:
    """Send push notification. If user_id given, only to that user. Otherwise all."""
    await init_subscriptions()

    if not VAPID_PRIVATE_KEY:
        return 0

    targets = []
    if user_id and user_id in _subscriptions:
        targets = _subscriptions[user_id]
    elif not user_id:
        for subs in _subscriptions.values():
            targets.extend(subs)

    if not targets:
        return 0

    try:
        from pywebpush import webpush
    except ImportError:
        logger.warning("pywebpush not installed")
        return 0

    payload = json.dumps({
        "title": title,
        "body": body[:200],
        "data": data or {},
    })

    sent = 0
    failed_endpoints = []

    for sub in targets:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=VAPID_CLAIMS,
            )
            sent += 1
        except Exception as e:
            logger.warning("Push failed for %s: %s", sub.get("endpoint", "?")[:50], e)
            failed_endpoints.append(sub.get("endpoint"))

    for ep in failed_endpoints:
        remove_subscription(ep)

    return sent
