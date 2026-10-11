"""reply_mono must degrade to plain text when Telegram rejects the Markdown."""

from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-real")

from telegram.error import BadRequest  # noqa: E402

from tgbot import ops  # noqa: E402


class TestReplyMono(unittest.IsolatedAsyncioTestCase):
    async def test_markdown_sent_first(self):
        message = mock.AsyncMock()
        await ops.reply_mono(message, "line")
        message.reply_text.assert_awaited_once_with("```\nline\n```", parse_mode="Markdown")

    async def test_falls_back_to_plain_text_on_bad_request(self):
        message = mock.AsyncMock()
        message.reply_text.side_effect = [BadRequest("Can't parse entities"), None]
        await ops.reply_mono(message, "has ` backtick", prefix="Restarted")
        self.assertEqual(message.reply_text.await_count, 2)
        second = message.reply_text.await_args_list[1]
        self.assertEqual(second.args, ("Restarted\nhas ` backtick",))
        self.assertNotIn("parse_mode", second.kwargs)

    async def test_long_output_is_truncated_before_formatting(self):
        message = mock.AsyncMock()
        await ops.reply_mono(message, "x" * 10_000)
        sent = message.reply_text.await_args.args[0]
        self.assertLessEqual(len(sent), ops.MAX_TELEGRAM_LENGTH + len("```\n\n```"))
        self.assertIn("truncated", sent)


class TestRunShellTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_timed_out_child_is_killed_and_reaped(self):
        out, rc = await ops.run_shell("sleep 5", timeout=0.2)
        self.assertEqual((out, rc), ("Command timed out", -1))


if __name__ == "__main__":
    unittest.main()
