"""Claude Code agent session management — persistent sessions via --resume."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from telegram import Update
from telegram.ext import ContextTypes

from .config import CLAUDE_CLI, COMPANION_DIR, MAX_TELEGRAM_LENGTH, SESSION_IDLE_TIMEOUT

logger = logging.getLogger(__name__)


class AgentSession:
    """Tracks a persistent Claude Code session."""

    def __init__(self, idle_timeout: int = SESSION_IDLE_TIMEOUT) -> None:
        self.session_id: str | None = None
        self.last_activity: float = 0.0
        self.idle_timeout = idle_timeout
        self.process: asyncio.subprocess.Process | None = None

    @property
    def is_active(self) -> bool:
        if self.session_id is None:
            return False
        return (time.time() - self.last_activity) < self.idle_timeout

    def recycle(self) -> None:
        self.session_id = None
        self.last_activity = 0.0
        self.process = None

    def touch(self) -> None:
        self.last_activity = time.time()


# Module-level singleton
_session = AgentSession()


def get_session() -> AgentSession:
    return _session


def _build_command(prompt: str) -> list[str]:
    """Build the Claude CLI command."""
    cmd = [
        CLAUDE_CLI,
        "-p", prompt,
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
    ]
    if _session.is_active and _session.session_id:
        cmd.extend(["--resume", _session.session_id])
    return cmd


def _extract_session_id(event: dict) -> str | None:
    """Extract session ID from a stream-json event."""
    if "session_id" in event:
        return event["session_id"]
    return None


def _should_forward(event: dict) -> tuple[bool, str]:
    """Decide whether a stream-json event should be forwarded to Telegram.

    Returns (should_forward, formatted_text).
    """
    etype = event.get("type", "")

    # Assistant text messages — always forward
    if etype == "assistant" and "content" in event:
        for block in event["content"]:
            if block.get("type") == "text":
                return True, block["text"]

    # Result messages
    if etype == "result":
        text = event.get("result", "")
        if text:
            return True, text

    return False, ""


async def handle_agent_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a free-text message by routing it to Claude Code."""
    prompt = update.message.text
    if not prompt:
        return

    # Check for idle recycling
    if not _session.is_active and _session.session_id:
        logger.info("Session %s expired, recycling", _session.session_id)
        _session.recycle()

    # Immediate ack
    ack = await update.message.reply_text("Working on it...")

    cmd = _build_command(prompt)
    start_time = time.time()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=COMPANION_DIR,
        )
        _session.process = proc
        _session.touch()

        # Read stream-json output line by line
        last_update_time = 0.0
        last_text = ""
        while True:
            line = await proc.stdout.readline()
            if not line:
                break

            try:
                event = json.loads(line.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            # Capture session ID
            sid = _extract_session_id(event)
            if sid:
                _session.session_id = sid
                _session.touch()

            # Forward meaningful events
            should_fwd, text = _should_forward(event)
            if should_fwd and text.strip():
                last_text = text
                # Throttle updates to avoid Telegram rate limits (max every 2s)
                now = time.time()
                if now - last_update_time >= 2.0:
                    last_update_time = now
                    snippet = text[:MAX_TELEGRAM_LENGTH]
                    try:
                        await update.message.reply_text(snippet)
                    except Exception as e:
                        logger.warning("Failed to send progress: %s", e)

        await proc.wait()
        elapsed = time.time() - start_time

        # Final summary
        status = "Done" if proc.returncode == 0 else f"Exited with code {proc.returncode}"
        summary = f"{status} ({elapsed:.0f}s)"

        # Include last meaningful output if we haven't sent it
        stderr_bytes = await proc.stderr.read()
        if proc.returncode != 0 and stderr_bytes:
            err_text = stderr_bytes.decode()[:1000]
            summary += f"\n```\n{err_text}\n```"

        try:
            await ack.edit_text(summary, parse_mode="Markdown")
        except Exception:
            await ack.edit_text(summary)

    except Exception as e:
        logger.error("Agent error: %s", e)
        try:
            await ack.edit_text(f"Agent error: {e}")
        except Exception:
            pass
        _session.recycle()

    finally:
        _session.process = None
        _session.touch()


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current agent session info."""
    s = _session
    if not s.session_id:
        await update.message.reply_text("No active session.")
        return

    age = time.time() - s.last_activity
    active = s.is_active
    running = s.process is not None and s.process.returncode is None

    lines = [
        f"Session: {s.session_id[:12]}...",
        f"Active: {active}",
        f"Running: {running}",
        f"Idle: {age:.0f}s / {s.idle_timeout}s",
    ]
    await update.message.reply_text("```\n" + "\n".join(lines) + "\n```", parse_mode="Markdown")
