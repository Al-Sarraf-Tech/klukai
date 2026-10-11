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

# Minimum gap between progress messages so a chatty agent does not trip
# Telegram's per-chat rate limit. The final result is never throttled.
PROGRESS_INTERVAL = 2.0


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

# One agent run at a time: two overlapping runs would share (and clobber) the
# session above, and the second would race the first for --resume.
_run_lock = asyncio.Lock()


def get_session() -> AgentSession:
    return _session


def _build_command(prompt: str) -> list[str]:
    """Build the Claude CLI command.

    ``--verbose`` is required: in print mode the CLI refuses
    ``--output-format stream-json`` without it and exits non-zero before
    producing any events.
    """
    cmd = [
        CLAUDE_CLI,
        "-p", prompt,
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",
    ]
    if _session.is_active and _session.session_id:
        cmd.extend(["--resume", _session.session_id])
    return cmd


def _extract_session_id(event: dict) -> str | None:
    """Extract session ID from a stream-json event."""
    sid = event.get("session_id")
    return sid if isinstance(sid, str) and sid else None


def _should_forward(event: dict) -> tuple[bool, str]:
    """Decide whether a stream-json event should be forwarded to Telegram.

    Returns (should_forward, formatted_text).

    Assistant turns carry their content blocks under ``event["message"]``
    (the Anthropic message object); a bare top-level ``content`` is accepted
    too for older emitters. All text blocks of one turn are joined so a
    multi-block answer is not cut to its first paragraph.
    """
    etype = event.get("type", "")

    if etype == "assistant":
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else event.get("content")
        if isinstance(content, list):
            texts = [
                block["text"]
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ]
            text = "\n\n".join(t for t in texts if t.strip())
            if text:
                return True, text
        return False, ""

    if etype == "result":
        text = event.get("result", "")
        if isinstance(text, str) and text:
            return True, text

    return False, ""


async def _send_chunk(message, text: str) -> None:
    """Send one piece of agent output, never failing the run over a send."""
    snippet = text[:MAX_TELEGRAM_LENGTH]
    try:
        await message.reply_text(snippet)
    except Exception as e:
        logger.warning("Failed to send progress: %s", e)


async def _run_agent(message, prompt: str) -> None:
    # Check for idle recycling
    if not _session.is_active and _session.session_id:
        logger.info("Session %s expired, recycling", _session.session_id)
        _session.recycle()

    # Immediate ack
    ack = await message.reply_text("Working on it...")

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

        # Drain stderr concurrently. Reading it only after stdout hits EOF
        # deadlocks once the child fills the stderr pipe and blocks on write.
        stderr_task = asyncio.create_task(proc.stderr.read())

        last_update_time = 0.0
        pending_text = ""  # most recent text that was throttled, not yet sent
        while True:
            line = await proc.stdout.readline()
            if not line:
                break

            try:
                event = json.loads(line.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue

            sid = _extract_session_id(event)
            if sid:
                _session.session_id = sid
                _session.touch()

            should_fwd, text = _should_forward(event)
            if not (should_fwd and text.strip()):
                continue

            # Throttle progress to one message per PROGRESS_INTERVAL; the
            # final result always goes out, replacing any held progress.
            now = time.time()
            is_result = event.get("type") == "result"
            if is_result or now - last_update_time >= PROGRESS_INTERVAL:
                last_update_time = now
                pending_text = ""
                await _send_chunk(message, text)
            else:
                pending_text = text

        await proc.wait()
        stderr_bytes = await stderr_task
        elapsed = time.time() - start_time

        # Flush the last throttled progress line so the user sees how it ended.
        if pending_text.strip():
            await _send_chunk(message, pending_text)

        status = "Done" if proc.returncode == 0 else f"Exited with code {proc.returncode}"
        summary = f"{status} ({elapsed:.0f}s)"

        if proc.returncode != 0 and stderr_bytes:
            err_text = stderr_bytes.decode(errors="replace")[:1000]
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


async def handle_agent_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a free-text message by routing it to Claude Code."""
    message = update.effective_message
    if message is None or not message.text:
        return

    if _run_lock.locked():
        await message.reply_text("Agent is busy with the previous request. Try again when it finishes.")
        return

    async with _run_lock:
        await _run_agent(message, message.text)


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current agent session info."""
    message = update.effective_message
    if message is None:
        return
    s = _session
    if not s.session_id:
        await message.reply_text("No active session.")
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
    await message.reply_text("```\n" + "\n".join(lines) + "\n```", parse_mode="Markdown")
