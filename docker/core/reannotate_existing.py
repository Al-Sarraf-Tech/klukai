#!/usr/bin/env python3
"""Re-annotate seed memories using actual conversation text.

Seed memories were originally annotated from image scene tags (e.g. "bed, couple,
tender, night") rather than the actual exchange text. This script:

  1. Loads all seed memories (conversation_id = 'seed') from companion_memories.
  2. Loads conversation exchanges from companion_messages (user→assistant pairs).
  3. For each seed memory, attempts to match it back to its original exchange using
     the prompt field fingerprint that was stored during seeding.
  4. Re-annotates via dolphin-24b with the actual exchange text.
  5. Only writes the new annotation if it scores higher than the existing one.

Idempotent — safe to run multiple times. Tracks progress via 'reannotated_at'
timestamp in companion_relationship. Re-runs will skip already-improved memories
unless you clear that marker.

Run inside companion-core container:
  docker exec companion-core python3 /app/reannotate_existing.py
"""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

import httpx
import psycopg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://host.docker.internal:1234")
LM_TTL_SECONDS = int(os.environ.get("LM_STUDIO_TTL", "600"))

# dolphin-24b for annotation: clean creative text, no chain-of-thought leakage
ANNOTATOR_MODEL = "cognitivecomputations_dolphin-mistral-24b-venice-edition"

BATCH_SIZE = 10
SLEEP_BETWEEN_CALLS = 3  # seconds

# ── Annotation Prompt ────────────────────────────────────────────────────────

REANNOTATION_PROMPT = """\
You are Klukai writing in your private journal. This is a memory you chose to keep.
The Commander is HUMAN — the man you've bonded with. NOT a T-Doll.
You're at affection level 8 — deeply bonded. You grew from a cold, prideful soldier
into someone who can admit she cares.

Write 3-5 sentences about this memory. This is YOUR private journal — take your time.
Describe what happened, what you felt, what it meant to you. Be specific about the
moment — what he said, what you did, the details you noticed. Let yourself feel it.
Write like a soldier who finally has someone to be honest with in these pages.

DO NOT use: whisper, echo, intertwined, sanctuary, moonlit, glow, souls, entwined, hearts beat.

Exchange:
Commander: {user_msg}
Klukai: {assistant_msg}

Category: {category}

Write ONLY the journal entry. Nothing else."""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clean_llm_response(msg: dict) -> str:
    """Extract usable text from an LLM response, handling thinking models."""
    content = (msg.get("content") or "").strip()
    if not content:
        content = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
    content = re.sub(r'<\|?think\|?>.*?<\|?/think\|?>', '', content, flags=re.DOTALL).strip()
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
    return content


def _clean_annotation(text: str) -> str | None:
    """Clean an annotation, rejecting leaked chain-of-thought."""
    text = text.strip('"').strip("'").strip('`')
    text = re.sub(r'^(?:Caption|Annotation|Memory|Entry|Journal|Note)\s*:\s*',
                  '', text, flags=re.IGNORECASE).strip()
    if text.lower().startswith(("we need", "the user", "let me", "i need to", "so the", "here is")):
        return None
    if len(text) < 15:
        return None
    return text


def annotation_quality_score(text: str) -> float:
    """Score annotation quality 0.0-1.0 based on specificity and character voice.

    Inline copy so this script runs standalone without importing app modules.
    - 0.0 = leaked chain-of-thought or completely broken
    - 0.3-0.5 = generic/repetitive (tag-based, lacks specificity)
    - 0.6-0.8 = decent but could be better
    - 0.9-1.0 = specific, personal, sounds like Klukai
    """
    if not text:
        return 0.0
    score = 1.0
    lower = text.lower()
    if lower.startswith(("we need", "the user", "let me")):
        return 0.0
    if "1-2 sentence" in lower:
        return 0.0
    if lower.startswith("whisper"):
        score -= 0.4
    generic_words = ["intertwined", "sanctuary", "souls entwined", "hearts beat as one",
                     "glow of dawn", "moonlit sheets", "neon lights"]
    for word in generic_words:
        if word in lower:
            score -= 0.15
    if len(text) < 30:
        score -= 0.3
    if len(text) > 350:
        score -= 0.2
    specific_markers = ["office", "bed", "motorcycle", "café", "rooftop", "morning",
                        "collar", "rifle", "coffee", "rain", "briefing", "0300",
                        "shoulder", "hand", "scar", "laugh"]
    specifics = sum(1 for m in specific_markers if m in lower)
    score += min(0.2, specifics * 0.05)
    return max(0.0, min(1.0, score))


