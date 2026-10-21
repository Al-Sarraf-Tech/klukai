"""Her journal entry for one turn's picture, joined to that picture.

The picture (the account worker, 20-60 s) almost always lands after the turn's
extraction (about 8 s), so the curation cannot go to "the latest picture": at
that moment the latest picture is the previous one, which then took this
turn's caption and keep/discard. A turn that asks for a picture gets one
PictureCuration; the picture job hands it the memory id, the extraction hands
it the curation, and whichever arrives second applies it, once.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any

logger = logging.getLogger(__name__)


class PictureCuration:
    def __init__(self, apply: Callable[..., Awaitable[bool]], user_id: str) -> None:
        self._apply = apply
        self._user_id = user_id
        self._memory_id: str | None = None
        self._curation: tuple[dict[str, Any], int] | None = None
        self._applied = False

    async def picture_saved(self, memory_id: str) -> None:
        """The picture is in her album under ``memory_id``."""
        if self._memory_id is None:
            self._memory_id = memory_id
            await self._apply_once()

    async def curation_ready(
        self, curation: dict[str, Any], affection_level: int
    ) -> None:
        """The extraction wrote her keep, caption and category for it."""
        if self._curation is None:
            self._curation = (curation, affection_level)
            await self._apply_once()

    async def _apply_once(self) -> None:
        if self._applied or self._memory_id is None or self._curation is None:
            return
        self._applied = True
        curation, level = self._curation
        try:
            await self._apply(self._memory_id, curation, level, user_id=self._user_id)
        except Exception as e:
            logger.warning(
                "Memory curation update failed for %s: %s", self._memory_id, e
            )
