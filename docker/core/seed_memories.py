#!/usr/bin/env python3
"""Seed Klukai's memory archive from conversation history.

Three-pass approach:
  Pass 1: gpt-oss-20b selects which exchanges to keep (reliable JSON)
  Pass 2: dolphin-24b writes annotations using ACTUAL conversation text
  Pass 3: ComfyUI generates Illustrious images for each memory

Tracks last_seeded_at so only new exchanges are processed on each run.
Each saved memory carries conversation_id "seed:<exchange created_at>", the
fingerprint that dedups it and lets reannotate_existing.py find its exchange.

SEED_DEADLINE_SECONDS (optional) is a soft wall-clock budget: once spent, the
run stops before its next unit of work, records a "deadline" failure, and
leaves last_seeded_at untouched so the range is retried on a later run.
SEED_MAX_EXCHANGES (default 40) bounds each run to the oldest unseeded
exchanges, so a run can finish its prefix inside the budget and advance the
watermark; a deadline hit on an unbounded backlog would otherwise repeat the
same selection forever without saving or advancing.
SEED_HARD_KILL_SECONDS (optional) is the hard bound: SIGALRM ends the process
outright once it is spent, wherever it is stuck. The production image is
distroless (no coreutils `timeout`), so the bound lives here.

Run inside companion-core container:
  docker exec companion-core python3 /app/seed_memories.py
"""

import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime

import httpx
import psycopg

from app.image_gen import build_prompt, free_comfyui_vram, generate_image
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


async def _get_last_seeded_at(conn, user_id: str = "jalsarraf") -> datetime | None:
    """Get the timestamp of the last seeded exchange for a user."""
    key = f"last_seeded_at:{user_id}"
    # Try user-scoped key first, fall back to legacy key
    row = await (
        await conn.execute(
            "SELECT value FROM companion_relationship WHERE key = %s", (key,)
        )
    ).fetchone()
    if not row:
        # Legacy fallback for jalsarraf
        row = await (
            await conn.execute(
                "SELECT value FROM companion_relationship WHERE key = 'last_seeded_at'"
            )
        ).fetchone()
    if row and row[0]:
        val = row[0]
        # value is JSONB, so psycopg hands back the bare ISO string already
        # decoded. A legacy TEXT-encoded value still arrives as '"<iso>"' and
        # needs one json.loads; a bare ISO string is not JSON and is kept as-is.
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except ValueError:
                pass
        try:
            return datetime.fromisoformat(val)
        except (ValueError, TypeError):
            pass
    return None


async def _set_last_seeded_at(conn, ts: datetime, user_id: str = "jalsarraf") -> None:
    """Record the timestamp of the most recent seeded exchange for a user."""
    key = f"last_seeded_at:{user_id}"
    await conn.execute(
        "INSERT INTO companion_relationship (key, value, updated_at) "
        "VALUES (%s, %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = NOW()",
        (key, json.dumps(ts.isoformat()), json.dumps(ts.isoformat())),
    )
    await conn.commit()


# ── Deduplication ────────────────────────────────────────────────────────────


def _seed_conversation_id(created_at: str) -> str:
    """Fingerprint a seed memory by the exchange it came from."""
    return f"seed:{created_at}"


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


def _bounded_prefix(stamps: list[datetime | None], limit: int) -> int:
    """How many of the oldest exchanges this run takes: ``limit``, extended
    over exchanges that share the last one's timestamp, because the next
    run's query is ``created_at > watermark`` and would skip a tie."""
    if len(stamps) <= limit:
        return len(stamps)
    end = limit
    while end < len(stamps) and stamps[end] is not None and stamps[end] == stamps[end - 1]:
        end += 1
    return end