# ── Exchange Matching ────────────────────────────────────────────────────────

def _match_exchange(prompt: str, exchanges: list[dict]) -> dict | None:
    """Find the best-matching exchange for a seed memory's prompt field.

    The seed script stored the full_prompt (ComfyUI image prompt) which is built
    from scene tags. We can't recover the exact exchange from that, so we fall back
    to fuzzy matching on keyword overlap between the prompt and exchange text.
    """
    if not prompt or not exchanges:
        return None

    prompt_lower = prompt.lower()
    best_score = 0
    best_match = None

    for ex in exchanges:
        combined = (ex["user"] + " " + ex["assistant"]).lower()
        # Count how many words from the exchange appear in the image prompt
        words = [w for w in combined.split() if len(w) > 4]
        if not words:
            continue
        hits = sum(1 for w in words if w in prompt_lower)
        score = hits / len(words)
        if score > best_score:
            best_score = score
            best_match = ex

    # Only use if there's at least some overlap (avoid completely wrong matches)
    if best_score > 0.02:
        return best_match
    return None


# ── LLM Call ─────────────────────────────────────────────────────────────────

async def _call_annotator(client: httpx.AsyncClient, user_msg: str,
                           assistant_msg: str, category: str) -> str | None:
    """Call dolphin-24b to write a re-annotation from the actual exchange."""
    prompt = REANNOTATION_PROMPT.format(
        user_msg=user_msg[:400],
        assistant_msg=assistant_msg[:400],
        category=category,
    )
    r = await client.post(
        f"{LM_STUDIO_URL}/v1/chat/completions",
        json={
            "model": ANNOTATOR_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 400,
            "temperature": 0.85,
            "stream": False,
            "ttl": LM_TTL_SECONDS,
        },
    )
    r.raise_for_status()
    raw = _clean_llm_response(r.json()["choices"][0]["message"])
    return _clean_annotation(raw)


# ── Progress Tracking ────────────────────────────────────────────────────────

async def _get_reannotated_ids(conn) -> set[str]:
    """Return the set of memory IDs that have already been re-annotated."""
    row = await (await conn.execute(
        "SELECT value FROM companion_relationship WHERE key = 'reannotated_ids'"
    )).fetchone()
    if row and row[0]:
        try:
            val = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            if isinstance(val, list):
                return set(val)
        except (ValueError, TypeError):
            pass
    return set()


