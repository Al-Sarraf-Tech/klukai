# Klukai Telegram Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Telegram bot that gives the project owner instant ops commands, a persistent Claude Code agent for dev work, and push notifications for health/events — all running as a systemd service on dominus.

**Architecture:** Single Python process with three async handlers (ops, agent, notify) sharing one asyncio event loop. Telegram long-polling for inbound, subprocess for shell/Claude CLI, Redis pub/sub for companion-core events.

**Tech Stack:** python-telegram-bot 21+, redis[asyncio], asyncio subprocess, systemd user service

---

## File Structure

| File | Responsibility |
|---|---|
| `telegram/config.py` | Env vars, constants, paths |
| `telegram/ops.py` | Slash commands -> shell execution -> formatted output |
| `telegram/agent.py` | Claude Code session lifecycle, stream parsing, progress forwarding |
| `telegram/notify.py` | Health monitor, Redis subscriber, quiet hours, Telegram push |
| `telegram/bot.py` | Entry point, dispatcher, auth gate, main loop |
| `telegram/requirements.txt` | Dependencies |
| `telegram/.env` | Secrets (not committed) |
| `telegram/klukai-bot.service` | systemd user unit |
| `telegram/tests/test_ops.py` | Ops handler unit tests |
| `telegram/tests/test_agent.py` | Session lifecycle unit tests |
| `telegram/tests/test_notify.py` | Health state machine unit tests |
| `docker/core/app/events.py` | Redis event publisher for companion-core |

---

### Task 1: Project scaffolding + config

**Files:**
- Create: `telegram/config.py`
- Create: `telegram/requirements.txt`
- Create: `telegram/.env`
- Create: `telegram/.gitignore`
- Create: `telegram/__init__.py`
- Create: `telegram/tests/__init__.py`

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p /home/jalsarraf/git/companion/telegram/tests
```

- [ ] **Step 2: Write requirements.txt**

```
python-telegram-bot>=21.0
redis>=5.2.0
httpx>=0.28.0
```

Write to `telegram/requirements.txt`.

- [ ] **Step 3: Write .gitignore**

```
.env
.venv/
__pycache__/
```

Write to `telegram/.gitignore`.

- [ ] **Step 4: Write .env file**

```bash
TELEGRAM_BOT_TOKEN=<ROTATE_AND_SET_LOCALLY>
ALLOWED_USER_IDS=
REDIS_URL=redis://100.111.198.19:16379/1
COMPANION_CORE_URL=http://localhost:8300
COMPANION_VOICE_URL=http://localhost:8301
CLAUDE_CLI=/home/jalsarraf/.local/bin/claude
COMPANION_DIR=/home/jalsarraf/git/companion
```

Write to `telegram/.env` and `chmod 600 telegram/.env`.

Note: `ALLOWED_USER_IDS` will be populated after Telegram pairing in Task 8.

- [ ] **Step 5: Write config.py**

```python
"""Bot configuration — all values from environment."""

from __future__ import annotations

import os

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_IDS: set[int] = {
    int(uid)
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}
REDIS_URL = os.environ.get("REDIS_URL", "redis://100.111.198.19:16379/1")
COMPANION_CORE_URL = os.environ.get("COMPANION_CORE_URL", "http://localhost:8300")
COMPANION_VOICE_URL = os.environ.get("COMPANION_VOICE_URL", "http://localhost:8301")
CLAUDE_CLI = os.environ.get("CLAUDE_CLI", "/home/jalsarraf/.local/bin/claude")
COMPANION_DIR = os.environ.get("COMPANION_DIR", "/home/jalsarraf/git/companion")

# Ops layer validates service names against this set to prevent injection
VALID_SERVICES = {"core", "voice"}

HEALTH_CHECK_INTERVAL = 60  # seconds
HEALTH_FAIL_THRESHOLD = 3   # consecutive failures before alerting
SESSION_IDLE_TIMEOUT = 1800  # 30 min -> recycle session
QUIET_HOUR_START = 23
QUIET_HOUR_END = 8
MAX_TELEGRAM_LENGTH = 4000
```

Write to `telegram/config.py`.

- [ ] **Step 6: Create empty __init__.py files**

Write empty `telegram/__init__.py` and `telegram/tests/__init__.py`.

- [ ] **Step 7: Create venv and install dependencies**

```bash
cd /home/jalsarraf/git/companion/telegram
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

