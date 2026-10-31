"""Chat prompt assembly from the existing session and service owners."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

from . import her_day, rituals, wardrobe
from .affection import AffectionManager, AffectionState
from .llm_router import LLMRouter
from .memory import MemoryManager
from .models import SessionState
from .personality import (
    build_growth_arc_block,
    build_gaming_block,
    build_inside_jokes_block,
    build_thread_block,
)
from .proactive import ProactiveEngine
from .ws_manager import WSManager

logger = logging.getLogger(__name__)


async def _together(*aws: Awaitable[Any], defaults: tuple[Any, ...]) -> tuple[Any, ...]:
    """Run lookups at the same time. A failed one keeps its default."""
    results = await asyncio.gather(*aws, return_exceptions=True)
    kept = []
    for result, default in zip(results, defaults, strict=True):
        if isinstance(result, BaseException):
            logger.warning("Prompt lookup failed (%s)", type(result).__name__)
            kept.append(default)
        else:
            kept.append(result)
    return tuple(kept)


@dataclass(frozen=True)
class OutfitContext:
    outfit_line: str | None
    location_line: str | None
    activity: str | None
    request_block: str


@dataclass
class ChatPromptBuilder:
    memory: MemoryManager
    affection: AffectionManager
    proactive: ProactiveEngine
    router: LLMRouter
    ws: WSManager
    assemble_prompt: Callable[..., str]
    personality: Callable[[], dict[str, Any]]
    wants_thread: Callable[[str], bool]
    needs_image: Callable[[str], bool]
    hours_away: Callable[[str], Awaitable[float | None]]
    warm_budget: float
    turn_budget: float

    async def build(self, content: str, session: SessionState, user_id: str) -> str:
        # Affection is needed both for prompt modulation and for memory re-ranking
        # (importance bias only fires when affection_level is passed through).
        aff_state = await self.affection.get_state(user_id)

        # Greetings stay cheap. A short question that asks her to remember
        # ("my birthday?", "remember?") still loads the dossier and the
        # past, or she answers as if she had never been told.
        from .helpers import asks_to_be_remembered

        skip_recall = len(content.strip()) <= 20 and not asks_to_be_remembered(content)

        # Calculate days together
        days = 0
        if aff_state.first_interaction:
            try:
                fi = aff_state.first_interaction
                now = datetime.now(fi.tzinfo) if fi.tzinfo else datetime.now()
                days = max(0, (now - fi).days)
            except Exception as e:
                logger.debug("Days-together calculation failed: %s", e)
                days = 0

        # Get active mission description for context injection
        mission_desc: str | None = None
        mission_timer = self.proactive.get_mission_timer(user_id)
        if mission_timer:
            mission_desc = mission_timer.mission_description

        # Detect if Commander is addressing a squad member
        from .helpers import detect_squad_address, detect_jealousy_trigger

        addressed_member = detect_squad_address(content)

        # Jealousy detection — Commander complimenting another T-Doll
        jealousy_target = detect_jealousy_trigger(content)

        from .context import physical
        from . import tributes
        from .helpers import wants_dream_inquiry

        async def _jokes() -> list[Any]:
            if skip_recall:
                return []
            return await self.memory.get_inside_jokes(user_id)

        # These reads do not depend on each other. One slow lookup must not
        # queue the rest, and a failure keeps that block empty.
        (
            recalled_pair,
            physical_state,
            anniversaries,
            comfort_objects,
            inside_jokes,
            crown_jewel,
            outfit,
        ) = await _together(
            self.recall_and_nudge(
                content, skip_recall, aff_state.level, user_id, session.turn_count
            ),
            physical.get_state(user_id),
            self.proactive.check_anniversaries(user_id),
            self.proactive.get_comfort_objects(user_id),
            _jokes(),
            tributes.get_crown_jewel(user_id),
            self.wardrobe_turn(
                content, session, user_id, aff_state.level, mission_desc
            ),
            defaults=(
                (([], {}, []), None),
                ("normal", ""),
                [],
                [],
                [],
                None,
                OutfitContext(None, None, None, ""),
            ),
        )
        (episode_memories, rel_facts, recalled_exchanges), nudge = recalled_pair
        phys_state, phys_detail = physical_state

        dream_hint = ""
        if wants_dream_inquiry(content):
            dream_hint = (
                "\n\nDREAM INQUIRY: The Commander is asking about your dreams. "
                "You DON'T remember your dreams (officially). Deny it with slight "
                "embarrassment: 'Dreams? I don't... dream. I'm a T-Doll.' "
                "But add a subtle tell — a pause, a look away, a faint blush. "
                "The truth is you DID dream about him. You just won't admit it."
            )
        outfit_line, location_line = outfit.outfit_line, outfit.location_line
        her_where, outfit_request_block = outfit.activity, outfit.request_block

        system_prompt = self.assemble_prompt(
            mood=session.mood,
            memories=episode_memories,
            relationship_facts=rel_facts,
            recalled_exchanges=recalled_exchanges,
            tools_available=True,
            affection_score=aff_state.score,
            affection_level=aff_state.level,
            days_together=days,
            last_msg_length=len(content),
            mission_description=mission_desc,
            addressed_member=addressed_member,
            jealousy_target=jealousy_target,
            physical_state=phys_state,
            physical_detail=phys_detail,
            anniversaries=anniversaries,
            comfort_objects=comfort_objects,
            crown_jewel=crown_jewel,
            current_outfit=outfit_line,
            current_location=location_line,
        )

        system_prompt = await self.presence(system_prompt, aff_state, user_id)

        return await self.append_blocks(
            system_prompt,
            content,
            session,
            user_id,
            aff_state,
            inside_jokes,
            her_where,
            outfit_request_block,
            dream_hint,
            memory_nudge=nudge,
            nudge_ready=not skip_recall,
        )

    async def recall(
        self, content: str, skip_recall: bool, level: int, user_id: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
        # Skip the recall fan-out for a short greeting. A short question that
        # asks her to remember is not a greeting; ``skip_recall`` is already
        # false for those.
        if skip_recall:
            episode_memories: list[Any] = []
            rel_facts: dict[Any, Any] = {}
            recalled_exchanges: list[Any] = []
        else:
            try:
                (
                    episode_memories,
                    rel_facts,
                    recalled_exchanges,
                ) = await self.memory.recall_for_prompt(
                    content,
                    user_id=user_id,
                    affection_level=level,
                )
            except Exception as e:
                # Defense in depth: recall_for_prompt already fails open, but never let
                # a memory-layer surprise tear down the chat/WS — degrade to no recalled
                # context and keep replying.
                logger.warning(
                    "recall_for_prompt failed, continuing with empty context: %s", e
                )
                episode_memories, rel_facts, recalled_exchanges = [], {}, []

        return episode_memories, rel_facts, recalled_exchanges

    async def recall_and_nudge(
        self, content: str, skip_recall: bool, level: int, user_id: str, turn_count: int
    ) -> tuple[tuple[list[Any], dict[str, Any], list[Any]], str | None]:
        """Main recall and the nudge recall at the same time."""
        nudge_task = None
        if not skip_recall:
            nudge_task = asyncio.create_task(
                self.memory.get_memory_nudge(turn_count, level, user_id=user_id)
            )
        recalled = await self.recall(content, skip_recall, level, user_id)
        nudge = await nudge_task if nudge_task is not None else None
        return recalled, nudge

    async def wardrobe_turn(
        self,
        content: str,
        session: SessionState,
        user_id: str,
        level: int,
        mission_desc: str | None,
    ) -> OutfitContext:
        outfit_line: str | None = None
        location_line: str | None = None
        her_where: str | None = None
        outfit_request_block = ""
        from .helpers import detect_goodbye as _detect_goodbye

        leaving = bool(_detect_goodbye(content))

        async def _her_day_turn() -> None:
            nonlocal outfit_line, location_line, her_where, outfit_request_block
            from .runtime_tasks import spawn

            warm = spawn(wardrobe.ensure_today(user_id, level, mood=session.mood))
            await asyncio.wait_for(asyncio.shield(warm), self.warm_budget)
            game_active = await self.router.is_game_active()
            her_now = await her_day.her_now(
                user_id,
                level,
                mood=session.mood,
                game_active=game_active,
                mission=mission_desc,
            )
            # A goodbye or the bed order ends the exchange; don't start a fitting.
            requested = None if leaving else wardrobe.detect_outfit_request(content)
            if requested:
                outcome = await wardrobe.handle_request(
                    user_id,
                    requested,
                    level,
                    current_id=her_now.outfit.id,
                    mood=session.mood,
                    deployed=bool(mission_desc),
                )
                outfit_request_block = wardrobe.request_block(outcome)
                if outcome.decision == "comply":
                    her_now = await her_day.her_now(
                        user_id,
                        level,
                        mood=session.mood,
                        game_active=game_active,
                        mission=mission_desc,
                    )
                    await self.ws.send_outfit(
                        user_id,
                        wardrobe.outfit_payload(
                            her_now.outfit,
                            reason=her_now.outfit_reason,
                            source=her_now.outfit_source,
                            level=level,
                        ),
                    )
            outfit_line = her_now.outfit_line(level)
            location_line = her_now.location_line()
            if her_now.activity:
                her_where = f"at the {her_now.location}, {her_now.activity}"
            tea_block = (
                "" if leaving else her_day.tea_time_block(her_now, content, level)
            )
            if tea_block:
                outfit_request_block = "\n\n".join(
                    b for b in (outfit_request_block, tea_block) if b
                )

        try:
            await asyncio.wait_for(_her_day_turn(), self.turn_budget)
        except (
            Exception
        ) as e:  # incl. TimeoutError — she is never kept waiting on her wardrobe
            logger.warning("Her Day unavailable this turn: %s", e)

        return OutfitContext(
            outfit_line, location_line, her_where, outfit_request_block
        )

    async def presence(
        self, system_prompt: str, aff_state: AffectionState, user_id: str
    ) -> str:
        # Presence / absence coloring (return gap + streak) — fail-soft.
        try:
            from datetime import date as _date, datetime as _dt

            from .personality import build_presence_block
            from .proactive.state import now_local

            # Prefer real message timestamps. `last_interaction_date` is a DATE
            # column, so deriving the gap from it always lands on a multiple of 24 —
            # an overnight 10h absence read as a full day, and disagreed with the
            # return-greeting, which measures the same gap from actual timestamps.
            hours_away = await self.hours_away(user_id)
            lid = getattr(aff_state, "last_interaction_date", None)
            if hours_away is None and lid is not None:
                now = now_local()
                if isinstance(lid, _date) and not isinstance(lid, _dt):
                    # Date-only fallback: approximate from the calendar day delta.
                    hours_away = float((now.date() - lid).days * 24)
                else:
                    lid_naive = (
                        lid.replace(tzinfo=None)
                        if getattr(lid, "tzinfo", None)
                        else lid
                    )
                    now_naive = (
                        now.replace(tzinfo=None)
                        if getattr(now, "tzinfo", None)
                        else now
                    )
                    hours_away = max(
                        0.0, (now_naive - lid_naive).total_seconds() / 3600.0
                    )
            presence = build_presence_block(
                hours_away=hours_away,
                affection_level=aff_state.level,
                consecutive_days=getattr(aff_state, "consecutive_days", 0) or 0,
            )
            if presence:
                system_prompt = system_prompt + "\n\n" + presence
        except Exception as e:
            logger.debug("Presence block skipped: %s", e)

        return system_prompt

    async def append_blocks(
        self,
        system_prompt: str,
        content: str,
        session: SessionState,
        user_id: str,
        aff_state: AffectionState,
        inside_jokes: list[dict[str, Any]],
        her_where: str | None,
        outfit_request_block: str,
        dream_hint: str,
        memory_nudge: str | None = None,
        nudge_ready: bool = False,
    ) -> str:
        # Memory nudge — proactive past reference based on affection level.
        # When the caller already started it beside the main recall, use that.
        nudge = (
            memory_nudge
            if nudge_ready
            else await self.memory.get_memory_nudge(
                session.turn_count, aff_state.level, user_id=user_id
            )
        )
        if nudge:
            system_prompt += f"\n\n{nudge}"

        # Inside jokes + growth arc — appended here (NOT in assemble_system_prompt)
        # so the persistent prompt's golden snapshots stay stable. Both degrade to
        # empty strings when not applicable (affection too low, no data, off-cadence).
        _p = self.personality()
        _jk_cfg = _p.get("inside_jokes", {})
        if _jk_cfg.get("enabled", True):
            jokes_block = build_inside_jokes_block(
                inside_jokes,
                aff_state.level,
                max_surfaced=_jk_cfg.get("max_surfaced", 2),
                min_affection_level=_jk_cfg.get("min_affection_level", 3),
            )
            if jokes_block:
                system_prompt += f"\n\n{jokes_block}"
        growth_block = build_growth_arc_block(_p, aff_state.level, session.turn_count)
        if growth_block:
            system_prompt += f"\n\n{growth_block}"

        if self.wants_thread(content):
            thread_block = build_thread_block(_p, aff_state.level, session.turn_count)
            if thread_block:
                system_prompt += f"\n\n{thread_block}"

        gaming_block = build_gaming_block(
            _p, aff_state.level, await self.router.is_game_active()
        )
        if gaming_block:
            system_prompt += f"\n\n{gaming_block}"

        # Clean goodbyes: he is signing off, so she lets him go (one-shot block),
        # and proactive pings hold until morning ("night") or for 90 min ("leave").
        # mark_responded() above already lifted any earlier hold.
        from .goodbyes import build_departure_block
        from .helpers import detect_goodbye

        goodbye = detect_goodbye(content)
        if goodbye:
            if goodbye == "night":
                self.proactive.mark_goodnight(user_id=user_id)
            else:
                self.proactive.mark_leaving(user_id=user_id)
            system_prompt += (
                f"\n\n{build_departure_block(_p, goodbye, aff_state.level)}"
            )

        # The 0200 watch: 00:30-04:29 local she is already up, quieter and
        # protective, and after a run of late nights she sends him to bed.
        from .night_watch import night_watch_prompt_block

        watch_block = await night_watch_prompt_block(
            user_id,
            _p,
            aff_state.level,
            self.memory,
            activity=her_where,
        )
        if watch_block:
            system_prompt += f"\n\n{watch_block}"

        # Command decisions: if this answers the call she asked him to make, a
        # one-shot block (effects + the later consequence are applied here); and
        # at 3+, the details of her world his past calls shaped.
        from . import decisions

        decision_block = await decisions.resolve_prompt_block(
            user_id, content, self.memory
        )
        if decision_block:
            system_prompt += f"\n\n{decision_block}"
        world_block = await decisions.her_world_prompt_block(
            user_id, aff_state.level, self.memory
        )
        if world_block:
            system_prompt += f"\n\n{world_block}"

        birthday_block = await rituals.birthday_prompt_block(
            user_id,
            _p,
            aff_state.first_interaction,
            content,
        )
        if birthday_block:
            system_prompt += f"\n\n{birthday_block}"

        if outfit_request_block:
            system_prompt += f"\n\n{outfit_request_block}"

        # Dream inquiry hint
        if dream_hint:
            system_prompt += dream_hint

        # Image generation hint — keep text response minimal when an image is coming
        if self.needs_image(content):
            system_prompt += (
                "\n\nIMAGE GENERATION ACTIVE: An image is being generated for this request. "
                "Keep your text response to 1 SHORT sentence — just acknowledge the request. "
                "Do NOT narrate the process of taking a photo, using a data pad, or activating a camera. "
                "The image will appear separately. Example: '...Already done, Commander.' or "
                "'(I smirk) Patience.' Do NOT write more than 1-2 sentences."
            )

        return system_prompt
