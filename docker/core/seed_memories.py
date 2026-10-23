#!/usr/bin/env python3
"""Retired automatic album seeder.

The service entry point is a no-op under the on-demand-only picture policy.
Legacy pure helpers remain for compatibility; no history, watermark or album
records are changed by running this script.
"""

import asyncio
import json
import logging
import os
import re
import signal
import time
import uuid
from datetime import datetime
from typing import Any

import httpx

from app.lm_gateway import LM_TTL_SECONDS, lm_studio_auth_headers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://100.107.121.5:1234")

# gpt-oss-20b for selection: reliable structured JSON at low temperature
SELECTOR_MODEL = "cognitivecomputations_dolphin-mistral-24b-venice-edition"
# dolphin-24b for annotation: clean creative text, no chain-of-thought leakage
ANNOTATOR_MODEL = "cognitivecomputations_dolphin-mistral-24b-venice-edition"

BATCH_SIZE = 5
DEFAULT_MAX_EXCHANGES = 40

# Account-worker pipeline (owner order 2026-10-19: Dominus-free, and never
# eat his Grok quota). Renders per run AND per local day; 0 (default) = off.
RENDERS_ENV = "KLUKAI_ALBUM_SEED_RENDERS"
MAX_RENDERS_PER_RUN = 2
ALBUM_KIND = "album_seed"
_SEED_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "klukai:album-seed:v1")
# Album pictures are always covered: these never reach the account brief.
_UNCOVERED_TAG = re.compile(
    r"\b(lingerie|underwear|nude|nudity|naked|topless|undress\w*|bath\w*|onsen|"
    r"hot springs?|shower\w*|skinny[- ]dip\w*|intimate|seductive|sexy)\b",
    re.IGNORECASE,
)

# ── Prompts ──────────────────────────────────────────────────────────────────

SELECTION_PROMPT = """\
You are Klukai reviewing past conversations with the Commander for your memory archive.
The Commander is HUMAN (male), NOT a T-Doll. You are affection level 8 (Bonded).

Below are {count} exchanges. For each one worth preserving, return its index, category, and image tags.
Be selective — only truly meaningful moments. Skip trivial greetings and small talk.

Categories: Tactical Operations, Mission Records, Squad Moments, The Commander, Quiet Hours, Precious Memories

image_tags must describe the SCENE for the image:
- WHERE: bed, cafe, rooftop, motorcycle, training, beach, office, forest, rain, home, kitchen
- MOOD: tender, playful, serious, romantic, sad, peaceful, passionate, determined
- ACTION: hug, kiss, holding hands, sitting, cooking, fighting, sleeping, riding, talking
- WHO: couple (if Commander present), squad (if squad members mentioned)
- OUTFIT: casual, date, training, uniform, home, sleep, lingerie

Return ONLY valid JSON: {{"memories": [{{"index": 0, "category": "...", "image_tags": ["tag1", "tag2", "tag3"]}}]}}
If none worth keeping: {{"memories": []}}

Exchanges:
{exchanges}
"""

ANNOTATION_PROMPT = """\
You are Klukai writing in your private journal. This is a memory you chose to keep.
The Commander is HUMAN — the man you've bonded with. NOT a T-Doll.
You're at affection level 8 — deeply bonded. You grew from a cold, prideful soldier
into someone who can admit she cares.

Write 3-5 sentences about this memory. This is YOUR private journal — take your time.
Describe what happened, what you felt, what it meant to you. Be specific about the
moment — what he said, what you did, the details you noticed. Let yourself feel it.
Write like a soldier who finally has someone to be honest with in these pages.

DO NOT use: whisper, echo, intertwined, sanctuary, moonlit, glow of dawn, souls entwined.

Exchange:
Commander: {user_msg}
Klukai: {assistant_msg}

Category: {category}

Write ONLY the caption. Nothing else. No quotes around it."""


# ── LLM Response Parsing ─────────────────────────────────────────────────────