- [ ] **Step 8: Commit**

```bash
git add telegram/config.py telegram/requirements.txt telegram/.gitignore telegram/__init__.py telegram/tests/__init__.py
git commit -m "feat(telegram): scaffold bot project with config and dependencies"
```

---

### Task 2: Ops handler

**Files:**
- Create: `telegram/ops.py`
- Create: `telegram/tests/test_ops.py`

- [ ] **Step 1: Write the test for output truncation and service validation**

```python
"""Tests for ops command handlers."""

from __future__ import annotations

import unittest

from telegram.ops import truncate, format_mono, validate_service


class TestTruncate(unittest.TestCase):
    def test_short_text_unchanged(self):
        assert truncate("hello", 100) == "hello"

    def test_long_text_truncated(self):
        text = "x" * 5000
        result = truncate(text, 4000)
        assert len(result) <= 4000
        assert "truncated" in result

    def test_exact_limit_unchanged(self):
        text = "x" * 4000
        assert truncate(text, 4000) == text


class TestFormatMono(unittest.TestCase):
    def test_wraps_in_code_block(self):
        assert format_mono("hello") == "```\nhello\n```"

    def test_empty_input(self):
        assert format_mono("") == "```\n\n```"


class TestValidateService(unittest.TestCase):
    def test_valid_service(self):
        assert validate_service("core") == "core"
        assert validate_service("voice") == "voice"

    def test_invalid_service_returns_default(self):
        assert validate_service("rm -rf /") == "core"
        assert validate_service("'; DROP TABLE") == "core"


if __name__ == "__main__":
    unittest.main()
```

Write to `telegram/tests/test_ops.py`.

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/jalsarraf/git/companion && telegram/.venv/bin/python -m pytest telegram/tests/test_ops.py -v
```

Expected: FAIL — `telegram.ops` does not exist yet.

- [ ] **Step 3: Write ops.py**

```python
"""Ops command handlers — shell execution with formatted Telegram output."""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from .config import (
    COMPANION_CORE_URL,
    COMPANION_DIR,
    COMPANION_VOICE_URL,
    MAX_TELEGRAM_LENGTH,
    VALID_SERVICES,
)

logger = logging.getLogger(__name__)


def truncate(text: str, limit: int = MAX_TELEGRAM_LENGTH) -> str:
    """Truncate text to Telegram's message limit."""
    if len(text) <= limit:
        return text
    cutoff = limit - 60
    return text[:cutoff] + "\n\n... (truncated, use /logs with fewer lines)"


def format_mono(text: str) -> str:
    """Wrap text in a Telegram monospace code block."""
    return f"```\n{text}\n```"


def validate_service(name: str) -> str:
    """Validate a service name. Returns the name if valid, 'core' otherwise."""
    return name if name in VALID_SERVICES else "core"


