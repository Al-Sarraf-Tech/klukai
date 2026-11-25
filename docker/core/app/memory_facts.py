"""User-scoped factual memory and relationship convenience operations."""

from __future__ import annotations
from typing import Any
import logging
from datetime import timezone
from . import fact_store
from .picture_denial import refusal_reference

logger = logging.getLogger(__name__)

# A relationship fact is one short line, not a transcript.
MAX_FACT_TEXT = 1000


class FactControlError(ValueError):
    """correct/forget/list refused. ``code`` is not_found, invalid, or unavailable."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def relationship_storage_key(user_id: str, fact_id: str) -> str | None:
    """Full ``memory`` key for one of this user's ``rel:`` facts.

    ``fact_id`` is the short id (``favorite_drink``, ``joke:tea``), never a
    storage key and never another user's row. None means it is not his id.
    """
    if not isinstance(user_id, str) or not user_id or user_id != user_id.strip():
        return None
    if not isinstance(fact_id, str) or not fact_id or fact_id != fact_id.strip():
        return None
    if len(fact_id) > 80 or "\x00" in fact_id or ".." in fact_id:
        return None
    if any(ch in fact_id for ch in "/\\%?#"):
        return None
    if fact_id.startswith("companion:"):
        return None
    return f"companion:{user_id}:rel:{fact_id}"


def retired_marker_key(user_id: str, fact_id: str) -> str | None:
    """Permanent marker that blocks a forgotten ``rel:`` fact from being stored again.

    It is not itself a ``rel:`` row, so list and dossier recall do not show it.
    """
    if relationship_storage_key(user_id, fact_id) is None:
        return None
    return f"companion:{user_id}:retired-rel:{fact_id}"


def clean_fact_text(value: object) -> str | None:
    """Corrected wording, or None if it is empty, too long, or not text.

    Blank text is not a forget. Forget is a separate operation.
    """
    if not isinstance(value, str) or "\x00" in value:
        return None
    text = value.strip()
    if not text or len(text) > MAX_FACT_TEXT:
        return None
    return text


def relationship_facts_from_rows(
    rows: list[dict[str, Any]], user_id: str
) -> list[dict[str, str]]:
    """His live ``rel:`` rows as ``{id, value, ts}``.

    A key that does not belong to ``user_id``, or that is not a ``rel:`` fact,
    is dropped. Order is the caller's order.
    """
    if not isinstance(user_id, str) or not user_id:
        return []
    out: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row.get("key")
        if not isinstance(key, str):
            continue
        prefix = f"companion:{user_id}:rel:"
        if not key.startswith(prefix):
            continue
        fact_id = key.removeprefix(prefix)
        if relationship_storage_key(user_id, fact_id) != key:
            continue
        value = row.get("value")
        if not isinstance(value, str) or not value:
            continue
        out.append({"id": fact_id, "value": value, "ts": str(row.get("ts", ""))})
    return out


class MemoryFactsMixin:
    # Facts live in Klukai's own database (app/fact_store.py). Every operation
    # fails open: they back the dossier, inside jokes and milestones on the
    # live chat read path, so a database hiccup degrades context, never the
    # reply.

    async def _relationship_fact_retired(self, key: str, user_id: str) -> bool:
        """True when ``key`` is a ``rel:`` fact he forgot.

        A lookup failure stays open, matching ``store_fact``: the write itself
        still fails open if the database is down. A present marker does not.
        """
        if not isinstance(key, str) or not key.startswith("rel:"):
            return False
        marker = retired_marker_key(user_id, key.removeprefix("rel:"))
        if marker is None:
            return False
        try:
            return await fact_store.recall(marker) is not None
        except Exception as e:
            logger.warning("retired-fact check failed for %s: %s", key, e)
            return False

    async def store_fact(
        self, key: str, value: str, ttl: int | None = None, user_id: str = "jalsarraf"
    ) -> None:
        try:
            if await self._relationship_fact_retired(key, user_id):
                return
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
        prefix = f"companion:{user_id}:"
        try:
            rows = await fact_store.recall_pattern(scope + pattern)
        except Exception as e:
            logger.warning("recall_facts_by_pattern failed (%s): %s", pattern, e)
            return []
        # LIKE is not the authority: a pattern that still matches another
        # user's key must not become one of his facts.
        return [row for row in rows if str(row.get("key", "")).startswith(prefix)]

    # ── Relationship facts (convenience) ─────────────────────────────────

    async def get_relationship_facts(
        self, user_id: str = "jalsarraf"
    ) -> dict[str, Any]:
        entries = await self.recall_facts_by_pattern("rel:%", user_id=user_id)
        # Strip the full prefix: companion:{user_id}:rel:
        prefix = f"companion:{user_id}:rel:"
        out: dict[str, Any] = {}
        for e in entries:
            key = str(e.get("key", ""))
            if not key.startswith(prefix):
                continue
            short = key.removeprefix(prefix)
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

    async def list_relationship_facts(
        self, user_id: str = "jalsarraf"
    ) -> list[dict[str, str]]:
        """His ``rel:`` rows, including inside jokes. Not another user's.

        Unlike dossier recall, a database error is raised. An empty list is
        a real empty list, not a hidden outage.
        """
        if not isinstance(user_id, str) or not user_id:
            raise FactControlError("not_found")
        scope = fact_store.escape_like(f"companion:{user_id}:")
        try:
            rows = await fact_store.recall_pattern(scope + "rel:%")
        except Exception:
            logger.warning("relationship fact list unavailable")
            raise FactControlError("unavailable") from None
        return relationship_facts_from_rows(rows, user_id)

    async def correct_relationship_fact(
        self, fact_id: str, value: str, user_id: str = "jalsarraf"
    ) -> dict[str, str]:
        """Replace his fact's text. The old wording is what recall no longer returns.

        A fact id that is not his live ``rel:`` row is not found. This does
        not insert a row and does not read or write conversation messages.
        """
        key = relationship_storage_key(user_id, fact_id)
        text = clean_fact_text(value)
        if key is None:
            raise FactControlError("not_found")
        if text is None:
            raise FactControlError("invalid")
        try:
            replaced = await fact_store.replace_live(key, text)
        except Exception:
            logger.warning("relationship fact correction unavailable")
            raise FactControlError("unavailable") from None
        if not replaced:
            raise FactControlError("not_found")
        return {"id": fact_id, "value": text}

    async def forget_relationship_fact(
        self, fact_id: str, user_id: str = "jalsarraf"
    ) -> dict[str, Any]:
        """Retire his fact from recall. Conversation rows are not deleted.

        The marker stops a later ``rel:`` store from writing the old wording
        back. An id that is not his live fact is not found, and no marker is
        written for it.
        """
        key = relationship_storage_key(user_id, fact_id)
        marker = retired_marker_key(user_id, fact_id)
        if key is None or marker is None:
            raise FactControlError("not_found")
        try:
            live = await fact_store.recall(key)
            if not live:
                raise FactControlError("not_found")
            await fact_store.store(marker, "retired")
            retired = await fact_store.retire(key)
        except FactControlError:
            raise
        except Exception:
            logger.warning("relationship fact forget unavailable")
            raise FactControlError("unavailable") from None
        if not retired:
            raise FactControlError("unavailable")
        return {"id": fact_id, "forgotten": True}

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
        if refusal_reference(label, note):
            return  # a running joke must never teach her to refuse a picture
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
        out: dict[str, str] = {}
        for e in entries:
            key = str(e.get("key", ""))
            if key.startswith(prefix):
                out[key.removeprefix(prefix)] = e["value"]
        return out