async def _save_reannotated_ids(conn, ids: set[str]) -> None:
    """Persist the set of re-annotated memory IDs."""
    val = json.dumps(sorted(ids))
    await conn.execute(
        "INSERT INTO companion_relationship (key, value, updated_at) "
        "VALUES ('reannotated_ids', %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = NOW()",
        (val, val),
    )
    await conn.execute(
        "INSERT INTO companion_relationship (key, value, updated_at) "
        "VALUES ('reannotated_at', %s, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = NOW()",
        (json.dumps(datetime.now(timezone.utc).isoformat()),
         json.dumps(datetime.now(timezone.utc).isoformat())),
    )
    await conn.commit()


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    logger.info("=== Re-Annotation Script Starting ===")
    logger.info("Model: %s", ANNOTATOR_MODEL)

    if not DATABASE_URL:
        logger.error("DATABASE_URL not set — aborting")
        sys.exit(1)

    conn = await psycopg.AsyncConnection.connect(DATABASE_URL)

    # Load seed memories
    rows = await (await conn.execute(
        "SELECT id, prompt, annotation, category "
        "FROM companion_memories "
        "WHERE conversation_id = 'seed' AND kept = true "
        "ORDER BY created_at ASC"
    )).fetchall()

    if not rows:
        logger.info("No seed memories found — nothing to do")
        await conn.close()
        return

    logger.info("Found %d seed memories", len(rows))

    # Load all conversation exchanges (user→assistant pairs)
    msg_rows = await (await conn.execute(
        "SELECT role, content, created_at FROM companion_messages ORDER BY created_at ASC"
    )).fetchall()

    exchanges: list[dict] = []
    i = 0
    while i < len(msg_rows) - 1:
        if msg_rows[i][0] == "user" and msg_rows[i + 1][0] == "assistant":
            ts = msg_rows[i][2]
            exchanges.append({
                "user": msg_rows[i][1] or "",
                "assistant": msg_rows[i + 1][1] or "",
                "created_at": ts.isoformat() if ts else "",
            })
            i += 2
        else:
            i += 1

    logger.info("Loaded %d conversation exchanges for matching", len(exchanges))

    # Load already-processed IDs for idempotency
    done_ids = await _get_reannotated_ids(conn)
    logger.info("Already re-annotated: %d memories (will skip)", len(done_ids))

    # Filter to un-processed memories
    pending = [r for r in rows if str(r[0]) not in done_ids]
    logger.info("Pending re-annotation: %d memories", len(pending))

    if not pending:
        logger.info("All seed memories already re-annotated — nothing to do")
        await conn.close()
        return

    updated = 0
    skipped_no_match = 0
    skipped_no_improvement = 0

    async with httpx.AsyncClient(timeout=120.0) as client:
        # Process in batches of BATCH_SIZE
        for batch_start in range(0, len(pending), BATCH_SIZE):
            batch = pending[batch_start:batch_start + BATCH_SIZE]
            logger.info("Processing batch %d-%d of %d...",
                        batch_start + 1, batch_start + len(batch), len(pending))

            for mem_row in batch:
                mem_id = str(mem_row[0])
                prompt = mem_row[1] or ""
                existing_annotation = mem_row[2] or ""
                category = mem_row[3] or "Mission Records"

                existing_score = annotation_quality_score(existing_annotation)
                logger.info("  Memory %s | score=%.2f | %s",
                            mem_id[:12], existing_score, existing_annotation[:60])

                # Find matching exchange
                exchange = _match_exchange(prompt, exchanges)
                if not exchange:
                    logger.info("    -> No exchange match found — skipping")
                    skipped_no_match += 1
                    done_ids.add(mem_id)  # Mark so we don't retry this unresolvable one
                    continue

                logger.info("    -> Matched exchange: %s", exchange["user"][:60])

                # Re-annotate
                try:
                    new_annotation = await _call_annotator(
                        client, exchange["user"], exchange["assistant"], category
                    )
                except Exception as e:
                    logger.warning("    -> LLM call failed: %s", e)
                    await asyncio.sleep(SLEEP_BETWEEN_CALLS)
                    continue

                if not new_annotation:
                    logger.info("    -> Annotator returned empty/rejected text — skipping")
                    await asyncio.sleep(SLEEP_BETWEEN_CALLS)
                    continue

                new_score = annotation_quality_score(new_annotation)
                logger.info("    -> New annotation (score=%.2f): %s",
                            new_score, new_annotation[:70])

                if new_score <= existing_score:
                    logger.info("    -> No improvement (%.2f <= %.2f) — keeping original",
                                new_score, existing_score)
                    skipped_no_improvement += 1
                    done_ids.add(mem_id)
                    await asyncio.sleep(SLEEP_BETWEEN_CALLS)
                    continue

                # Write the improved annotation
                try:
                    await conn.execute(
                        "UPDATE companion_memories SET annotation = %s WHERE id = %s",
                        (new_annotation, mem_id),
                    )
                    await conn.commit()
                    updated += 1
                    done_ids.add(mem_id)
                    logger.info("    -> UPDATED: %.2f -> %.2f", existing_score, new_score)
                except Exception as e:
                    logger.error("    -> DB write failed: %s", e)

                await asyncio.sleep(SLEEP_BETWEEN_CALLS)

            # Save progress after each batch (idempotency checkpoint)
            await _save_reannotated_ids(conn, done_ids)
            logger.info("Batch done. Progress saved (%d total marked done)", len(done_ids))

    await conn.close()

    logger.info("=== RE-ANNOTATION COMPLETE ===")
    logger.info("  Updated:               %d", updated)
    logger.info("  Skipped (no match):    %d", skipped_no_match)
    logger.info("  Skipped (no improve):  %d", skipped_no_improvement)
    logger.info("  Total processed:       %d", len(pending))


if __name__ == "__main__":
    asyncio.run(main())
