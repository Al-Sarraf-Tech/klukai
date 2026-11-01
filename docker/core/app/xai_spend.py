"""Shared xAI spend math and the daily USD ledger.

Chat (core) and pictures (the host image worker) both write this file, so a
cap is one number. The API key is never read from disk here and never written
into the ledger or a log line.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import UTC, datetime
import fcntl
import logging
import os
from pathlib import Path
import re
import sqlite3
from collections.abc import Iterator

logger = logging.getLogger(__name__)

CHAT_MODEL = "grok-4.7"
# Text models on https://docs.x.ai/developers/models as of 2026-10-31.
# Grok 4.7 Fast is the same model on faster infrastructure, and the docs
# say it is not on the public API. Image and voice slugs are not chat.
DOCUMENTED_CHAT_MODELS = frozenset(
    {
        "grok-4.7",
        "grok-4.6",
        "grok-4.5",
        "grok-4.3",
        "grok-4.20-0309-reasoning",
        "grok-4.20-0309-non-reasoning",
        "grok-build-0.1",
        "grok-4.20-multi-agent-0309",
    }
)
# The documented non-reasoning chat slug. There is no public fast slug.
FASTER_CHAT_CANDIDATE = "grok-4.20-0309-non-reasoning"
IMAGE_MODEL = "grok-imagine-image-2.0"
CHAT_URL = "https://api.x.ai/v1/chat/completions"
IMAGE_EDIT_URL = "https://api.x.ai/v1/images/edits"
# xAI: 1 USD = 10^10 ticks. Live 2k edits were about $0.09; a vision check about $0.007.
USD_TICKS = 10_000_000_000
IMAGE_USD = 0.09
CHECK_USD = 0.007

_KEY = re.compile(r"xai-[A-Za-z0-9]{8,}")
_KINDS = frozenset({"chat", "image"})


def api_key() -> str:
    return os.environ.get("KLUKAI_XAI_API_KEY", "").strip()


def _choice(name: str) -> str:
    """``api`` when the flag says so, or when it is unset and a key is present."""
    raw = os.environ.get(name, "").strip().lower()
    if raw in {"api", "cli"}:
        return raw
    return "api" if api_key() else "cli"


def chat_model() -> str:
    """Chat model from ``KLUKAI_CHAT_MODEL``. Unknown slugs stay on grok-4.7.

    Story and summary calls do not use this. They stay on ``CHAT_MODEL``.
    """
    raw = os.environ.get("KLUKAI_CHAT_MODEL", "").strip()
    if not raw or raw == CHAT_MODEL:
        return CHAT_MODEL
    if raw not in DOCUMENTED_CHAT_MODELS:
        logger.warning(
            "KLUKAI_CHAT_MODEL is not a documented chat model; using grok-4.7"
        )
        return CHAT_MODEL
    return raw


def chat_backend() -> str:
    return _choice("KLUKAI_CHAT_BACKEND")


def image_backend() -> str:
    return _choice("KLUKAI_IMAGE_BACKEND")


def redact(text: str) -> str:
    """Drop the live key and any xAI key-shaped token from an error string."""
    key = api_key()
    if key:
        text = text.replace(key, "[redacted]")
    return _KEY.sub("[redacted]", text)


def _positive(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def rates() -> tuple[float, float, float, float]:
    """USD per million input, cached input, output, and USD per image."""
    return (
        _positive("KLUKAI_XAI_USD_PER_M_INPUT", 2.0),
        _positive("KLUKAI_XAI_USD_PER_M_CACHED", 0.5),
        _positive("KLUKAI_XAI_USD_PER_M_OUTPUT", 6.0),
        _positive("KLUKAI_XAI_USD_PER_IMAGE", IMAGE_USD),
    )


def chat_usd(input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    """Price the three billed token classes. Reasoning tokens are inside output."""
    per_input, per_cached, per_output, _per_image = rates()
    fresh_in = max(0, input_tokens)
    cached = min(max(0, cached_tokens), fresh_in)
    fresh = fresh_in - cached
    output = max(0, output_tokens)
    return (fresh * per_input + cached * per_cached + output * per_output) / 1_000_000


def image_usd() -> float:
    return rates()[3]


def ticks_usd(ticks: object) -> float | None:
    """The API's billed cost. Reasoning tokens are already inside it."""
    if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 0:
        return None
    return ticks / USD_TICKS


def daily_cap() -> float:
    raw = os.environ.get("KLUKAI_API_DAILY_USD_CAP", "3").strip()
    try:
        value = float(raw)
    except ValueError:
        return 3.0
    return value if value >= 0 else 3.0


def ledger_path() -> Path:
    override = os.environ.get("KLUKAI_XAI_LEDGER", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".local" / "share" / "klukai" / "xai-spend.sqlite3"


def _day(now: datetime | None) -> str:
    moment = datetime.now(UTC) if now is None else now
    return moment.astimezone(UTC).strftime("%Y-%m-%d")


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(path.parent / ".spend.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    os.chmod(path, 0o600)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS xai_spend ("
        "day TEXT NOT NULL, kind TEXT NOT NULL, usd REAL NOT NULL, "
        "recorded_at TEXT NOT NULL)"
    )
    return conn


def spent_today(now: datetime | None = None) -> float:
    path = ledger_path()
    if not path.is_file():
        return 0.0
    day = _day(now)
    with _lock(path), closing(_db(path)) as db:
        row = db.execute(
            "SELECT COALESCE(SUM(usd), 0) FROM xai_spend WHERE day = ?", (day,)
        ).fetchone()
    return float(row[0] if row else 0.0)


def record(kind: str, usd: float, now: datetime | None = None) -> float:
    """Append one charge and return today's total, including this charge."""
    if kind not in _KINDS or usd < 0:
        raise ValueError("invalid xai spend")
    path = ledger_path()
    day = _day(now)
    with _lock(path), closing(_db(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO xai_spend VALUES (?, ?, ?, ?)",
            (day, kind, usd, datetime.now(UTC).isoformat()),
        )
        row = db.execute(
            "SELECT COALESCE(SUM(usd), 0) FROM xai_spend WHERE day = ?", (day,)
        ).fetchone()
        db.execute("COMMIT")
    return float(row[0] if row else usd)


def capped(now: datetime | None = None) -> bool:
    return spent_today(now) >= daily_cap()


def log_use(
    kind: str,
    *,
    model: str,
    input_tokens: int | None = None,
    cached_tokens: int | None = None,
    output_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    ttft_s: float | None = None,
    total_s: float | None = None,
    usd: float = 0.0,
    daily_usd: float | None = None,
) -> None:
    """One line a operator can add up. No prompt, no key, no image bytes."""

    def ms(value: float | None) -> str:
        return "-" if value is None else str(int(value * 1000))

    def num(value: int | None) -> str:
        return "-" if value is None else str(value)

    logger.info(
        "xai %s model=%s input=%s cached=%s output=%s reasoning=%s "
        "ttft_ms=%s total_ms=%s usd=%.6f daily_usd=%s",
        kind,
        model,
        num(input_tokens),
        num(cached_tokens),
        num(output_tokens),
        num(reasoning_tokens),
        ms(ttft_s),
        ms(total_s),
        usd,
        "-" if daily_usd is None else f"{daily_usd:.6f}",
    )
