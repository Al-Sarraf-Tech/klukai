"""Multi-user authentication: bcrypt passwords, token sessions, IP banning."""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone

import bcrypt

from .db import get_conn, get_conn_autocommit

logger = logging.getLogger(__name__)

# Seed users — passwords read from environment variables.
# Set SEED_PASSWORD_<USERNAME> in .env or docker-compose environment.
# If a password env var is missing, that user is skipped (not created).
_SEED_USERS = [
    {"id": "jalsarraf", "username": "jalsarraf", "display_name": "Commander"},
    {"id": "ricky", "username": "ricky", "display_name": "Commander"},
    {"id": "miguel", "username": "miguel", "display_name": "Commander"},
    {"id": "blackman", "username": "blackman", "display_name": "Commander"},
]

# Failed login threshold — 3 failures from same IP within 1 hour = ban
IP_BAN_THRESHOLD = 3
IP_BAN_WINDOW_MINUTES = 60


async def init_users() -> None:
    """Create seed users if they don't already exist.

    Passwords are read from SEED_PASSWORD_<USERNAME> environment variables.
    If a password env var is missing, the user is skipped.
    Called once during application startup.
    """
    try:
        async with get_conn_autocommit() as conn:
            for user in _SEED_USERS:
                row = await (
                    await conn.execute(
                        "SELECT id FROM companion_users WHERE id = %s",
                        (user["id"],),
                    )
                ).fetchone()
                if row:
                    logger.debug("User %s already exists", user["id"])
                    continue

                # Read password from environment — skip user if not set
                env_key = f"SEED_PASSWORD_{user['username'].upper()}"
                password = os.environ.get(env_key, "")
                if not password:
                    logger.warning(
                        "Skipping user %s: no %s environment variable set",
                        user["id"], env_key,
                    )
                    continue

                pw_hash = bcrypt.hashpw(
                    password.encode(), bcrypt.gensalt()
                ).decode()
                await conn.execute(
                    "INSERT INTO companion_users (id, username, password_hash, display_name) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (user["id"], user["username"], pw_hash, user["display_name"]),
                )
                logger.info("Created user: %s", user["id"])

            # Ensure jalsarraf has an affection row (pinned at max)
            await _ensure_affection_row("jalsarraf")
    except Exception as e:
        logger.error("Failed to init users: %s", e)


async def _ensure_affection_row(user_id: str) -> None:
    """Make sure this user has an affection row in companion_affection."""
    try:
        async with get_conn_autocommit() as conn:
            row = await (
                await conn.execute(
                    "SELECT id FROM companion_affection WHERE user_id = %s",
                    (user_id,),
                )
            ).fetchone()
            if not row:
                await conn.execute(
                    "INSERT INTO companion_affection "
                    "(score, level, level_name, daily_points_earned, "
                    "consecutive_days, total_interactions, user_id) "
                    "VALUES (0, 0, 'Cold Assessment', 0, 0, 0, %s)",
                    (user_id,),
                )
                logger.info("Created affection row for user %s", user_id)
    except Exception as e:
        logger.warning("Failed to ensure affection row for %s: %s", user_id, e)


async def create_affection_for_user(user_id: str) -> None:
    """Create a fresh affection row at level 0 for a new user."""
    await _ensure_affection_row(user_id)


async def authenticate(username: str, password: str, ip: str) -> str | None:
    """Verify credentials and return a session token, or None on failure.

    Also records the login attempt for IP ban tracking.
    """
    try:
        async with get_conn_autocommit() as conn:
            row = await (
                await conn.execute(
                    "SELECT id, password_hash FROM companion_users WHERE username = %s",
                    (username,),
                )
            ).fetchone()

            if row and bcrypt.checkpw(password.encode(), row[1].encode()):
                # Success — create token
                token = secrets.token_urlsafe(48)
                await conn.execute(
                    "INSERT INTO companion_auth_sessions (token, user_id) "
                    "VALUES (%s, %s)",
                    (token, row[0]),
                )
                # Record successful attempt
                await conn.execute(
                    "INSERT INTO companion_login_attempts (ip_address, success) "
                    "VALUES (%s, TRUE)",
                    (ip,),
                )
                logger.info("User %s authenticated from %s", username, ip)
                return token
            else:
                # Failure — record attempt
                await conn.execute(
                    "INSERT INTO companion_login_attempts (ip_address, success) "
                    "VALUES (%s, FALSE)",
                    (ip,),
                )
                logger.warning("Failed login for '%s' from %s", username, ip)
                return None
    except Exception as e:
        logger.error("Authentication error: %s", e)
        return None


async def check_ip_banned(ip: str) -> bool:
    """Return True if this IP has 3+ failed attempts in the last hour."""
    try:
        async with get_conn() as conn:
            row = await (
                await conn.execute(
                    "SELECT COUNT(*) FROM companion_login_attempts "
                    "WHERE ip_address = %s AND success = FALSE "
                    "AND attempted_at > NOW() - INTERVAL '%s minutes'",
                    (ip, IP_BAN_WINDOW_MINUTES),
                )
            ).fetchone()
            if row and row[0] >= IP_BAN_THRESHOLD:
                logger.warning("IP %s is banned (%d failed attempts)", ip, row[0])
                return True
    except Exception as e:
        logger.warning("IP ban check failed: %s", e)
    return False


async def get_user_from_token(token: str) -> str | None:
    """Look up user_id from a session token. Returns None if expired/invalid."""
    try:
        async with get_conn() as conn:
            row = await (
                await conn.execute(
                    "SELECT user_id, expires_at FROM companion_auth_sessions "
                    "WHERE token = %s",
                    (token,),
                )
            ).fetchone()
            if row:
                expires = row[1]
                # Make both timezone-aware for comparison
                now = datetime.now(timezone.utc)
                if expires.tzinfo is None:
                    from datetime import timezone as tz
                    expires = expires.replace(tzinfo=tz.utc)
                if now < expires:
                    return row[0]
                else:
                    logger.debug("Token expired for user %s", row[0])
    except Exception as e:
        logger.warning("Token lookup failed: %s", e)
    return None


async def cleanup_expired_sessions() -> int:
    """Delete expired session tokens. Call periodically."""
    try:
        async with get_conn_autocommit() as conn:
            cur = await conn.execute(
                "DELETE FROM companion_auth_sessions WHERE expires_at < NOW() RETURNING token"
            )
            rows = await cur.fetchall()
            if rows:
                logger.info("Cleaned up %d expired sessions", len(rows))
            return len(rows)
    except Exception as e:
        logger.warning("Session cleanup failed: %s", e)
        return 0
