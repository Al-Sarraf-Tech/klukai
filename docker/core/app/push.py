"""Web Push notification dispatch via VAPID."""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_CLAIMS = {"sub": "mailto:companion@localhost"}

# In-memory subscription store (single user)
_subscriptions: list[dict] = []


def get_vapid_public_key() -> str:
    return VAPID_PUBLIC_KEY


def add_subscription(sub: dict) -> None:
    # Avoid duplicates by endpoint
    for existing in _subscriptions:
        if existing.get("endpoint") == sub.get("endpoint"):
            return
    _subscriptions.append(sub)
    logger.info("Push subscription added (total: %d)", len(_subscriptions))


def remove_subscription(endpoint: str) -> None:
    global _subscriptions
    _subscriptions = [s for s in _subscriptions if s.get("endpoint") != endpoint]


async def send_push(title: str, body: str, data: dict | None = None) -> int:
    """Send push notification to all subscriptions. Returns count sent."""
    if not VAPID_PRIVATE_KEY or not _subscriptions:
        logger.debug("Push skipped: no key or no subscriptions")
        return 0

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logger.warning("pywebpush not installed, push notifications disabled")
        return 0

    payload = json.dumps({
        "title": title,
        "body": body[:200],
        "data": data or {},
    })

    sent = 0
    failed_endpoints = []

    for sub in _subscriptions:
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

    # Clean up failed subscriptions
    for ep in failed_endpoints:
        remove_subscription(ep)

    return sent