async def run_shell(cmd: str, timeout: int = 30) -> tuple[str, int]:
    """Run a shell command and return (output, return_code)."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=COMPANION_DIR,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode() if stdout else ""
        if proc.returncode != 0 and stderr:
            output += f"\nSTDERR:\n{stderr.decode()}"
        return output.strip(), proc.returncode
    except asyncio.TimeoutError:
        proc.kill()
        return "Command timed out", -1


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show Docker container status."""
    out, _ = await run_shell("docker compose ps --format 'table {{.Name}}\t{{.Status}}'")
    await update.message.reply_text(format_mono(truncate(out)), parse_mode="Markdown")


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check health endpoints for core and voice."""
    results = []
    for name, url in [("core", COMPANION_CORE_URL), ("voice", COMPANION_VOICE_URL)]:
        out, rc = await run_shell(f"curl -sf {url}/health", timeout=10)
        status = out if rc == 0 else "UNREACHABLE"
        results.append(f"{name}: {status}")
    await update.message.reply_text(format_mono("\n".join(results)), parse_mode="Markdown")


async def cmd_deploy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Build and restart the companion stack."""
    await update.message.reply_text("Deploying... (build + restart)")
    out, rc = await run_shell("make build && make run", timeout=300)
    status = "Deploy complete" if rc == 0 else "Deploy FAILED"
    await update.message.reply_text(f"{status}\n{format_mono(truncate(out))}", parse_mode="Markdown")


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tail container logs. Usage: /logs [service] [lines]"""
    args = context.args or []
    service = validate_service(args[0]) if args else "core"
    lines = min(int(args[1]), 200) if len(args) > 1 and args[1].isdigit() else 50
    container = f"companion-{service}"
    out, _ = await run_shell(f"docker logs --tail={lines} {container}", timeout=15)
    await update.message.reply_text(format_mono(truncate(out)), parse_mode="Markdown")


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Restart a service. Usage: /restart [service]"""
    args = context.args or []
    service = validate_service(args[0]) if args else "core"
    container = f"companion-{service}"
    await update.message.reply_text(f"Restarting {container}...")
    out, rc = await run_shell(f"docker compose restart {container}", timeout=60)
    status = "Restarted" if rc == 0 else "FAILED"
    await update.message.reply_text(f"{status}\n{format_mono(truncate(out))}", parse_mode="Markdown")


async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Voice service status."""
    out, rc = await run_shell(f"curl -sf {COMPANION_VOICE_URL}/health", timeout=10)
    status = out if rc == 0 else "UNREACHABLE"
    await update.message.reply_text(format_mono(f"voice: {status}"), parse_mode="Markdown")


async def cmd_gateway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check gateway on amarillo."""
    out, rc = await run_shell(
        "ssh -o ConnectTimeout=5 amarillo "
        "'docker ps --format \"table {{.Names}}\\t{{.Status}}\" | grep companion'",
        timeout=15,
    )
    status = out if rc == 0 else "UNREACHABLE or no gateway container"
    await update.message.reply_text(format_mono(truncate(status)), parse_mode="Markdown")


