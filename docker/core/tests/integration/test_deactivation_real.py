"""Real end-to-end test that account deactivation actually revokes access.

The audit found ``/api/account/deactivate`` set ``deactivated_at`` but nothing
ever read it: ``authenticate()`` had no ``deactivated_at IS NULL`` filter and
``get_user_from_token`` never joined to check it, so a "deactivated" user logged
straight back in and a still-live token kept working. Mocked unit tests can't
catch this — only a real DB round-trip exercises the SQL filter.

Touches only the synthetic ``itest_*`` user and restores it afterward; no SACRED
data is read or deleted.
"""
from __future__ import annotations

import pytest

from app import auth
from app.db import get_conn_autocommit, init_pool

TEST_IP = "203.0.113.200"  # RFC 5737 TEST-NET-3 — never a real client


async def _purge_attempts(ip: str) -> None:
    async with get_conn_autocommit() as conn:
        await conn.execute(
            "DELETE FROM companion_login_attempts WHERE ip_address = %s", (ip,)
        )


async def _set_deactivated_now(user_id: str) -> None:
    async with get_conn_autocommit() as conn:
        await conn.execute(
            "UPDATE companion_users SET deactivated_at = NOW() WHERE id = %s", (user_id,)
        )


async def _reactivate(user_id: str) -> None:
    async with get_conn_autocommit() as conn:
        await conn.execute(
            "UPDATE companion_users SET deactivated_at = NULL WHERE id = %s", (user_id,)
        )


@pytest.mark.integration
class TestDeactivationEnforced:
    async def test_deactivated_user_is_locked_out_then_restored(
        self, test_user_id: str, test_password: str, _create_test_user
    ) -> None:
        await init_pool()
        await _purge_attempts(TEST_IP)
        try:
            # Active: correct credentials authenticate and the token validates.
            tok = await auth.authenticate(test_user_id, test_password, TEST_IP)
            assert tok, "active user must authenticate"
            assert await auth.get_user_from_token(tok) == test_user_id

            # Deactivate → login refused even with correct creds, token revoked.
            await _set_deactivated_now(test_user_id)
            try:
                assert await auth.authenticate(test_user_id, test_password, TEST_IP) is None, \
                    "deactivated user must NOT authenticate"
                assert await auth.get_user_from_token(tok) is None, \
                    "deactivated user's existing token must stop working"
            finally:
                # Restore so the shared session-scoped user works for other tests.
                await _reactivate(test_user_id)

            # Reactivated → access is restored.
            assert await auth.get_user_from_token(tok) == test_user_id
        finally:
            await _purge_attempts(TEST_IP)
