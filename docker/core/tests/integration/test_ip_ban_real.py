"""Real end-to-end IP-ban test against live Postgres.

Proves the 3-strikes login ban actually fires. This is the path that silently
broke when ``check_ip_banned`` used the ``INTERVAL '%s'`` bind anti-pattern:
psycopg3 could not bind the window parameter, the ``COUNT(*)`` raised at
execute time, and the swallowing ``try/except`` returned "not banned" forever.
The mocked unit tests (which stub the DB connection) could never catch this —
only a real DB round-trip exercises the ``make_interval(mins => %s)`` bind.

Autoskips without a reachable stack (see ``conftest._backends_reachable``).

Only synthetic RFC-5737 test IPs are touched, and only those rows are cleaned
up — no SACRED data (chat/episodes/affection/vectors) is read or deleted.
"""

from __future__ import annotations

import secrets

import pytest

from app import auth
from app.db import get_conn_autocommit, init_pool


def _synthetic_ip() -> str:
    # TEST-NET-3 (203.0.113.0/24, RFC 5737) — reserved for docs/tests, never a
    # real client, so it can't collide with genuine login-attempt rows.
    return f"203.0.113.{secrets.randbelow(254) + 1}"


async def _purge(ip: str) -> None:
    async with get_conn_autocommit() as conn:
        await conn.execute(
            "DELETE FROM companion_login_attempts WHERE ip_address = %s", (ip,)
        )


@pytest.mark.integration
class TestIpBanRealDB:
    async def test_three_failed_logins_trigger_ip_ban(
        self, test_user_id: str, _create_test_user
    ) -> None:
        await init_pool()
        ip = _synthetic_ip()
        await _purge(ip)
        try:
            # First two failures: under the threshold of 3 — not banned yet.
            assert await auth.authenticate(test_user_id, "wrong-pw", ip) is None
            assert await auth.authenticate(test_user_id, "wrong-pw", ip) is None
            assert await auth.check_ip_banned(ip) is False, "2 failures must not ban"

            # Third failure crosses IP_BAN_THRESHOLD (3) inside the window.
            assert await auth.authenticate(test_user_id, "wrong-pw", ip) is None
            assert await auth.check_ip_banned(ip) is True, "3 failures must ban"

            # Isolation: a different IP is unaffected.
            other = _synthetic_ip()
            await _purge(other)
            assert await auth.check_ip_banned(other) is False
            await _purge(other)
        finally:
            await _purge(ip)

    async def test_successful_login_does_not_count_toward_ban(
        self, test_user_id: str, test_password: str, _create_test_user
    ) -> None:
        await init_pool()
        ip = _synthetic_ip()
        await _purge(ip)
        try:
            token = await auth.authenticate(test_user_id, test_password, ip)
            assert token, "valid credentials must succeed"
            assert await auth.check_ip_banned(ip) is False
        finally:
            await _purge(ip)