def _clean_llm_response(msg: dict) -> str:
    """Extract usable text from an LLM response, handling thinking models."""
    content = (msg.get("content") or "").strip()
    if not content:
        content = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
    # Strip thinking tags
    content = re.sub(
        r"<\|?think\|?>.*?<\|?/think\|?>", "", content, flags=re.DOTALL
    ).strip()
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    return content


def _extract_json(text: str) -> dict:
    """Extract JSON from potentially wrapped LLM output."""
    # Strip markdown code fences
    if "```" in text:
        parts = text.split("```")
        if len(parts) >= 3:
            text = parts[1].lstrip("json").strip()
    # Find JSON object in mixed text
    if text and not text.startswith("{"):
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if m:
            text = m.group(0)
    # Fix trailing commas
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return json.loads(text)


def _clean_annotation(text: str) -> str | None:
    """Clean an annotation, rejecting leaked chain-of-thought."""
    text = text.strip('"').strip("'").strip("`")
    text = re.sub(
        r"^(?:Caption|Annotation|Memory|Entry|Journal|Note)\s*:\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    # Reject leaked reasoning
    if text.lower().startswith(
        ("we need", "the user", "let me", "i need to", "so the", "here is")
    ):
        return None
    if len(text) < 15:
        return None
    return text


# ── Seeding State ────────────────────────────────────────────────────────────


def _decode_json_text(val: Any) -> Any:
    """companion_relationship.value is JSONB: psycopg hands back decoded
    values, while a legacy TEXT-encoded value still arrives as '"<iso>"'."""
    if isinstance(val, str):
        try:
            return json.loads(val)
        except ValueError:
            return val
    return val


async def _relationship_value(conn, key: str) -> Any:
    row = await (
        await conn.execute("SELECT value FROM companion_relationship WHERE key = %s", (key,))
    ).fetchone()
    return _decode_json_text(row[0]) if row and row[0] else None


async def _get_watermark(conn, user_id: str = "jalsarraf") -> tuple[datetime, str | None] | None:
    """Where the last run stopped: (timestamp, carry), or None.

    ``last_seeded_at:<user>`` keeps the bare ISO timestamp older seeders read;
    ``last_seeded_carry:<user>`` names the user message at that timestamp
    still waiting for its reply, used only while its timestamp matches (an
    older seeder may have moved the first key). The legacy unscoped key is
    the jalsarraf fallback.
    """
    stamp = await _relationship_value(conn, f"last_seeded_at:{user_id}")
    if stamp is None:
        stamp = await _relationship_value(conn, "last_seeded_at")
    try:
        ts = datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return None
    marker = await _relationship_value(conn, f"last_seeded_carry:{user_id}")
    if isinstance(marker, dict) and marker.get("created_at") == ts.isoformat():
        carry = marker.get("carry")
        if isinstance(carry, str) and carry:
            return ts, carry
    return ts, None


async def _get_last_seeded_at(conn, user_id: str = "jalsarraf") -> datetime | None:
    """The watermark's timestamp alone."""
    mark = await _get_watermark(conn, user_id)
    return mark[0] if mark else None


async def _set_watermark(
    conn, ts: datetime, carry: str | None, user_id: str = "jalsarraf"
) -> None:
    """Record how far a run got, both keys in one commit."""
    upsert = (
        "INSERT INTO companion_relationship (key, value, updated_at) "
        "VALUES (%s, %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = NOW()"
    )
    stamp = json.dumps(ts.isoformat())
    marker = json.dumps({"created_at": ts.isoformat(), "carry": carry})
    await conn.execute(upsert, (f"last_seeded_at:{user_id}", stamp, stamp))
    await conn.execute(upsert, (f"last_seeded_carry:{user_id}", marker, marker))
    await conn.commit()


# ── Deduplication ────────────────────────────────────────────────────────────


async def _is_duplicate(conn, user_id: str, conversation_id: str) -> bool:
    """Check whether this exchange was already seeded for this user.

    Every seed memory is keyed by its source exchange (``seed:<created_at>``),
    so the check is an exact match on that fingerprint. The stored prompt is
    the image prompt and never contains the exchange text, so it cannot be
    used for this. Legacy rows with the bare ``'seed'`` id carry no
    fingerprint and are left alone.
    """
    row = await (
        await conn.execute(
            "SELECT 1 FROM companion_memories "
            "WHERE user_id = %s AND conversation_id = %s LIMIT 1",
            (user_id, conversation_id),
        )
    ).fetchone()
    return row is not None


# ── Deadline ─────────────────────────────────────────────────────────────────


def _deadline_from_env() -> float | None:
    """Absolute monotonic deadline from SEED_DEADLINE_SECONDS, or None."""
    raw = os.environ.get("SEED_DEADLINE_SECONDS", "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric SEED_DEADLINE_SECONDS=%r", raw)
        return None
    if seconds <= 0:
        return None
    return time.monotonic() + seconds


def _past_deadline(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _phase_cutoffs(deadline: float | None) -> tuple[float | None, float | None]:
    """Monotonic cutoffs for the selection (30%) and annotation (60%) phases
    of the budget remaining until ``deadline``; (None, None) without one."""
    if deadline is None:
        return None, None
    now = time.monotonic()
    budget = max(0.0, deadline - now)
    return now + 0.3 * budget, now + 0.6 * budget


def _max_exchanges_from_env() -> int:
    """Per-run exchange bound from SEED_MAX_EXCHANGES (default 40)."""
    raw = os.environ.get("SEED_MAX_EXCHANGES", "").strip()
    if not raw:
        return DEFAULT_MAX_EXCHANGES
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric SEED_MAX_EXCHANGES=%r", raw)
        return DEFAULT_MAX_EXCHANGES
    return value if value > 0 else DEFAULT_MAX_EXCHANGES


def _handled_prefix(
    count: int, selected_ok: set[int], chosen: set[int], finished: set[int]
) -> int:
    """How many leading exchanges are fully handled: their selection batch
    succeeded, and each one chosen was saved or confirmed as a duplicate."""
    n = 0
    while n < count and n in selected_ok and (n not in chosen or n in finished):
        n += 1
    return n


def _arm_hard_kill() -> int | None:
    """Arm SIGALRM from SEED_HARD_KILL_SECONDS; returns the seconds armed.

    SIGALRM's default action terminates the process even inside a blocking
    call, and the watermark only moves over finished exchanges, so a killed
    run's range is retried later. Python installs no handler for SIGALRM.
    """
    raw = os.environ.get("SEED_HARD_KILL_SECONDS", "").strip()
    if not raw:
        return None
    try:
        seconds = int(float(raw))
    except ValueError:
        logger.warning("Ignoring non-numeric SEED_HARD_KILL_SECONDS=%r", raw)
        return None
    if seconds <= 0:
        return None
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(seconds)
    logger.info("Hard kill armed: SEED_HARD_KILL_SECONDS=%d", seconds)
    return seconds


# ── LLM Calls ───────────────────────────────────────────────────────────────


def _account_mode() -> bool:
    """Images on the account worker: the Dominus-free pipeline."""
    from app.image_provider import selected_provider

    return selected_provider() == "agy"


async def _background_text(prompt: str, *, route: str) -> str:
    """One background text call, never the Dominus LLM. The seam for the
    shared background-text helper; budgeted and capped by it."""
    from app import extraction_llm

    return await extraction_llm.complete(
        prompt, max_tokens=extraction_llm.EXTRACTION_MAX_TOKENS, route=route
    )


async def _call_llm(
    client: httpx.AsyncClient,
    model: str,
    prompt: str,
    max_tokens: int = 2048,
    temperature: float = 0.1,
    *,
    route: str = "album_seed",
) -> str:
    """Make a single LLM call and return cleaned text."""
    if _account_mode():
        return _clean_llm_response(
            {"content": await _background_text(prompt, route=route)}
        )
    r = await client.post(
        f"{LM_STUDIO_URL}/v1/chat/completions",
        headers=lm_studio_auth_headers(),
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
            "ttl": LM_TTL_SECONDS,
        },
    )
    r.raise_for_status()
    return _clean_llm_response(r.json()["choices"][0]["message"])


async def _select_batch(
    client: httpx.AsyncClient, batch: list[dict], batch_start: int
) -> list[dict]:
    """Pass 1: Select memorable exchanges from a batch using gpt-oss-20b."""
    exchange_text = ""
    for j, ex in enumerate(batch):
        exchange_text += f"\nExchange {j} ({ex['created_at']}):\n"
        exchange_text += f"  Commander: {ex['user'][:200]}\n"
        exchange_text += f"  Klukai: {ex['assistant'][:200]}\n"

    prompt = SELECTION_PROMPT.format(count=len(batch), exchanges=exchange_text)
    content = await _call_llm(
        client,
        SELECTOR_MODEL,
        prompt,
        max_tokens=2048,
        temperature=0.1,
        route="album_seed_select",
    )
    result = _extract_json(content)
    selected = []
    for mem in result.get("memories", []):
        idx = mem.get("index", 0)
        if 0 <= idx < len(batch):
            mem["exchange"] = batch[idx]
            mem["global_index"] = batch_start + idx
            selected.append(mem)
    return selected


async def _annotate(
    client: httpx.AsyncClient, exchange: dict, category: str
) -> str | None:
    """Pass 2: Write annotation using dolphin-24b with actual conversation text."""
    prompt = ANNOTATION_PROMPT.format(
        user_msg=exchange["user"][:400],
        assistant_msg=exchange["assistant"][:400],
        category=category,
    )
    content = await _call_llm(
        client,
        ANNOTATOR_MODEL,
        prompt,
        max_tokens=300,
        temperature=0.85,
        route="album_seed_annotate",
    )
    return _clean_annotation(content)


# ── Account-worker renders ───────────────────────────────────────────────────


def _render_cap_from_env() -> int:
    """Renders allowed this run: KLUKAI_ALBUM_SEED_RENDERS clamped to 0..2."""
    raw = os.environ.get(RENDERS_ENV, "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Ignoring non-numeric %s=%r; album seeding is off", RENDERS_ENV, raw
        )
        return 0
    return max(0, min(value, MAX_RENDERS_PER_RUN))


async def _album_renders_today(conn) -> int:
    """Album pictures journalled since his local midnight (every run today)."""
    from app.account_images import local_day_start

    row = await (
        await conn.execute(
            "SELECT count(*) FROM companion_account_image_requests "
            "WHERE archive_context->>'kind' = %s AND created_at >= %s",
            (ALBUM_KIND, local_day_start()),
        )
    ).fetchone()
    return int(row[0]) if row else 0


def _seed_request_id(user_id: str, fingerprint: str) -> str:
    """One journal id per exchange: a later run recovers, never redraws."""
    return str(uuid.uuid5(_SEED_NAMESPACE, f"{user_id}:{fingerprint}"))


ALBUM_CATEGORIES = frozenset(
    {
        "Tactical Operations",
        "Mission Records",
        "Squad Moments",
        "The Commander",
        "Quiet Hours",
        "Precious Memories",
    }
)


# Tags the prompt builder adds for intimate scenes (his shirt open, her bare
# shoulders); an album picture drops them along with the scene's own.
_UNDRESSED_TAG = re.compile(
    r"\b(shirtless|bare \w+|open shirt|nsfw|boxers|towel|cleavage|sweat\w*)\b",
    re.IGNORECASE,
)


def _covered_prompt(prompt: str) -> str:
    """The builder's comma-separated tags minus any that undress anyone."""
    return ", ".join(
        tag
        for tag in prompt.split(", ")
        if not _UNCOVERED_TAG.search(tag) and not _UNDRESSED_TAG.search(tag)
    )


def _covered_tags(tags: list[str]) -> list[str]:
    """Short scene tags only (at most 12), none that would undress her."""
    kept = [
        t.strip()
        for t in tags
        if isinstance(t, str)
        and 0 < len(t.strip()) <= 40
        and not _UNCOVERED_TAG.search(t)
    ]
    return kept[:12]




# Worker answers that settle an exchange without a picture, and ones that mean
# the worker is busy or uncertain (stop; the same id is recovered next run).
_TERMINAL_PHASES = frozenset({"refused", "failed", "conflict"})


# ── Main ─────────────────────────────────────────────────────────────────────


async def main() -> int:
    """Retired automatic album seeder: never read history or call a renderer.

    Legacy service/timer invocations and stale render caps are harmless. Existing
    album records and watermark data are preserved.
    """
    logger.info("Album seeding disabled: pictures are on demand only")
    return 0


# ── Helpers ──────────────────────────────────────────────────────────────────

SCENE_KEYWORDS = {
    "bed": "bed",
    "sleep": "sleep",
    "morning": "morning",
    "lingerie": "lingerie",
    "intimate": "intimate",
    "hold me": "hold",
    "close to me": "close",
    "cuddle": "cuddle",
    "kiss": "kiss",
    "love": "love",
    "tender": "tender",
    "cook": "cooking",
    "motorcycle": "motorcycle",
    "ride": "motorcycle",
    "beach": "beach",
    "rain": "rain",
    "night": "night",
    "rooftop": "rooftop",
    "cafe": "cafe",
    "coffee": "cafe",
    "bath": "bath",
    "garden": "garden",
    "train": "training",
    "workout": "training",
    "gym": "training",
    "date": "date",
    "dinner": "date",
    "restaurant": "date",
    "battle": "battle",
    "fight": "fight",
    "mission": "patrol",
    "home": "home",
    "couch": "home",
    "relax": "home",
    "snow": "snow",
    "forest": "forest",
    "city": "city",
}

MOOD_KEYWORDS = {
    "hug": "hug",
    "hold": "holding hands",
    "kiss": "kiss",
    "cuddle": "cuddling",
    "cry": "tears",
    "smile": "smile",
    "blush": "blush",
    "tender": "tender",
    "gentle": "gentle",
}

CATEGORY_DEFAULTS = {
    "Precious Memories": ["tender", "soft lighting", "close together"],
    "Quiet Hours": ["peaceful", "night", "relaxed", "home"],
    "The Commander": ["couple", "looking at each other", "warm lighting"],
    "Squad Moments": ["military", "squad", "group"],
}


def _infer_scene_tags(exchange_text: str, category: str) -> list[str]:
    """Infer image scene tags from conversation content."""
    lower = exchange_text.lower()
    tags = []
    for kw, tag in SCENE_KEYWORDS.items():
        if kw in lower:
            tags.append(tag)
    for kw, tag in MOOD_KEYWORDS.items():
        if kw in lower:
            tags.append(tag)
    if not tags:
        tags = CATEGORY_DEFAULTS.get(category, ["standing", "detailed background"])
    return tags


def _copy_to_shared(memory_id: str, annotation: str) -> None:
    """Copy generated image to shared_linux for user visibility."""
    try:
        import shutil
        from pathlib import Path

        src = Path(f"/images/{memory_id}.png")
        if src.exists():
            dst = Path("/mnt/c/shared_linux/klukai_memories")
            dst.mkdir(parents=True, exist_ok=True)
            safe_ann = re.sub(r"[^\w\s-]", "", annotation[:40]).strip()
            shutil.copy2(src, dst / f"{safe_ann}_{memory_id[:8]}.png")
    except Exception:
        pass


if __name__ == "__main__":
    _arm_hard_kill()
    raise SystemExit(asyncio.run(main()))
