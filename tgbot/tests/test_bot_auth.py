"""Tests for bot authorization — must FAIL CLOSED on an empty allowlist.

Regression guard: an empty/unset ALLOWED_USER_IDS previously authorized every
Telegram user, exposing privileged host ops (/deploy, /restart, /db) and the
Claude Code agent bridge to the world.
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

# config.py requires TELEGRAM_BOT_TOKEN at import; provide a dummy for tests.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-real")

try:
    import tgbot.bot as bot

    _HAVE_TELEGRAM = True
except Exception:  # python-telegram-bot not installed in this environment
    _HAVE_TELEGRAM = False


def _update(user_id: int | None) -> SimpleNamespace:
    if user_id is None:
        return SimpleNamespace(effective_user=None)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id, username="tester")
    )


@unittest.skipUnless(_HAVE_TELEGRAM, "python-telegram-bot not installed")
class TestBotAuthorization(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = bot.ALLOWED_USER_IDS

    def tearDown(self) -> None:
        bot.ALLOWED_USER_IDS = self._orig

    def test_empty_allowlist_denies_everyone(self) -> None:
        bot.ALLOWED_USER_IDS = set()
        self.assertFalse(bot._is_authorized(_update(999)))

    def test_user_in_allowlist_is_authorized(self) -> None:
        bot.ALLOWED_USER_IDS = {123}
        self.assertTrue(bot._is_authorized(_update(123)))

    def test_user_not_in_allowlist_is_denied(self) -> None:
        bot.ALLOWED_USER_IDS = {123}
        self.assertFalse(bot._is_authorized(_update(999)))

    def test_no_effective_user_is_denied(self) -> None:
        bot.ALLOWED_USER_IDS = {123}
        self.assertFalse(bot._is_authorized(_update(None)))


if __name__ == "__main__":
    unittest.main()
