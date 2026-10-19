#!/usr/bin/env python3
"""Re-annotate seed memories using actual conversation text.

Seed memories were originally annotated from image scene tags (e.g. "bed, couple,
tender, night") rather than the actual exchange text. This script:

  1. Loads one user's seed memories (conversation_id LIKE 'seed%') from
     companion_memories.
  2. Loads that user's conversation exchanges from companion_messages
     (user→assistant pairs).
  3. Matches each memory to its exchange ONLY by the exact fingerprint
     seed_memories.py stores in conversation_id ("seed:<user message id>").
     Legacy rows with the bare 'seed' id have no fingerprint and are skipped;
     nothing is ever matched by text similarity.
  4. Re-annotates via dolphin-24b with the actual exchange text.
  5. Proposes the new annotation only if it scores higher than the existing one.

Default is a DRY RUN: it still spends the LLM calls, prints what would change,
and writes nothing. Pass --rewrite to apply. A rewrite is NOT idempotent — it
replaces the annotation — but it is never lossy: the previous text is appended to
annotation_history ({"annotation": old, "replaced_at": iso}) in the same UPDATE.
Progress ('reannotated_ids' in companion_relationship) is saved only with
--rewrite, so a re-run skips memories already handled unless that marker is
cleared.

Run inside companion-core container:
  docker exec companion-core python3 /app/reannotate_existing.py [--user U] [--rewrite]
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

import httpx
import psycopg

from app.lm_gateway import LM_TTL_SECONDS, lm_studio_auth_headers
from app.seed_exchanges import SEED_FINGERPRINT_PREFIX, pair_exchanges

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://100.107.121.5:1234")

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

def _match_exchange(conversation_id: str | None, exchanges: dict[str, dict]) -> dict | None:
    """Return the exchange a seed memory was made from, by exact fingerprint only.

    ``exchanges`` is keyed by the same ``seed:<user message id>`` fingerprint that
    seed_memories.py writes into conversation_id. A memory without that
    fingerprint (legacy bare 'seed') never matches: there is no safe way to
    recover its exchange, and guessing by text overlap wrote other exchanges'
    words over her annotations in the past.
    """
    if not conversation_id or not conversation_id.startswith(SEED_FINGERPRINT_PREFIX):
        return None
    return exchanges.get(conversation_id)


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
        headers=lm_studio_auth_headers(),
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


async def _save_reannotated_ids(conn, ids: set[str]) -> bool:
    """Persist the set of re-annotated memory IDs. Returns False on failure.

    A failed checkpoint is rolled back and reported, never raised: the
    annotation UPDATEs already committed stay committed, and the worst case
    is that a later run re-reads a few memories it has already handled.
    """
    val = json.dumps(sorted(ids))
    now_iso = json.dumps(datetime.now(timezone.utc).isoformat())
    try:
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
            (now_iso, now_iso),
        )
        await conn.commit()
        return True
    except Exception as e:
        logger.error("Progress checkpoint failed (%s); rolling back", type(e).__name__)
        try:
            await conn.rollback()
        except Exception:
            pass
        return False


# ── Write Path ───────────────────────────────────────────────────────────────

async def _replace_annotation(
    conn, mem_id: str, user_id: str, new_annotation: str
) -> bool:
    """Swap in the new annotation, keeping the replaced one in annotation_history.

    One statement, one transaction: the history append and the replacement
    either both land or neither does. The history entry is built from the
    row's annotation as the UPDATE sees it, not from the text this run read
    earlier, so two concurrent rewrites archive each other's revision instead
    of both archiving the original (READ COMMITTED re-evaluates the SET list
    against the row the first writer committed). Returns False (after
    rollback) on failure.
    """
    replaced_at = datetime.now(timezone.utc).isoformat()
    try:
        await conn.execute(
            "UPDATE companion_memories "
            "SET annotation_history = annotation_history || jsonb_build_array("
            "jsonb_build_object('annotation', annotation, 'replaced_at', %s::text)), "
            "annotation = %s "
            "WHERE id = %s AND user_id = %s",
            (replaced_at, new_annotation, mem_id, user_id),
        )
        await conn.commit()
        return True
    except Exception as e:
        logger.error("    -> DB write failed (%s); rolling back", type(e).__name__)
        try:
            await conn.rollback()
        except Exception:
            pass
        return False


# ── Main ─────────────────────────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Re-annotate seed memories from their exchanges.")
    ap.add_argument("--user", default="jalsarraf", help="user whose seed memories to process")
    ap.add_argument(
        "--rewrite",
        action="store_true",
        help="apply the improved annotations (default: dry run, print only)",
    )
    return ap.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    user_id = args.user
    mode = "REWRITE" if args.rewrite else "DRY RUN"
    logger.info("=== Re-Annotation Script Starting (user: %s, %s) ===", user_id, mode)
    logger.info("Model: %s", ANNOTATOR_MODEL)
    if not args.rewrite:
        logger.info("Dry run: nothing will be written. Pass --rewrite to apply.")

    if not DATABASE_URL:
        logger.error("DATABASE_URL not set — aborting")
        return 1

    conn = await psycopg.AsyncConnection.connect(DATABASE_URL)

    # Load this user's seed memories. LIKE 'seed%' keeps legacy bare-'seed'
    # rows in view so they can be counted as unmatchable, not silently dropped.
    rows = await (await conn.execute(
        "SELECT id, annotation, category, conversation_id "
        "FROM companion_memories "
        "WHERE user_id = %s AND conversation_id LIKE 'seed%%' AND kept = true "
        "ORDER BY created_at ASC",
        (user_id,),
    )).fetchall()

    if not rows:
        logger.info("No seed memories found for %s — nothing to do", user_id)
        await conn.close()
        return 0

    logger.info("Found %d seed memories", len(rows))

    # Load this user's exchanges, paired exactly as the seeder pairs them and
    # keyed by the same fingerprint (seed:<user message id>).
    msg_rows = await (await conn.execute(
        "SELECT id, role, content, created_at FROM companion_messages "
        "WHERE user_id = %s ORDER BY created_at ASC, id ASC",
        (user_id,),
    )).fetchall()

    exchanges: dict[str, dict] = {ex["fingerprint"]: ex for ex in pair_exchanges(msg_rows)}

    logger.info("Loaded %d conversation exchanges for matching", len(exchanges))

    # Load already-processed IDs
    done_ids = await _get_reannotated_ids(conn)
    logger.info("Already re-annotated: %d memories (will skip)", len(done_ids))

    # Filter to un-processed memories
    pending = [r for r in rows if str(r[0]) not in done_ids]
    logger.info("Pending re-annotation: %d memories", len(pending))

    if not pending:
        logger.info("All seed memories already re-annotated — nothing to do")
        await conn.close()
        return 0

    updated = 0
    would_update = 0
    skipped_no_fingerprint = 0
    skipped_no_match = 0
    skipped_no_improvement = 0
    write_failures = 0

    async with httpx.AsyncClient(timeout=120.0) as client:
        # Process in batches of BATCH_SIZE
        for batch_start in range(0, len(pending), BATCH_SIZE):
            batch = pending[batch_start:batch_start + BATCH_SIZE]
            logger.info("Processing batch %d-%d of %d...",
                        batch_start + 1, batch_start + len(batch), len(pending))

            for mem_row in batch:
                mem_id = str(mem_row[0])
                existing_annotation = mem_row[1] or ""
                category = mem_row[2] or "Mission Records"
                conversation_id = mem_row[3] or ""

                existing_score = annotation_quality_score(existing_annotation)
                logger.info("  Memory %s | score=%.2f | %s",
                            mem_id[:12], existing_score, existing_annotation[:60])

                # Find the exchange by exact fingerprint only
                if not conversation_id.startswith(SEED_FINGERPRINT_PREFIX):
                    logger.info("    -> No exchange fingerprint (legacy %r) — skipping, "
                                "cannot be matched safely", conversation_id)
                    skipped_no_fingerprint += 1
                    done_ids.add(mem_id)  # permanently unresolvable
                    continue
                exchange = _match_exchange(conversation_id, exchanges)
                if not exchange:
                    logger.info("    -> No exchange with fingerprint %s for %s — skipping",
                                conversation_id, user_id)
                    skipped_no_match += 1
                    done_ids.add(mem_id)
                    continue

                logger.info("    -> Matched exchange: %s", exchange["user"][:60])

                # Re-annotate
                try:
                    new_annotation = await _call_annotator(
                        client, exchange["user"], exchange["assistant"], category
                    )
                except Exception as e:
                    logger.warning("    -> LLM call failed (%s)", type(e).__name__)
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

                if not args.rewrite:
                    would_update += 1
                    logger.info("    -> DRY RUN would update %.2f -> %.2f\n"
                                "       old: %s\n       new: %s",
                                existing_score, new_score, existing_annotation, new_annotation)
                    await asyncio.sleep(SLEEP_BETWEEN_CALLS)
                    continue

                # Write the improved annotation, preserving the old one
                if await _replace_annotation(conn, mem_id, user_id, new_annotation):
                    updated += 1
                    done_ids.add(mem_id)
                    logger.info("    -> UPDATED: %.2f -> %.2f", existing_score, new_score)
                else:
                    write_failures += 1

                await asyncio.sleep(SLEEP_BETWEEN_CALLS)

            # Save progress after each batch (checkpoint) — only when writing
            if args.rewrite:
                if await _save_reannotated_ids(conn, done_ids):
                    logger.info("Batch done. Progress saved (%d total marked done)", len(done_ids))
                else:
                    logger.warning("Batch done. Progress NOT saved (%d marked done in memory)",
                                   len(done_ids))

    await conn.close()

    logger.info("=== RE-ANNOTATION COMPLETE (%s) ===", mode)
    logger.info("  Updated:                 %d", updated)
    logger.info("  Would update (dry run):  %d", would_update)
    logger.info("  Skipped (no fingerprint):%d", skipped_no_fingerprint)
    logger.info("  Skipped (no match):      %d", skipped_no_match)
    logger.info("  Skipped (no improve):    %d", skipped_no_improvement)
    logger.info("  Write failures:          %d", write_failures)
    logger.info("  Total processed:         %d", len(pending))
    return 1 if write_failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
