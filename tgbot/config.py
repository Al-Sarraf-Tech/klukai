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
COMPANION_DIR = os.environ.get("COMPANION_DIR", "/home/jalsarraf/git/klukai")

# Ops layer validates service names against this set to prevent injection
VALID_SERVICES = {"core", "voice"}

HEALTH_CHECK_INTERVAL = 60  # seconds
HEALTH_FAIL_THRESHOLD = 3   # consecutive failures before alerting
SESSION_IDLE_TIMEOUT = 1800  # 30 min -> recycle session
QUIET_HOUR_START = 23
QUIET_HOUR_END = 8
MAX_TELEGRAM_LENGTH = 4000
