"""Tier 3 factual memory (aichat-data /memory) — split from app/memory.py.

Relationship facts, inside jokes and milestones all live in the data
service's key/value store under ``companion:{user_id}:...``. The methods
are a mixin so ``MemoryManager`` (app/memory.py) keeps them verbatim and
``context.memory`` is still the one instance tests patch.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

DATA_URL = os.environ.get("DATA_URL", "http://aichat-data:8091")


class MemoryFactsMixin:
    """Tier 3 store methods; the host class owns the ``_http`` client."""

    _http: httpx.AsyncClient | None

    # ── Tier 3: Factual Memory (aichat-data /memory) ────────────────────

    async def store_fact(self, key: str, value: str, ttl: int | None = None,
                         user_id: str = "jalsarraf") -> None:
        try:
            body: dict = {"key": f"companion:{user_id}:{key}", "value": value}
            if ttl:
                body["ttl_seconds"] = ttl
            await self._http.post(f"{DATA_URL}/memory/store", json=body)
        except Exception as e:
            logger.warning("store_fact failed for %s: %s", key, e)

    async def recall_fact(self, key: str, user_id: str = "jalsarraf") -> str | None:
        try:
            r = await self._http.get(
                f"{DATA_URL}/memory/recall", params={"key": f"companion:{user_id}:{key}"}
            )
            data = r.json()
            if data.get("found"):
                return data.get("value")
        except Exception as e:
            logger.warning("recall_fact failed for %s: %s", key, e)
        return None

    async def recall_facts_by_pattern(self, pattern: str,
                                       user_id: str = "jalsarraf") -> list[dict]:
        # Fail open like recall_fact/store_fact — this backs inside-jokes, the
        # Commander dossier, and milestones on the live chat read path, so a
        # data-service hiccup must return empty, not raise.
        try:
            r = await self._http.get(
                f"{DATA_URL}/memory/recall",
                params={"pattern": f"companion:{user_id}:{pattern}"},
            )
            r.raise_for_status()
            data = r.json()
            return data.get("entries", [])
        except Exception as e:
            logger.warning("recall_facts_by_pattern failed (%s): %s", pattern, e)
            return []

    # ── Relationship facts (convenience) ─────────────────────────────────

    async def get_relationship_facts(self, user_id: str = "jalsarraf") -> dict:
        entries = await self.recall_facts_by_pattern("rel:%", user_id=user_id)
        # Strip the full prefix: companion:{user_id}:rel:
        prefix = f"companion:{user_id}:rel:"
        out: dict = {}
        for e in entries:
            short = e["key"].replace(prefix, "")
            # Inside jokes live under rel:joke:* and surface via their own
            # per-message block — exclude them so the Commander dossier stays
            # clean and they aren't double-injected.
            if short.startswith("joke:"):
                continue
            out[short] = e["value"]
        return out

    async def set_relationship_fact(self, key: str, value: str,
                                     user_id: str = "jalsarraf") -> None:
        await self.store_fact(f"rel:{key}", value, user_id=user_id)

    # ── Inside jokes / running references ────────────────────────────────
    # Reuses the relationship-fact store (companion_relationship) under a
    # dedicated "joke:" sub-namespace so running references stay separate
    # from the Commander dossier yet need no new table. Key is a slug of the
    # label; value is the note (how the bit is used).

    @staticmethod
    def _joke_slug(label: str) -> str:
        """Stable, store-safe slug for an inside-joke label."""
        slug = "".join(c if c.isalnum() else "_" for c in label.strip().lower())
        slug = "_".join(filter(None, slug.split("_")))  # collapse repeats
        return slug[:60] or "ref"

    async def set_inside_joke(self, label: str, note: str,
                              user_id: str = "jalsarraf") -> None:
        """Store/refresh a relationship-specific running reference.

        Stored as ``rel:joke:<slug>`` -> ``"<label> :: <note>"`` so the label
        survives the round-trip even though the key is slugged. Re-storing the
        same label overwrites (keeps the latest phrasing), which doubles as a
        natural recency signal."""
        if not label or not label.strip() or not note or not note.strip():
            return
        slug = self._joke_slug(label)
        await self.store_fact(
            f"rel:joke:{slug}", f"{label.strip()} :: {note.strip()}", user_id=user_id
        )

    async def get_inside_jokes(self, user_id: str = "jalsarraf") -> list[dict]:
        """Return all stored running references as [{label, note}] dicts.

        Order follows whatever the data service returns; callers cap the count
        themselves. Malformed rows (missing the '::' separator) degrade to a
        label-only entry rather than being dropped."""
        entries = await self.recall_facts_by_pattern("rel:joke:%", user_id=user_id)
        jokes: list[dict] = []
        for e in entries:
            raw = e.get("value", "") or ""
            if " :: " in raw:
                label, note = raw.split(" :: ", 1)
            else:
                label, note = raw, ""
            label = label.strip()
            if label:
                jokes.append({"label": label, "note": note.strip()})
        return jokes

    # ── Milestone tracking ────────────────────────────────────────────────

    async def record_milestone(self, milestone: str, user_id: str = "jalsarraf") -> bool:
        """Record a relationship milestone. Returns True if new."""
        existing = await self.recall_fact(f"milestone:{milestone}", user_id=user_id)
        if existing:
            return False
        await self.store_fact(f"milestone:{milestone}", datetime.now(timezone.utc).isoformat(), user_id=user_id)
        logger.info("New milestone recorded: %s for %s", milestone, user_id)
        return True

    async def get_milestones(self, user_id: str = "jalsarraf") -> dict[str, str]:
        """Get all recorded relationship milestones for a user."""
        entries = await self.recall_facts_by_pattern("milestone:%", user_id=user_id)
        prefix = f"companion:{user_id}:milestone:"
        return {
            e["key"].replace(prefix, ""): e["value"]
            for e in entries
        }