async def cmd_db(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick DB stats via psql."""
    sql = (
        "SELECT 'messages' AS metric, COUNT(*)::text AS val FROM companion_messages "
        "UNION ALL SELECT 'episodes', COUNT(*)::text FROM companion_episodes "
        "UNION ALL SELECT 'affection_score', score::text FROM companion_affection WHERE id=1 "
        "UNION ALL SELECT 'affection_level', level::text FROM companion_affection WHERE id=1"
    )
    out, rc = await run_shell(
        f"docker exec companion-core python3 -c \""
        f"import psycopg,os;"
        f"c=psycopg.connect(os.environ['DATABASE_URL']);"
        f"rows=c.execute('''{sql}''').fetchall();"
        f"print(chr(10).join(f'{{r[0]}}: {{r[1]}}' for r in rows));"
        f"c.close()\"",
        timeout=15,
    )
    if rc != 0:
        out = "Failed to query DB"
    await update.message.reply_text(format_mono(truncate(out)), parse_mode="Markdown")
```

Write to `telegram/ops.py`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /home/jalsarraf/git/companion && telegram/.venv/bin/python -m pytest telegram/tests/test_ops.py -v
```

Expected: 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add telegram/ops.py telegram/tests/test_ops.py
git commit -m "feat(telegram): ops handler — status, health, deploy, logs, restart, voice, gateway, db"
```

---

### Task 3: Agent handler

**Files:**
- Create: `telegram/agent.py`
- Create: `telegram/tests/test_agent.py`

- [ ] **Step 1: Write tests for session lifecycle**

```python
"""Tests for Claude Code agent session management."""

from __future__ import annotations

import time
import unittest

from telegram.agent import AgentSession


class TestAgentSession(unittest.TestCase):
    def test_new_session_has_no_id(self):
        s = AgentSession()
        assert s.session_id is None
        assert not s.is_active

    def test_session_becomes_active(self):
        s = AgentSession()
        s.session_id = "abc-123"
        s.last_activity = time.time()
        assert s.is_active

    def test_session_expires_after_idle(self):
        s = AgentSession(idle_timeout=1)
        s.session_id = "abc-123"
        s.last_activity = time.time() - 2
        assert not s.is_active

    def test_recycle_clears_session(self):
        s = AgentSession()
        s.session_id = "abc-123"
        s.last_activity = time.time()
        s.recycle()
        assert s.session_id is None


if __name__ == "__main__":
    unittest.main()
```

Write to `telegram/tests/test_agent.py`.

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/jalsarraf/git/companion && telegram/.venv/bin/python -m pytest telegram/tests/test_agent.py -v
```

Expected: FAIL — `telegram.agent` does not exist yet.

- [ ] **Step 3: Write agent.py**

```python
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
```

Write to `telegram/agent.py`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /home/jalsarraf/git/companion && telegram/.venv/bin/python -m pytest telegram/tests/test_agent.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add telegram/agent.py telegram/tests/test_agent.py
git commit -m "feat(telegram): agent handler — persistent Claude Code sessions via --resume"
```

---

### Task 4: Notification handler

**Files:**
- Create: `telegram/notify.py`
- Create: `telegram/tests/test_notify.py`

- [ ] **Step 1: Write tests for health state machine**

```python
"""Tests for notification handler — health state machine."""

from __future__ import annotations

import unittest

from telegram.notify import HealthState


class TestHealthState(unittest.TestCase):
    def test_initial_state_is_unknown(self):
        h = HealthState(threshold=3)
        assert h.status == "unknown"

    def test_success_transitions_to_up(self):
        h = HealthState(threshold=3)
        changed = h.record_success()
        assert h.status == "up"
        assert changed  # unknown -> up is a change

    def test_single_failure_does_not_alert(self):
        h = HealthState(threshold=3)
        h.record_success()
        changed = h.record_failure()
        assert h.status == "up"  # still up, threshold not met
        assert not changed

    def test_threshold_failures_transitions_to_down(self):
        h = HealthState(threshold=3)
        h.record_success()
        h.record_failure()
        h.record_failure()
        changed = h.record_failure()
        assert h.status == "down"
        assert changed  # up -> down

    def test_recovery_after_down(self):
        h = HealthState(threshold=3)
        h.record_success()
        for _ in range(3):
            h.record_failure()
        assert h.status == "down"
        changed = h.record_success()
        assert h.status == "up"
        assert changed  # down -> up


if __name__ == "__main__":
    unittest.main()
```

Write to `telegram/tests/test_notify.py`.

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/jalsarraf/git/companion && telegram/.venv/bin/python -m pytest telegram/tests/test_notify.py -v
```

Expected: FAIL — `telegram.notify` does not exist yet.

- [ ] **Step 3: Write notify.py**

```python
"""Notification handler — health monitor, Redis subscriber, quiet hours."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

import httpx
import redis.asyncio as aioredis
from telegram import Bot

from .config import (
    COMPANION_CORE_URL,
    COMPANION_VOICE_URL,
    HEALTH_CHECK_INTERVAL,
    HEALTH_FAIL_THRESHOLD,
    QUIET_HOUR_END,
    QUIET_HOUR_START,
    REDIS_URL,
)

logger = logging.getLogger(__name__)


class HealthState:
    """Tracks health state with threshold-based alerting."""

    def __init__(self, threshold: int = HEALTH_FAIL_THRESHOLD) -> None:
        self.status: str = "unknown"  # unknown, up, down
        self.consecutive_failures: int = 0
        self.threshold = threshold

    def record_success(self) -> bool:
        """Record a successful check. Returns True if state changed."""
        self.consecutive_failures = 0
        old = self.status
        self.status = "up"
        return old != "up"

    def record_failure(self) -> bool:
        """Record a failed check. Returns True if state changed."""
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold:
            old = self.status
            self.status = "down"
            return old != "down"
        return False


def is_quiet_hour() -> bool:
    """Check if current time is in quiet hours."""
    hour = datetime.now().hour
    return QUIET_HOUR_START <= hour or hour < QUIET_HOUR_END


class NotificationManager:
    """Manages health checks and Redis event subscriptions."""

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._health: dict[str, HealthState] = {
            "core": HealthState(),
            "voice": HealthState(),
        }
        self._http: httpx.AsyncClient | None = None
        self._redis: aioredis.Redis | None = None
        self._queued: list[str] = []

    async def start(self) -> None:
        """Start background tasks."""
        self._http = httpx.AsyncClient(timeout=10.0)
        asyncio.create_task(self._health_loop())
        asyncio.create_task(self._redis_loop())
        asyncio.create_task(self._queue_flush_loop())
        logger.info("Notification manager started")

    async def stop(self) -> None:
        if self._http:
            await self._http.aclose()
        if self._redis:
            await self._redis.aclose()

    async def _notify(self, message: str, bypass_quiet: bool = False) -> None:
        """Send a notification, respecting quiet hours."""
        if is_quiet_hour() and not bypass_quiet:
            self._queued.append(message)
            return
        try:
            await self._bot.send_message(chat_id=self._chat_id, text=message)
        except Exception as e:
            logger.error("Failed to send notification: %s", e)

    # ── Health monitor ───────────────────────────────────────────────────

    async def _check_service(self, name: str, url: str) -> None:
        state = self._health[name]
        try:
            r = await self._http.get(f"{url}/health")
            r.raise_for_status()
            if state.record_success():
                await self._notify(f"[UP] {name} is back up", bypass_quiet=True)
        except Exception:
            if state.record_failure():
                await self._notify(
                    f"[DOWN] {name} unreachable ({state.threshold} consecutive failures)",
                    bypass_quiet=True,
                )

    async def _health_loop(self) -> None:
        while True:
            await self._check_service("core", COMPANION_CORE_URL)
            await self._check_service("voice", COMPANION_VOICE_URL)
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)

    # ── Redis subscriber ─────────────────────────────────────────────────

    async def _redis_loop(self) -> None:
        while True:
            try:
                self._redis = aioredis.from_url(REDIS_URL, decode_responses=True)
                pubsub = self._redis.pubsub()
                await pubsub.subscribe("companion:events")
                logger.info("Subscribed to companion:events")

                async for message in pubsub.listen():
                    if message["type"] != "message":
                        continue
                    try:
                        event = json.loads(message["data"])
                        await self._handle_event(event)
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.warning("Bad Redis event: %s", e)

            except Exception as e:
                logger.warning("Redis error: %s — reconnecting in 10s", e)
                await asyncio.sleep(10)

    async def _handle_event(self, event: dict) -> None:
        etype = event.get("type", "")
        data = event.get("data", "")

        if etype == "proactive":
            await self._notify(f"Klukai: {data}")
        elif etype == "affection_change":
            level = event.get("level", "")
            delta = event.get("delta", 0)
            direction = "+" if delta > 0 else ""
            await self._notify(f"Affection {direction}{delta} -> Level {level}")
        elif etype == "error":
            await self._notify(f"[ERROR] {data}", bypass_quiet=True)
        else:
            await self._notify(f"[{etype}] {data}")

    # ── Queue flush ──────────────────────────────────────────────────────

    async def _queue_flush_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            if not is_quiet_hour() and self._queued:
                batch = self._queued.copy()
                self._queued.clear()
                summary = f"Queued during quiet hours ({len(batch)}):\n\n"
                summary += "\n".join(f"- {m}" for m in batch)
                await self._notify(summary, bypass_quiet=True)
