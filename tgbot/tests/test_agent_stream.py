"""Tests for the stream-json bridge between Claude Code and Telegram.

Regression guards:
- assistant text lives under event["message"]["content"]; the old reader
  looked for a top-level "content" and so never forwarded progress.
- the progress throttle must defer, not drop, and must never hold the
  final result.
- print mode needs --verbose for stream-json, or the CLI exits at once.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-real")

from tgbot import agent  # noqa: E402


def _assistant(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


class TestShouldForward(unittest.TestCase):
    def test_assistant_text_under_message_is_forwarded(self):
        self.assertEqual(agent._should_forward(_assistant("hello")), (True, "hello"))

    def test_multiple_text_blocks_are_joined(self):
        event = {
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "one"},
                {"type": "tool_use", "name": "Bash"},
                {"type": "text", "text": "two"},
            ]},
        }
        self.assertEqual(agent._should_forward(event), (True, "one\n\ntwo"))

    def test_tool_use_only_is_not_forwarded(self):
        event = {"type": "assistant", "message": {"content": [{"type": "tool_use"}]}}
        self.assertEqual(agent._should_forward(event), (False, ""))

    def test_legacy_top_level_content_still_works(self):
        event = {"type": "assistant", "content": [{"type": "text", "text": "legacy"}]}
        self.assertEqual(agent._should_forward(event), (True, "legacy"))

    def test_result_is_forwarded(self):
        self.assertEqual(agent._should_forward({"type": "result", "result": "done"}), (True, "done"))

    def test_system_init_is_ignored(self):
        self.assertEqual(agent._should_forward({"type": "system"}), (False, ""))


class TestBuildCommand(unittest.TestCase):
    def test_stream_json_requires_verbose(self):
        cmd = agent._build_command("hi")
        self.assertIn("--verbose", cmd)
        self.assertIn("stream-json", cmd)

    def test_resume_only_when_session_active(self):
        s = agent.get_session()
        s.recycle()
        self.assertNotIn("--resume", agent._build_command("hi"))
        s.session_id = "sid-1"
        s.touch()
        try:
            cmd = agent._build_command("hi")
            self.assertEqual(cmd[cmd.index("--resume") + 1], "sid-1")
        finally:
            s.recycle()


class _FakeStream:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    async def read(self) -> bytes:
        return b""


class _FakeProc:
    def __init__(self, lines: list[bytes], returncode: int = 0) -> None:
        self.stdout = _FakeStream(lines)
        self.stderr = _FakeStream([])
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode


class _FakeMessage:
    def __init__(self, text: str = "do the thing") -> None:
        self.text = text
        self.sent: list[str] = []
        self.ack = mock.AsyncMock()

    async def reply_text(self, text: str, **_: object):
        self.sent.append(text)
        return self.ack


def _update(message: _FakeMessage):
    return mock.Mock(effective_message=message)


def _lines(*events: dict) -> list[bytes]:
    return [json.dumps(e).encode() + b"\n" for e in events]


class TestRunAgent(unittest.IsolatedAsyncioTestCase):
    async def _run(self, message, lines, returncode=0):
        with mock.patch.object(
            agent.asyncio, "create_subprocess_exec",
            new=mock.AsyncMock(return_value=_FakeProc(lines, returncode)),
        ):
            await agent.handle_agent_message(_update(message), context=None)

    async def test_result_is_never_throttled_and_replaces_held_progress(self):
        message = _FakeMessage()
        agent.get_session().recycle()
        await self._run(message, _lines(
            {"type": "system", "session_id": "s-42"},
            _assistant("p1"), _assistant("p2"), _assistant("p3"),
            {"type": "result", "result": "final answer", "session_id": "s-42"},
        ))
        self.assertEqual(message.sent, ["Working on it...", "p1", "final answer"])
        self.assertEqual(agent.get_session().session_id, "s-42")
        self.assertTrue(message.ack.edit_text.await_args.args[0].startswith("Done"))
        agent.get_session().recycle()

    async def test_last_throttled_progress_is_flushed_when_no_result_follows(self):
        message = _FakeMessage()
        await self._run(message, _lines(_assistant("p1"), _assistant("p2")), returncode=1)
        self.assertEqual(message.sent[1:], ["p1", "p2"])
        self.assertTrue(message.ack.edit_text.await_args.args[0].startswith("Exited with code 1"))

    async def test_overlapping_message_is_refused(self):
        message = _FakeMessage()
        async with agent._run_lock:
            await agent.handle_agent_message(_update(message), context=None)
        self.assertEqual(len(message.sent), 1)
        self.assertIn("busy", message.sent[0])

    async def test_message_without_text_is_ignored(self):
        message = _FakeMessage(text="")
        await agent.handle_agent_message(_update(message), context=None)
        self.assertEqual(message.sent, [])


if __name__ == "__main__":
    unittest.main()
