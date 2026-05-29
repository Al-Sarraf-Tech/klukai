"""Multi-user authentication: bcrypt passwords, token sessions, IP banning."""

from __future__ import annotations

import hashlib
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

# The single privileged operator. Env-overridable so admin identity isn't a
# magic string ("jalsarraf") scattered across route handlers.
ADMIN_USER_ID = os.environ.get("KLUKAI_ADMIN_USER", "jalsarraf")


def is_admin(user_id: str | None) -> bool:
    """True iff user_id is the privileged operator (admin endpoints)."""
    return user_id == ADMIN_USER_ID


# Session tokens are stored HASHED at rest (sha256). The plaintext token is the
# bearer returned to the client; the DB only holds its hash, so a DB read/backup
# leak can't be replayed. SESSION_MAX_DAYS caps absolute lifetime regardless of
# rolling refresh.
SESSION_MAX_DAYS = 30


def _hash_token(token: str) -> str:
    """sha256 hex of a session token (high-entropy → a fast hash is sufficient)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
                    (_hash_token(token), row[0]),
                )
                # Record successful attempt
                await conn.execute(
                    "INSERT INTO companion_login_attempts (ip_address, success) "
                    "VALUES (%s, TRUE)",
                    (ip,),
                )
                logger.info("User %s authenticated from %s", username, ip)
                try:
                    from . import audit
                    await audit.log(audit.EVENT_LOGIN_SUCCESS, user_id=row[0],
                                    ip_address=ip, metadata={"username": username})
                except Exception:
                    pass
                return token
            else:
                # Failure — record attempt
                await conn.execute(
                    "INSERT INTO companion_login_attempts (ip_address, success) "
                    "VALUES (%s, FALSE)",
                    (ip,),
                )
                logger.warning("Failed login for '%s' from %s", username, ip)
                try:
                    from . import audit
                    await audit.log(audit.EVENT_LOGIN_FAILURE, user_id=None,
                                    ip_address=ip, metadata={"username": username})
                except Exception:
                    pass
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
                    "AND attempted_at > NOW() - make_interval(mins => %s)",
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
    """Look up user_id from a session token. Returns None if expired/invalid.

    Rolling-refresh: successful lookups extend expires_at by 7 days if
    current expiry is <3 days away, so an active user never gets logged
    out mid-session.
    """
    try:
        async with get_conn_autocommit() as conn:
            token_hash = _hash_token(token)
            row = await (
                await conn.execute(
                    "SELECT user_id, expires_at, created_at FROM companion_auth_sessions "
                    "WHERE token = %s",
                    (token_hash,),
                )
            ).fetchone()
            if row:
                from datetime import timedelta
                from datetime import timezone as tz
                expires = row[1]
                created = row[2] if len(row) > 2 else None
                # Make timestamps timezone-aware for comparison
                now = datetime.now(timezone.utc)
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=tz.utc)
                if created is not None and created.tzinfo is None:
                    created = created.replace(tzinfo=tz.utc)
                # Absolute lifetime cap — a token can't be rolled forward forever.
                if created is not None and now - created > timedelta(days=SESSION_MAX_DAYS):
                    logger.debug("Token exceeded absolute lifetime (%dd) for user %s", SESSION_MAX_DAYS, row[0])
                elif now < expires:
                    # Roll the expiry forward on active use (within 3 days of expiry)
                    if expires - now < timedelta(days=3):
                        try:
                            await conn.execute(
                                "UPDATE companion_auth_sessions "
                                "SET expires_at = NOW() + INTERVAL '7 days' "
                                "WHERE token = %s",
                                (token_hash,),
                            )
                        except Exception:
                            pass  # refresh is best-effort
                    return row[0]
                else:
                    logger.debug("Token expired for user %s", row[0])
    except Exception as e:
        logger.warning("Token lookup failed: %s", e)
    return None


async def get_session_info(token: str) -> dict | None:
    """Return session metadata (expires_at, created_at) for /api/session/info."""
    try:
        async with get_conn() as conn:
            row = await (
                await conn.execute(
                    "SELECT user_id, created_at, expires_at "
                    "FROM companion_auth_sessions WHERE token = %s",
                    (_hash_token(token),),
                )
            ).fetchone()
            if not row:
                return None
            return {
                "user_id": row[0],
                "created_at": row[1].isoformat() if row[1] else None,
                "expires_at": row[2].isoformat() if row[2] else None,
            }
    except Exception as e:
        logger.warning("get_session_info failed: %s", e)
        return None


async def change_password(user_id: str, old_password: str, new_password: str) -> bool:
    """Change a user's password. Requires correct current password.

    Returns True on success. On success, invalidates ALL existing sessions
    for this user (forces logout on all devices).
    """
    if not new_password or len(new_password) < 8:
        return False
    try:
        async with get_conn_autocommit() as conn:
            row = await (
                await conn.execute(
                    "SELECT password_hash FROM companion_users WHERE id = %s",
                    (user_id,),
                )
            ).fetchone()
            if not row or not bcrypt.checkpw(old_password.encode(), row[0].encode()):
                return False
            new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
            await conn.execute(
                "UPDATE companion_users SET password_hash = %s WHERE id = %s",
                (new_hash, user_id),
            )
            # Invalidate all existing sessions for this user
            await conn.execute(
                "DELETE FROM companion_auth_sessions WHERE user_id = %s",
                (user_id,),
            )
            try:
                from . import audit
                await audit.log("password.changed", user_id=user_id)
            except Exception:
                pass
            return True
    except Exception as e:
        logger.error("Password change failed for %s: %s", user_id, e)
        return False


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