```

Write to `telegram/notify.py`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /home/jalsarraf/git/companion && telegram/.venv/bin/python -m pytest telegram/tests/test_notify.py -v
```

Expected: 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add telegram/notify.py telegram/tests/test_notify.py
git commit -m "feat(telegram): notification handler — health monitor, Redis pub/sub, quiet hours"
```

---

### Task 5: Bot entry point with auth gate

**Files:**
- Create: `telegram/bot.py`

- [ ] **Step 1: Write bot.py**

```python
"""Klukai Project Telegram Bot — entry point."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .agent import cmd_session, handle_agent_message
from .config import ALLOWED_USER_IDS, TELEGRAM_BOT_TOKEN
from .notify import NotificationManager
from .ops import (
    cmd_db,
    cmd_deploy,
    cmd_gateway,
    cmd_health,
    cmd_logs,
    cmd_restart,
    cmd_status,
    cmd_voice,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def _is_authorized(update: Update) -> bool:
    """Check if the sender is in the allowlist. Empty list = open (pairing mode)."""
    user = update.effective_user
    if not user:
        return False
    if not ALLOWED_USER_IDS:
        return True  # Open during initial pairing
    return user.id in ALLOWED_USER_IDS


def auth_wrapper(handler):
    """Wrap a handler with auth checking. Unauthorized users are silently ignored."""
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_authorized(update):
            logger.warning(
                "Unauthorized: user=%s id=%s",
                update.effective_user.username,
                update.effective_user.id,
            )
            return
        await handler(update, context)
    return wrapped


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — show welcome + user ID for pairing."""
    user = update.effective_user
    await update.message.reply_text(
        f"Klukai Project Bot\n"
        f"Your Telegram ID: {user.id}\n\n"
        f"Commands:\n"
        f"/status - Container status\n"
        f"/health - Service health\n"
        f"/deploy - Build + restart\n"
        f"/logs [service] [n] - Tail logs\n"
        f"/restart [service] - Restart\n"
        f"/voice - Voice service\n"
        f"/gateway - Gateway (amarillo)\n"
        f"/db - DB stats\n"
        f"/session - Agent session\n\n"
        f"Free text -> Claude Code agent"
    )


