"""Chat prompt assembly from the existing session and service owners."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
import inspect
import logging
import re
from typing import Any

from . import her_day, picture_intent, rituals, wardrobe
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
from .picture_denial import without_denied_exchanges, without_refusal_references
from .proactive import ProactiveEngine
from .ws_manager import WSManager

logger = logging.getLogger(__name__)

PICTURE_UNDERWAY = (
    "\n\nIMAGE GENERATION ACTIVE: He asked for a picture and it is being drawn for him "
    "right now by the system, a picture of what is happening for you at this moment: "
    "where you are, what you have on and what you are doing. It arrives on its own in "
    "about a minute and has not arrived yet. Keep your text response to 1 SHORT sentence "
    "saying you are taking it for him now, in your own voice. You do have the means to "
    "send it. Never refuse, never say you have no image tool, cannot send one, or that "
    "pictures are down, and ignore any earlier turn that said so. Whatever his wording, the "
    "picture is always of the current situation: never ask what he wants to see, never offer "
    "to describe it in words, and never repeat his phrasing back, just say you are taking it. "
    "Never say it is done, "
    "sent or already there. If the drawing fails he is told separately after the attempt. "
    "Do NOT narrate the process of taking a photo, using a data pad, or activating a camera."
)
PICTURE_NOT_UNDERWAY = (
    "\n\nPICTURES: You can draw and send him pictures. When he asks for one, it is drawn "
    "and arrives on its own. No picture is being drawn this turn, so never say one is "
    "coming, done or sent. If he asks whether you can draw, say yes: a picture of what is "
    "happening right now is drawn whenever he asks, so never ask him what to show. Never say you can't draw or that you have no image tool."
)


def pictures_available(user_id: str) -> bool:
    """Whether a request from this account would really be drawn."""
    from .agy_image_provider import uses_agy_images
    from .image_provider import selected_provider

    return selected_provider() == "openai" or uses_agy_images(user_id)


def picture_turn_text(*, underway: bool, available: bool, status_note: str) -> str:
    """What she may say about pictures this turn.

    A picture still being drawn must not share a turn with "nothing is being
    drawn". A new request must not share a turn with "it already arrived" or
    "it failed", or she treats the one being drawn as already refused.
    """
    note = (status_note or "").strip()
    drawing = "still being drawn" in note
    blocks: list[str] = []
    if underway:
        blocks.append(PICTURE_UNDERWAY.lstrip("\n"))
    elif not drawing and available:
        blocks.append(PICTURE_NOT_UNDERWAY.lstrip("\n"))
    if note and not (underway and not drawing):
        blocks.append(note)
    return "\n\n".join(blocks)


# Whole turn, not the first word: "good morning" is a greeting, "good idea" is not.
_CHEAP_TURNS = frozenset(
    {
        "hi",
        "hey",
        "hello",
        "yo",
        "sup",
        "ok",
        "okay",
        "k",
        "kk",
        "thanks",
        "thank you",
        "ty",
        "thx",
        "gm",
        "gn",
        "good morning",
        "good night",
        "good evening",
        "good afternoon",
        "morning",
        "night",
        "evening",
        "afternoon",
        "bye",
        "goodbye",
        "later",
        "lol",
        "lmao",
        "haha",
        "hahaha",
        "yes",
        "no",
        "yeah",
        "yep",
        "nope",
        "nah",
        "sure",
        "cool",
        "nice",
        "wow",
        "hmm",
        "huh",
        "mhm",
        "mm",
        "oh",
        "ah",
        "aw",
        "heh",
        "good",
        "great",
        "fine",
        "alright",
        "all right",
        "np",
        "yw",
        "howdy",
        "welcome",
        "cheers",
        "hiya",
        "hey there",
        "hi there",
        "hello there",
        "whats up",
        "copy",
        "roger",
        "noted",
        "gotcha",
        "understood",
        "brb",
        "ttyl",
        "idk",
        "omg",
    }
)
_QUESTION_OPENERS = frozenset(
    {"who", "whats", "what", "when", "where", "why", "how", "which", "whose", "whom"}
)
_TURN_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")
# A dream she already narrated. The official denial ("I don't... dream") does not match.
_TOLD_DREAM = re.compile(
    r"\b(?:i dreamed|i dreamt|i had a dream|in my dream|in the dream|"
    r"my dream|woke from a dream|dreamed of|dreamt of|"
    r"dreamed about|dreamt about)\b",
    re.IGNORECASE,
)


def short_turn_skips_recall(content: str) -> bool:
    """True when a turn of at most 20 characters can skip stored facts.

    Greetings stay cheap. A short question or a name still loads facts already
    stored, so "Biscuit?" is not answered as if the name was never kept.
    """
    text = content.strip()
    if len(text) > 20:
        return False
    from .helpers import asks_to_be_remembered

    if asks_to_be_remembered(text):
        return False
    return not _asks_or_names(text)


def _asks_or_names(text: str) -> bool:
    if "?" in text:
        return True
    words = _TURN_WORD.findall(text)
    if not words:
        return False
    norm = " ".join(word.lower().replace("'", "") for word in words)
    if norm in _CHEAP_TURNS:
        return False
    first = words[0].lower().replace("'", "")
    if first in _QUESTION_OPENERS or first.startswith(
        ("who", "what", "when", "where", "why", "how", "which")
    ):
        return True
    if len(words) > 3:
        return False
    if any(word[:1].isupper() for word in words):
        return True
    return len(words) == 1 and len(words[0]) >= 3


def already_told_a_dream(
    replies: list[str],
    memories: list[Any],
    exchanges: list[Any],
    summary: str | None = None,
) -> bool:
    """True when a dream she narrated is already in what this turn can see."""
    texts = [reply for reply in replies if isinstance(reply, str)]
    if isinstance(summary, str):
        texts.append(summary)
    for memory in memories:
        if isinstance(memory, str):
            texts.append(memory)
        elif isinstance(memory, dict):
            for key in ("summary", "content", "text"):
                value = memory.get(key)
                if isinstance(value, str):
                    texts.append(value)
    for exchange in exchanges:
        if not isinstance(exchange, dict):
            continue
        reply = exchange.get("assistant_content")
        if isinstance(reply, str):
            texts.append(reply)
    return any(_TOLD_DREAM.search(text) for text in texts)


def dream_inquiry_hint(*, already_told: bool) -> str:
    """Ask about dreams without denying one she has already told him.

    Unspoken, she still won't admit them. A delivered dream is not taken back.
    """
    if already_told:
        return (
            "\n\nDREAM INQUIRY: The Commander is asking about your dreams. "
            "You already told him one. Stay with that dream. Do not say you "
            "don't dream, and do not take back what you already said."
        )
    return (
        "\n\nDREAM INQUIRY: The Commander is asking about your dreams. "
        "If you already told him a dream, stay with that dream: do not say "
        "you don't dream and do not take it back. "
        "If you have not told him one, you DON'T remember your dreams "
        "(officially). Deny it with slight embarrassment: "
        "'Dreams? I don't... dream. I'm a T-Doll.' "
        "But add a subtle tell — a pause, a look away, a faint blush. "
        "The truth is you DID dream about him. You just won't admit a dream "
        "you have not already told him."
    )


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


def _recent_openings(turns: list[dict[str, Any]], keep: int = 4) -> list[str]:
    """The first few words of her last replies, so she can see how she has been starting."""
    openings = [
        " ".join(str(turn.get("content") or "").split()[:4])
        for turn in turns
        if turn.get("role") == "assistant"
    ]
    return [opening for opening in openings if opening][-keep:]


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
    needs_image: Callable[[str], bool | Awaitable[bool]]
    hours_away: Callable[[str], Awaitable[float | None]]
    warm_budget: float
    turn_budget: float

    async def build(self, content: str, session: SessionState, user_id: str) -> str:
        # Affection is needed both for prompt modulation and for memory re-ranking
        # (importance bias only fires when affection_level is passed through).
        aff_state = await self.affection.get_state(user_id)

        # Greetings stay cheap. A short question or a name still loads the
        # dossier, or "Biscuit?" is answered as if the name was never stored.
        skip_recall = short_turn_skips_recall(content)

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
        from .conversation_continuity import (
            load_recent_assistant_replies,
            merge_recent_replies,
        )
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
            stored_replies,
            presence_block,
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
            load_recent_assistant_replies(user_id, 20),
            self.presence("", aff_state, user_id),
            defaults=(
                (([], {}, []), None),
                ("normal", ""),
                [],
                [],
                [],
                None,
                OutfitContext(None, None, None, ""),
                [],
                "",
            ),
        )
        (episode_memories, rel_facts, recalled_exchanges), nudge = recalled_pair
        from .personality.memory_blocks import (
            drop_repeated_memories,
            drop_repeated_replies,
        )

        session_replies = [
            str(turn.get("content") or "")
            for turn in reversed(session.turns)
            if turn.get("role") == "assistant"
        ]
        recent_replies = merge_recent_replies(session_replies, stored_replies, limit=20)
        episode_memories = drop_repeated_memories(episode_memories, recent_replies)
        recalled_exchanges = drop_repeated_replies(recalled_exchanges, recent_replies)
        phys_state, phys_detail = physical_state

        dream_hint = ""
        if wants_dream_inquiry(content):
            # A morning return can already have told him the dream. That text
            # must not share a turn with an order to say she does not dream.
            dream_hint = dream_inquiry_hint(
                already_told=already_told_a_dream(
                    recent_replies,
                    episode_memories,
                    recalled_exchanges,
                    session.context_summary,
                )
            )
        outfit_line, location_line = outfit.outfit_line, outfit.location_line
        her_where, outfit_request_block = outfit.activity, outfit.request_block

        assembled = self.assemble_prompt(
            mood=session.mood,
            memories=episode_memories,
            relationship_facts=rel_facts,
            recalled_exchanges=without_denied_exchanges(recalled_exchanges),
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

        # The persona string from assembly is the cache prefix. Presence and
        # the per-turn blocks are appended only to the tail, never onto it.
        stable = getattr(assembled, "stable", "") or ""
        full = str(assembled)
        if stable and full.startswith(stable):
            tail = full[len(stable) :].lstrip("\n")
        else:
            tail = full
        tail += presence_block
        tail = await self.append_blocks(
            tail,
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
        tail = str(tail).lstrip("\n")
        if not stable:
            return tail
        from .personality.system_prompt import PromptText

        text = f"{stable}\n\n{tail}" if tail else stable
        return PromptText(text, stable)

    async def recall(
        self, content: str, skip_recall: bool, level: int, user_id: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
        # Skip the recall fan-out for a short greeting. A short question, a
        # name, or a turn that asks her to remember is not one; ``skip_recall``
        # is already false for those.
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
        system_prompt += (
            "\n\nTHIS TURN: Open with what you have to say, not a pause or a stage "
            "direction, unless this moment calls for one."
        )
        openings = _recent_openings(session.turns)
        if openings:
            quoted = " / ".join(f'"{opening}"' for opening in openings)
            system_prompt += f" Your last replies opened: {quoted}. Open differently."
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
                without_refusal_references(inside_jokes),
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

        # Whether a picture is underway decides what she may say about one. Her
        # words never start one, and she cannot see one arrive or fail.
        underway = self.needs_image(content)
        if inspect.isawaitable(underway):
            underway = await underway
        picture_block = picture_turn_text(
            underway=bool(underway),
            available=pictures_available(user_id),
            status_note=picture_intent.state_note(user_id),
        )
        if picture_block:
            system_prompt += f"\n\n{picture_block}"

        return system_prompt