def _arm_hard_kill() -> int | None:
    """Arm SIGALRM from SEED_HARD_KILL_SECONDS; returns the seconds armed.

    SIGALRM's default action terminates the process even inside a blocking
    call, and last_seeded_at only moves after a completed run, so a killed
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


async def _call_llm(
    client: httpx.AsyncClient,
    model: str,
    prompt: str,
    max_tokens: int = 2048,
    temperature: float = 0.1,
) -> str:
    """Make a single LLM call and return cleaned text."""
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
        client, SELECTOR_MODEL, prompt, max_tokens=2048, temperature=0.1
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
        client, ANNOTATOR_MODEL, prompt, max_tokens=300, temperature=0.85
    )
    return _clean_annotation(content)


# ── Main ─────────────────────────────────────────────────────────────────────


async def main() -> int:
    from app.creative_text import uses_cloud_text
    from app.image_provider import uses_cloud_images

    try:
        remote = uses_cloud_text() or uses_cloud_images()
    except Exception as error:
        logger.error("Invalid creative provider configuration (%s)", type(error).__name__)
        return 1
    if remote:
        # The legacy watermark only advances after the entire run. A partial
        # failure can reselect and redraw previously saved exchanges next run;
        # its prompt-based dedupe is insufficient for paid generation.
        logger.error("Remote archive seeding requires durable source-aware jobs; run skipped")
        return 1

    # Parse --user argument (default: jalsarraf)
    target_user = "jalsarraf"
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--user" and i < len(sys.argv):
            target_user = sys.argv[i + 1] if (i + 1) < len(sys.argv) else "jalsarraf"
        elif arg.startswith("--user="):
            target_user = arg.split("=", 1)[1]

    logger.info("=== Memory Seeder Starting (user: %s) ===", target_user)
    logger.info("Selector: %s | Annotator: %s", SELECTOR_MODEL, ANNOTATOR_MODEL)

    deadline = _deadline_from_env()
    if deadline is not None:
        logger.info("Deadline: SEED_DEADLINE_SECONDS=%s", os.environ["SEED_DEADLINE_SECONDS"])
    pipeline_failures: list[str] = []

    def _deadline_hit(stage: str) -> bool:
        """True once the soft deadline has passed; records it exactly once."""
        if not _past_deadline(deadline):
            return False
        if "deadline" not in pipeline_failures:
            logger.error("Deadline reached during %s; stopping this run", stage)
            pipeline_failures.append("deadline")
        return True

    conn = await psycopg.AsyncConnection.connect(DATABASE_URL)

    # Get last seeded timestamp for incremental processing
    last_seeded = await _get_last_seeded_at(conn, target_user)
    if last_seeded:
        logger.info("Last seeded at: %s — processing only newer exchanges", last_seeded)
        where_clause = "WHERE user_id = %s AND created_at > %s"
        params = (target_user, last_seeded)
    else:
        logger.info("First run for %s — processing all exchanges", target_user)
        where_clause = "WHERE user_id = %s"
        params = (target_user,)

    cur = await conn.execute(
        f"SELECT role, content, created_at FROM companion_messages "
        f"{where_clause} ORDER BY created_at ASC",
        params,
    )
    rows = await cur.fetchall()
    logger.info("Found %d messages since last seed", len(rows))

    # Pair into user→assistant exchanges
    exchanges = []
    stamps: list[datetime | None] = []
    i = 0
    while i < len(rows) - 1:
        if rows[i][0] == "user" and rows[i + 1][0] == "assistant":
            ts = rows[i][2]
            exchanges.append(
                {
                    "user": rows[i][1][:500],
                    "assistant": rows[i + 1][1][:500],
                    "created_at": ts.isoformat() if ts else "",
                }
            )
            stamps.append(ts)
            i += 2
        else:
            i += 1

    logger.info("Grouped into %d exchanges", len(exchanges))
    # Take a bounded prefix of the oldest exchanges; the watermark advances to
    # the newest one in it, and the rest wait for the next run.
    take = _bounded_prefix(stamps, _max_exchanges_from_env())
    if take < len(exchanges):
        logger.info(
            "Processing the oldest %d of %d exchanges this run", take, len(exchanges)
        )
        exchanges = exchanges[:take]
        stamps = stamps[:take]
    newest_ts = max((ts for ts in stamps if ts), default=None)
    if not exchanges:
        logger.info("No new exchanges to process")
        await conn.close()
        return 0

    # Free stale ComfyUI allocations before the first real selection batch.
    # Even cleanup goes through the authenticated gateway facade while holding
    # the matching GPU lease; there is no raw ComfyUI network path.
    # The first batch itself performs the lazy model load; synthetic warm-up or
    # keepalive prompts are forbidden because they create false activity and
    # extend model residency.
    try:
        cleanup_ready = await free_comfyui_vram()
    except Exception as error:
        logger.error("Initial GPU cleanup failed (%s)", type(error).__name__)
        cleanup_ready = False
    if not cleanup_ready:
        logger.error("Seeding incomplete; last_seeded_at remains unchanged")
        await conn.close()
        return 1

    # ── PASS 1: Selection ────────────────────────────────────────────────
    logger.info("=== PASS 1: Selection (%s) ===", SELECTOR_MODEL)
    selected = []
    failed_batches = []
    unresolved_selector_batches: list[int] = []

    async with httpx.AsyncClient(timeout=60.0) as client:
        for batch_start in range(0, len(exchanges), BATCH_SIZE):
            if _deadline_hit("selection"):
                break
            batch = exchanges[batch_start : batch_start + BATCH_SIZE]
            logger.info(
                "Selecting batch %d-%d of %d...",
                batch_start,
                batch_start + len(batch),
                len(exchanges),
            )
            try:
                batch_selected = await _select_batch(client, batch, batch_start)
                selected.extend(batch_selected)
                for s in batch_selected:
                    logger.info(
                        "  Selected exchange %d (%s)",
                        s["global_index"],
                        s.get("category", "?"),
                    )
            except Exception as e:
                logger.warning("  Batch %d failed (%s)", batch_start, type(e).__name__)
                failed_batches.append((batch_start, batch))
            await asyncio.sleep(2)

    # Retry failed batches once with longer timeout
    if failed_batches:
        logger.info("Retrying %d failed batches...", len(failed_batches))
        async with httpx.AsyncClient(timeout=180.0) as client:
            for batch_start, batch in failed_batches:
                if _deadline_hit("selection retry"):
                    break
                try:
                    batch_selected = await _select_batch(client, batch, batch_start)
                    selected.extend(batch_selected)
                    for s in batch_selected:
                        logger.info(
                            "  RETRY: Selected exchange %d (%s)",
                            s["global_index"],
                            s.get("category", "?"),
                        )
                except Exception as e:
                    logger.warning(
                        "  RETRY batch %d failed again (%s)",
                        batch_start,
                        type(e).__name__,
                    )
                    unresolved_selector_batches.append(batch_start)
                await asyncio.sleep(3)

    pipeline_failures.extend(
        f"selector batch {batch_start}"
        for batch_start in unresolved_selector_batches
    )

    logger.info(
        "Pass 1 complete: %d exchanges selected from %d", len(selected), len(exchanges)
    )

    if not selected:
        if pipeline_failures:
            logger.error(
                "Seeding incomplete (%s); last_seeded_at remains unchanged",
                ", ".join(pipeline_failures),
            )
            await conn.close()
            return 1
        logger.info("No memories selected; selection completed successfully")
        if newest_ts:
            await _set_last_seeded_at(conn, newest_ts, target_user)
        await conn.close()
        return 0

    # ── PASS 2: Annotation ───────────────────────────────────────────────
    logger.info("=== PASS 2: Annotation (%s) ===", ANNOTATOR_MODEL)
    async with httpx.AsyncClient(timeout=120.0) as client:
        for mem in selected:
            if _deadline_hit("annotation"):
                break
            ex = mem["exchange"]
            category = mem.get("category", "Precious Memories")
            mem["conversation_id"] = _seed_conversation_id(ex["created_at"])

            # Deduplication check
            try:
                if await _is_duplicate(conn, target_user, mem["conversation_id"]):
                    logger.info("  Skipping duplicate: %s", ex["user"][:50])
                    mem["skip"] = True
                    continue
            except Exception:
                pass  # If dedup check fails, proceed anyway

            try:
                annotation = await _annotate(client, ex, category)
            except Exception as error:
                logger.error(
                    "  Annotation failed for exchange %s (%s)",
                    mem.get("global_index", "?"),
                    type(error).__name__,
                )
                mem["pipeline_failed"] = True
                pipeline_failures.append(
                    f"annotation exchange {mem.get('global_index', '?')}"
                )
                continue
            if not annotation:
                # Empty or rejected text is a pipeline failure, same as an
                # exception: no placeholder caption, no GPU render for it, and
                # the watermark stays put so the exchange is retried later.
                logger.error(
                    "  Annotation unusable for exchange %s; skipping render",
                    mem.get("global_index", "?"),
                )
                mem["pipeline_failed"] = True
                pipeline_failures.append(
                    f"annotation exchange {mem.get('global_index', '?')}"
                )
                continue
            mem["annotation"] = annotation
            logger.info("  Annotated: %s", annotation[:70])

            await asyncio.sleep(3)

    # Filter out skipped (duplicates)
    selected = [
        m
        for m in selected
        if not m.get("skip") and not m.get("pipeline_failed")
    ]
    logger.info("Pass 2 complete: %d annotations written (after dedup)", len(selected))

    if not selected:
        if pipeline_failures:
            logger.error(
                "Seeding incomplete (%s); last_seeded_at remains unchanged",
                ", ".join(pipeline_failures),
            )
        elif newest_ts:
            await _set_last_seeded_at(conn, newest_ts, target_user)
        await conn.close()
        return 1 if pipeline_failures else 0

    # ── PASS 3: Image Generation ─────────────────────────────────────────
    # generate_image acquires the authenticated exclusive GPU lease, drains
    # active inference, and unloads llama.cpp before ComfyUI can allocate.
    logger.info("Preparing exclusive GPU handoff for image generation...")

    logger.info("=== PASS 3: Image Generation ===")
    from app.memory_archive import save_image
    from app.db import init_pool, close_pool

    await init_pool(min_size=1, max_size=3)
    saved_count = 0

    for i, mem in enumerate(selected):
        if _deadline_hit("image generation"):
            break
        ex = mem["exchange"]
        logger.info(
            "Generating %d/%d: %s",
            i + 1,
            len(selected),
            mem.get("annotation", "")[:50],
        )

        try:
            tags = mem.get("image_tags", [])
            exchange_text = f"{ex['user']} {ex['assistant']}"

            # Infer tags from conversation if selector didn't provide them
            if not tags:
                tags = _infer_scene_tags(exchange_text, mem.get("category", ""))
                logger.info("  Inferred tags: %s", tags)

            scene_tags = ", ".join(tags)
            couple = any(
                kw in exchange_text.lower()
                for kw in [
                    "us",
                    "together",
                    "we",
                    "our",
                    "holding",
                    "hug",
                    "kiss",
                    "beside",
                    "couple",
                    "1boy",
                ]
            )

            full_prompt = build_prompt(
                scene_tags,
                couple=couple,
                affection_level=8,
                context=exchange_text,
            )

            img_bytes = await generate_image(full_prompt)
            if img_bytes:
                curation = {
                    "keep": True,
                    "annotation": mem.get("annotation", ""),
                    "category": mem.get("category", "Precious Memories"),
                    "image_tags": tags,
                }
                memory_id = await save_image(
                    img_bytes,
                    full_prompt,
                    mem["conversation_id"],
                    mood="tender",
                    affection_level=8,
                    curation=curation,
                    user_id=target_user,
                )
                if memory_id:
                    saved_count += 1
                    logger.info("  Saved: %s (%s)", memory_id[:12], mem.get("category"))

                    # Copy to shared_linux if accessible
                    _copy_to_shared(memory_id, mem.get("annotation", ""))
                else:
                    logger.warning("  Save failed")
                    pipeline_failures.append(
                        f"save exchange {mem.get('global_index', '?')}"
                    )
            else:
                logger.warning("  Image gen failed")
                pipeline_failures.append(
                    f"image exchange {mem.get('global_index', '?')}"
                )

            # generate_image frees Comfy weights before releasing its lease.
            if i < len(selected) - 1:
                logger.info("  Waiting 30s...")
                await asyncio.sleep(30)

        except Exception as e:
            logger.error(
                "  Pipeline failed for exchange %s (%s)",
                mem.get("global_index", "?"),
                type(e).__name__,
            )
            pipeline_failures.append(
                f"image/save exchange {mem.get('global_index', '?')}"
            )

    # Advance only after every selector batch and every selected memory reached
    # a durable save (or was confirmed as an existing duplicate). A partial
    # outage must leave the range eligible for a later retry.
    if not pipeline_failures and newest_ts:
        await _set_last_seeded_at(conn, newest_ts, target_user)
        logger.info("Updated last_seeded_at to %s", newest_ts)
    elif pipeline_failures:
        logger.error(
            "Seeding incomplete (%s); last_seeded_at remains unchanged",
            ", ".join(pipeline_failures),
        )

    await close_pool()
    await conn.close()

    logger.info(
        "=== SEEDING COMPLETE: %d/%d memories saved ===", saved_count, len(selected)
    )
    return 1 if pipeline_failures else 0


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
