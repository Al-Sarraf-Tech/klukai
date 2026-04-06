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