async def post_init(application: Application) -> None:
    """Start notification manager after bot is ready."""
    if not ALLOWED_USER_IDS:
        logger.warning("ALLOWED_USER_IDS empty — auth OPEN. Set after pairing.")
        return

    chat_id = next(iter(ALLOWED_USER_IDS))
    notify = NotificationManager(application.bot, chat_id)
    application.bot_data["notify"] = notify
    await notify.start()


def main() -> None:
    """Build and run the bot."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # /start open for pairing
    app.add_handler(CommandHandler("start", cmd_start))

    # All ops commands require auth
    for name, handler in [
        ("status", cmd_status),
        ("health", cmd_health),
        ("deploy", cmd_deploy),
        ("logs", cmd_logs),
        ("restart", cmd_restart),
        ("voice", cmd_voice),
        ("gateway", cmd_gateway),
        ("db", cmd_db),
        ("session", cmd_session),
    ]:
        app.add_handler(CommandHandler(name, auth_wrapper(handler)))

    # Free text -> agent (auth required)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        auth_wrapper(handle_agent_message),
    ))

    logger.info("Starting Klukai Project Bot")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
```

Write to `telegram/bot.py`.

- [ ] **Step 2: Write __main__.py for module execution**

```python
"""Allow running as: python -m telegram.bot"""
from .bot import main

main()
```

Write to `telegram/__main__.py`.

- [ ] **Step 3: Verify syntax**

```bash
cd /home/jalsarraf/git/companion && telegram/.venv/bin/python -c "import ast; ast.parse(open('telegram/bot.py').read()); print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add telegram/bot.py telegram/__main__.py
git commit -m "feat(telegram): bot entry point with auth gate, command routing, agent dispatch"
```

---

### Task 6: Companion-core Redis event publishing

**Files:**
- Create: `docker/core/app/events.py`
- Modify: `docker/core/app/main.py:127-145` (lifespan function)
- Modify: `docker/core/app/proactive.py:269-276` (_deliver method)
- Modify: `docker/core/app/affection.py:196-217` (classify_and_adjust level change)

- [ ] **Step 1: Write events.py**

```python
"""Redis event publisher — companion-core -> Telegram bot notifications."""

from __future__ import annotations

import json
import logging
import os

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://aichat-redis:6379/1")
CHANNEL = "companion:events"

_redis: aioredis.Redis | None = None


async def init() -> None:
    global _redis
    _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    logger.info("Event publisher connected to Redis")


async def close() -> None:
    global _redis
    if _redis:
        await _redis.aclose()
        _redis = None


async def publish(event_type: str, data: str = "", **kwargs) -> None:
    """Publish an event to companion:events channel."""
    if _redis is None:
        return
    event = {"type": event_type, "data": data, **kwargs}
    try:
        await _redis.publish(CHANNEL, json.dumps(event))
    except Exception as e:
        logger.warning("Failed to publish event: %s", e)
```

Write to `docker/core/app/events.py`.

- [ ] **Step 2: Add events init/close to main.py lifespan**

Add import near the top of `docker/core/app/main.py` (alongside other imports from the app package):

```python
from .events import init as events_init, close as events_close
```

In the `lifespan` function, add `await events_init()` after `proactive.start()`, and `await events_close()` after `proactive.stop()` in the shutdown section.

- [ ] **Step 3: Publish proactive events**

Add import to top of `docker/core/app/proactive.py`:

```python
from .events import publish as publish_event
```

In the `_deliver` method, add one line after the callback:

```python
await publish_event("proactive", message)
```

So `_deliver` becomes:

```python
    async def _deliver(self, message: str) -> None:
        if not self._can_send():
            return
        if self._on_message_callback:
            self._proactive_count_today += 1
            self._last_proactive_answered = False
            await self._on_message_callback(message)
            await publish_event("proactive", message)
            logger.info("Klukai proactive: %s", message[:60])
```

- [ ] **Step 4: Publish affection level changes**

Add import to top of `docker/core/app/affection.py`:

```python
from .events import publish as publish_event
```

In `classify_and_adjust`, after `level_direction = "up" if state.level > old_level else "down"` (line ~198), add:

```python
            await publish_event(
                "affection_change",
                f"{state.level_name} (level {state.level})",
                delta=delta, level=state.level, direction=level_direction,
            )
```

- [ ] **Step 5: Run all tests**

```bash
cd /home/jalsarraf/git/companion && telegram/.venv/bin/python -m pytest telegram/tests/ -v
```

Expected: All 16 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add docker/core/app/events.py docker/core/app/main.py docker/core/app/proactive.py docker/core/app/affection.py
git commit -m "feat(core): Redis event publishing for Telegram bot notifications"
```

---

### Task 7: systemd service setup

**Files:**
- Create: `telegram/klukai-bot.service`

- [ ] **Step 1: Write systemd unit**

```ini
[Unit]
Description=Klukai Project Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/jalsarraf/git/companion
ExecStart=/home/jalsarraf/git/companion/telegram/.venv/bin/python -m telegram.bot
EnvironmentFile=/home/jalsarraf/git/companion/telegram/.env
Restart=always
RestartSec=5
StandardOutput=append:/mnt/nvmeINT/logs/klukai-bot.log
StandardError=append:/mnt/nvmeINT/logs/klukai-bot.log

[Install]
WantedBy=default.target
```

Write to `telegram/klukai-bot.service`.

- [ ] **Step 2: Install and enable**

```bash
mkdir -p ~/.config/systemd/user
cp /home/jalsarraf/git/companion/telegram/klukai-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable klukai-bot.service
```

- [ ] **Step 3: Start and verify**

```bash
systemctl --user start klukai-bot.service
systemctl --user status klukai-bot.service
tail -20 /mnt/nvmeINT/logs/klukai-bot.log
```

Expected: Active (running). Log shows `Starting Klukai Project Bot`.

- [ ] **Step 4: Commit**

```bash
git add telegram/klukai-bot.service
git commit -m "feat(telegram): systemd user service — auto-start and crash recovery"
```

---

### Task 8: Telegram pairing and lockdown

**Files:**
- Modify: `telegram/.env`

- [ ] **Step 1: Get Telegram user ID**

DM `@KlukaiProjectBot` on Telegram. Send `/start`. Bot replies with your numeric ID.

- [ ] **Step 2: Set ALLOWED_USER_IDS**

Update `telegram/.env` — set `ALLOWED_USER_IDS=<your-id>`.

- [ ] **Step 3: Restart and verify**

```bash
systemctl --user restart klukai-bot.service
```

DM the bot: `/status`. Should return Docker status. Any other Telegram user should be silently ignored.

- [ ] **Step 4: Smoke test all commands**

```
/status    -> container status table
/health    -> core + voice health JSON
/voice     -> voice health specifically
/session   -> "No active session."
/logs core 10 -> last 10 log lines
```

Send free text: "what files are in the telegram directory?" — should trigger Claude Code agent.

- [ ] **Step 5: Verify notifications**

```bash
grep -i "notification\|subscribed\|health" /mnt/nvmeINT/logs/klukai-bot.log | tail -10
```

Expected: Health check and Redis subscription entries.
