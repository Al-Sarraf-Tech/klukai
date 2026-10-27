"""User-scoped factual memory and relationship convenience operations."""

from __future__ import annotations
from typing import Any
import logging
from datetime import timezone
from . import fact_store

logger = logging.getLogger(__name__)


class MemoryFactsMixin:
    # Facts live in Klukai's own database (app/fact_store.py). Every operation
    # fails open: they back the dossier, inside jokes and milestones on the
    # live chat read path, so a database hiccup degrades context, never the
    # reply.

    async def store_fact(
        self, key: str, value: str, ttl: int | None = None, user_id: str = "jalsarraf"
    ) -> None:
        try:
            await fact_store.store(f"companion:{user_id}:{key}", value, ttl)
        except Exception as e:
            logger.warning("store_fact failed for %s: %s", key, e)

    async def recall_fact(self, key: str, user_id: str = "jalsarraf") -> str | None:
        try:
            entry = await fact_store.recall(f"companion:{user_id}:{key}")
        except Exception as e:
            logger.warning("recall_fact failed for %s: %s", key, e)
            return None
        value = entry.get("value") if entry else None
        return value if isinstance(value, str) else None

    async def recall_facts_by_pattern(
        self, pattern: str, user_id: str = "jalsarraf"
    ) -> list[dict[str, Any]]:
        # The caller's pattern may use LIKE wildcards; the user scope never
        # does, so a user id containing % or _ cannot reach another user's rows.
        scope = fact_store.escape_like(f"companion:{user_id}:")
        try:
            return await fact_store.recall_pattern(scope + pattern)
        except Exception as e:
            logger.warning("recall_facts_by_pattern failed (%s): %s", pattern, e)
            return []

    # ── Relationship facts (convenience) ─────────────────────────────────

    async def get_relationship_facts(
        self, user_id: str = "jalsarraf"
    ) -> dict[str, Any]:
        entries = await self.recall_facts_by_pattern("rel:%", user_id=user_id)
        # Strip the full prefix: companion:{user_id}:rel:
        prefix = f"companion:{user_id}:rel:"
        out: dict[str, Any] = {}
        for e in entries:
            short = e["key"].replace(prefix, "")
            # Inside jokes live under rel:joke:* and surface via their own
            # per-message block — exclude them so the Commander dossier stays
            # clean and they aren't double-injected.
            if short.startswith("joke:"):
                continue
            out[short] = e["value"]
        return out

    async def set_relationship_fact(
        self, key: str, value: str, user_id: str = "jalsarraf"
    ) -> None:
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

    async def set_inside_joke(
        self, label: str, note: str, user_id: str = "jalsarraf"
    ) -> None:
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

    async def get_inside_jokes(
        self, user_id: str = "jalsarraf"
    ) -> list[dict[str, Any]]:
        """Return all stored running references as [{label, note}] dicts.

        Order follows whatever the data service returns; callers cap the count
        themselves. Malformed rows (missing the '::' separator) degrade to a
        label-only entry rather than being dropped."""
        entries = await self.recall_facts_by_pattern("rel:joke:%", user_id=user_id)
        jokes: list[dict[str, Any]] = []
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

    async def record_milestone(
        self, milestone: str, user_id: str = "jalsarraf"
    ) -> bool:
        """Record a relationship milestone once. True only if this call
        recorded it; an existing first date is never overwritten, and a
        database error reports False rather than a milestone that was not
        stored."""
        from . import memory

        # Aware UTC: the app renders milestones in his local time, and a
        # naive container-clock stamp cannot be converted.
        when = memory.datetime.now(timezone.utc).isoformat()
        try:
            new = await fact_store.insert_if_absent(
                f"companion:{user_id}:milestone:{milestone}", when
            )
        except Exception as e:
            logger.warning("record_milestone failed for %s: %s", milestone, e)
            return False
        if new:
            logger.info("New milestone recorded: %s for %s", milestone, user_id)
        return new

    async def get_milestones(self, user_id: str = "jalsarraf") -> dict[str, str]:
        """Get all recorded relationship milestones for a user."""
        entries = await self.recall_facts_by_pattern("milestone:%", user_id=user_id)
        prefix = f"companion:{user_id}:milestone:"
        return {e["key"].replace(prefix, ""): e["value"] for e in entries}
