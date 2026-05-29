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
    """Check if the sender is in the allowlist.

    Fail CLOSED: an empty/unset allowlist denies everyone. This bot can run
    privileged host operations (/deploy, /restart, /db) and forwards free text
    to a Claude Code agent, so an empty ALLOWED_USER_IDS is a misconfiguration,
    never an invitation to authorize the whole world.
    """
    user = update.effective_user
    if not user:
        return False
    if not ALLOWED_USER_IDS:
        logger.error(
            "ALLOWED_USER_IDS is empty — denying all requests (fail-closed). "
            "Set ALLOWED_USER_IDS to enable the bot."
        )
        return False
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
        logger.warning(
            "ALLOWED_USER_IDS empty — bot is DENYING all requests (fail-closed) "
            "and notifications are disabled. Set ALLOWED_USER_IDS to enable."
        )
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
