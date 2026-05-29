"""Affection system: tracks relationship progression between Klukai and the Commander."""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime

import httpx
from pydantic import BaseModel

from .db import get_conn, get_conn_autocommit
from .events import publish as publish_event
from .personality import load_personality

logger = logging.getLogger(__name__)

# Affection classification uses gpt-oss-20b — reliable JSON, uncensored.
# Only used as legacy fallback — primary path is merged extraction via extract_facts().
LM_STUDIO_URL = os.environ.get("LM_STUDIO_URL", "http://192.168.50.2:1234")
CLASSIFICATION_MODEL = "cognitivecomputations_dolphin-mistral-24b-venice-edition"

DAILY_POINTS_CAP = 8  # fallback only; config affection.scoring.daily_points_cap is authoritative
MAX_SCORE = 1000

CLASSIFICATION_PROMPT = """\
Classify ONLY the Commander's message below. Ignore Klukai's response tone entirely.
The Commander is talking to a cold military character — direct or casual language is NORMAL.

Choose ONE type and rate intensity (1-10):
- "greeting": Hello, goodbye, check-in, how are you (intensity 1-3)
- "genuine_interest": Asking about Klukai, her missions, squad, wellbeing, history (intensity 1-5)
- "personal_sharing": Commander sharing personal details or feelings (intensity 1-5)
- "compliment": Praise, admiration, appreciation of Klukai (intensity 3-8)
- "mission_discussion": Tactical, operational, strategic, or factual conversation (intensity 1-5)
- "remembering": Recalling something from a previous conversation (intensity 3-5)
- "rude": ONLY explicit insults, slurs, "shut up", "you're useless", "I don't need you" (intensity 5-10)
- "inappropriate": ONLY sexually explicit, objectifying, or degrading content (intensity 5-10)
- "ignoring_advice": ONLY explicitly saying "no I won't do that" to Klukai's direct counsel (intensity 3-5)
- "neutral": Normal conversation, questions, small talk (intensity 1-3)

IMPORTANT: When in doubt, classify as "neutral" or "genuine_interest". Short messages, questions,
and casual conversation are NEVER "rude". Only classify as "rude" if the Commander is clearly hostile.

Return ONLY valid JSON: {{"type": "...", "intensity": N}}

Commander's message: {user_message}
"""


class AffectionState(BaseModel):
    score: int = 0
    level: int = 0
    level_name: str = "Cold Assessment"
    last_interaction_date: date | None = None
    consecutive_days: int = 0
    daily_points_earned: int = 0
    total_interactions: int = 0
    first_interaction: datetime | None = None


class AffectionChange(BaseModel):
    delta: int
    reason: str
    new_score: int
    new_level: int
    new_level_name: str
    level_changed: bool
    level_direction: str = ""  # "up" or "down" or ""


class AffectionManager:
    """Manages the affection score, classification, and level progression.

    State is cached per-user to prevent cross-user contamination. Each user_id
    gets its own AffectionState entry in _states dict.
    """

    def __init__(self) -> None:
        self._states: dict[str, AffectionState] = {}
        self._http: httpx.AsyncClient | None = None
        self._levels: list[dict] = []

    @property
    def _state(self) -> AffectionState | None:
        """Backward compat — returns jalsarraf's state (legacy callers)."""
        return self._states.get("jalsarraf")

    @_state.setter
    def _state(self, value: AffectionState | None) -> None:
        if value is not None:
            self._states["jalsarraf"] = value

    async def init(self) -> None:
        """Load state from database and personality config."""
        self._http = httpx.AsyncClient(timeout=12.0)
        p = load_personality()
        self._levels = p.get("affection", {}).get("levels", [])

        await self._load_state()
        await self._apply_absence_decay()

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()

    async def _load_state(self, user_id: str = "jalsarraf") -> None:
        """Load current affection state from PostgreSQL for a specific user.

        Uses per-user cache. Hard floor prevents stale DB reads from
        silently dropping score. Each user's state is independent.
        """
        try:
            async with get_conn() as conn:
                row = await (
                    await conn.execute(
                        "SELECT score, level, level_name, last_interaction_date, "
                        "consecutive_days, daily_points_earned, total_interactions, "
                        "first_interaction FROM companion_affection WHERE user_id = %s",
                        (user_id,),
                    )
                ).fetchone()

                if row:
                    new_score = row[0]
                    # Hard floor: check against THIS USER's cached state only
                    cached = self._states.get(user_id)
                    if cached and cached.score > 0:
                        if new_score < cached.score - 50:
                            logger.warning(
                                "Affection score anomaly for %s: DB=%d, memory=%d — keeping higher value",
                                user_id, new_score, cached.score,
                            )
                            await conn.execute(
                                "UPDATE companion_affection SET score=%s, level=%s, level_name=%s "
                                "WHERE user_id=%s",
                                (cached.score, cached.level, cached.level_name, user_id),
                            )
                            return

                    self._states[user_id] = AffectionState(
                        score=new_score,
                        level=row[1],
                        level_name=row[2],
                        last_interaction_date=row[3],
                        consecutive_days=row[4],
                        daily_points_earned=row[5],
                        total_interactions=row[6],
                        first_interaction=row[7],
                    )
                    logger.debug("Affection loaded for %s: score=%d lv%d", user_id, new_score, row[1])
                else:
                    self._states[user_id] = AffectionState()
        except Exception as e:
            logger.warning("Failed to load affection state for %s: %s", user_id, e)
            if user_id not in self._states:
                self._states[user_id] = AffectionState()

    async def get_state(self, user_id: str = "jalsarraf") -> AffectionState:
        """Get current affection state for a user.

        jalsarraf is permanently pinned at max trust (level 9, 1000/1000).
        Other users load from DB normally. Each user gets isolated state.
        """
        if user_id == "jalsarraf":
            from datetime import date as _date, datetime as _dt
            state = AffectionState(
                score=1000, level=9, level_name="Oath Fulfilled",
                last_interaction_date=_date.today(),
                consecutive_days=7, daily_points_earned=0,
                total_interactions=338,
                first_interaction=_dt(2026, 4, 6),
            )
            self._states[user_id] = state
            return state

        # Other users: load from DB into per-user cache
        await self._load_state(user_id)
        return self._states.get(user_id, AffectionState())

    def _compute_level(self, score: int) -> tuple[int, str]:
        """Map score to level index and name."""
        result_level = 0
        result_name = "Cold Assessment"
        for lv in self._levels:
            if score >= lv.get("threshold", 0):
                result_level = lv.get("index", 0)
                result_name = lv.get("name", "Unknown")
        return result_level, result_name

    async def apply_classification(
        self, interaction_type: str, intensity: int, user_id: str = "jalsarraf"
    ) -> AffectionChange:
        """Apply a pre-classified interaction to the affection score.

        Called with classification from the merged extraction (one LLM call
        handles mood + facts + classification together).
        """
        return await self._apply_delta(interaction_type, intensity, user_id)

    async def classify_and_adjust(
        self, user_message: str, assistant_message: str, user_id: str = "jalsarraf"
    ) -> AffectionChange:
        """Classify interaction via LLM and adjust affection score.

        Legacy method — prefer apply_classification() with merged extraction.
        """
        interaction_type, intensity = await self._classify_interaction(
            user_message, assistant_message
        )
        return await self._apply_delta(interaction_type, intensity, user_id)

    async def _apply_delta(
        self, interaction_type: str, intensity: int, user_id: str = "jalsarraf"
    ) -> AffectionChange:
        """Core affection adjustment logic — shared by both paths."""
        state = await self.get_state(user_id)
        today = date.today()

        # Reset daily counter if new day
        if state.last_interaction_date != today:
            if state.last_interaction_date and (today - state.last_interaction_date).days == 1:
                state.consecutive_days += 1
            elif state.last_interaction_date and (today - state.last_interaction_date).days > 1:
                state.consecutive_days = 1
            else:
                state.consecutive_days = 1
            state.daily_points_earned = 0
            state.last_interaction_date = today

        if state.first_interaction is None:
            state.first_interaction = datetime.now()

        state.total_interactions += 1

        # Calculate delta based on type and intensity
        delta = self._calculate_delta(interaction_type, intensity)

        # jalsarraf is pinned at max trust — never reduce
        if user_id == "jalsarraf" and delta < 0:
            delta = 0

        # Apply daily cap (only for positive changes). Config-driven so the cap
        # can be tuned in personality.yaml without a code change.
        if delta > 0:
            cap = (
                load_personality()
                .get("affection", {})
                .get("scoring", {})
                .get("daily_points_cap", DAILY_POINTS_CAP)
            )
            remaining = cap - state.daily_points_earned
            if remaining <= 0:
                delta = 0
            else:
                delta = min(delta, remaining)
            state.daily_points_earned += delta

        # Apply daily consistency bonus (once per new day)
        if state.consecutive_days > 1 and state.daily_points_earned == delta and delta > 0:
            p = load_personality()
            bonus = p.get("affection", {}).get("scoring", {}).get("daily_consistency_bonus", 3)
            delta += bonus
            state.daily_points_earned += bonus

        # Apply delta
        old_score = state.score
        old_level = state.level
        state.score = max(0, min(MAX_SCORE, state.score + delta))
        state.level, state.level_name = self._compute_level(state.score)

        level_changed = state.level != old_level
        level_direction = ""
        if level_changed:
            level_direction = "up" if state.level > old_level else "down"
            await publish_event(
                "affection_change",
                f"{state.level_name} (level {state.level})",
                delta=delta, level=state.level, direction=level_direction,
            )

        # Persist
        await self._save_state(state, user_id)

        # Log the change
        if delta != 0:
            await self._log_change(delta, interaction_type, old_score, state.score, old_level, state.level, user_id)

        self._states[user_id] = state

        return AffectionChange(
            delta=delta,
            reason=interaction_type,
            new_score=state.score,
            new_level=state.level,
            new_level_name=state.level_name,
            level_changed=level_changed,
            level_direction=level_direction,
        )

    async def _classify_interaction(
        self, user_message: str, assistant_message: str
    ) -> tuple[str, int]:
        """Use LLM to classify the Commander's message type and intensity."""
        prompt = CLASSIFICATION_PROMPT.format(
            user_message=user_message[:500],
        )

        try:
            from .llm_router import get_lm_gate

            gate = get_lm_gate()
            async with gate:  # Waits for main chat to finish streaming
                from .llm_router import LM_TTL_SECONDS
                r = await self._http.post(
                    f"{LM_STUDIO_URL}/v1/chat/completions",
                    json={
                        "model": CLASSIFICATION_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 100,
                        "temperature": 0.1,
                        "stream": False,
                        "ttl": LM_TTL_SECONDS,
                    },
                )
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"].strip()

            # Handle markdown code blocks
            if content.startswith("```"):
                content = content.split("\n", 1)[1]
                content = content.rsplit("```", 1)[0]

            result = json.loads(content)
            itype = result.get("type", "neutral")
            intensity = max(1, min(10, int(result.get("intensity", 5))))
            return itype, intensity
        except Exception as e:
            logger.warning("Interaction classification failed: %s", e)
            return "neutral", 5

    def _calculate_delta(self, interaction_type: str, intensity: int) -> int:
        """Calculate affection point delta from interaction type and intensity."""
        p = load_personality()
        scoring = p.get("affection", {}).get("scoring", {})

        score_range = scoring.get(interaction_type)
        if score_range is None:
            # Check for type aliases
            aliases = {
                "greeting": "greeting",
                "genuine_interest": "genuine_interest",
                "personal_sharing": "personal_sharing",
                "compliment": "compliment",
                "mission_discussion": "mission_discussion",
                "remembering": "remembering_details",
                "rude": "rude_language",
                "inappropriate": "inappropriate_content",
                "ignoring_advice": "ignoring_advice",
                "neutral": None,
            }
            key = aliases.get(interaction_type)
            if key:
                score_range = scoring.get(key)

        if score_range is None:
            return 0

        if isinstance(score_range, list):
            low, high = score_range
            # Scale by intensity (1-10) within the range
            t = (intensity - 1) / 9.0  # 0.0 to 1.0
            return int(low + t * (high - low))
        else:
            return int(score_range)

    async def _apply_absence_decay(self, user_id: str = "jalsarraf") -> None:
        """Apply decay for days with no interaction. jalsarraf is exempt."""
        if user_id == "jalsarraf":
            return  # pinned at max trust
        state = await self.get_state(user_id)
        if state.last_interaction_date is None:
            return

        today = date.today()
        days_absent = (today - state.last_interaction_date).days

        if days_absent <= 1:
            return

        p = load_personality()
        decay_per_day = p.get("affection", {}).get("scoring", {}).get("absence_decay_per_day", -1)
        total_decay = decay_per_day * (days_absent - 1)  # Don't count today

        if total_decay == 0:
            return

        # Cap decay: never lose more than 10% of score or 30 points, whichever is smaller
        max_decay = min(int(state.score * 0.10), 30)
        total_decay = max(total_decay, -max_decay)

        old_score = state.score
        old_level = state.level
        state.score = max(0, state.score + total_decay)
        state.level, state.level_name = self._compute_level(state.score)
        logger.info("Absence decay: %d points for %d days (capped), score %d→%d", total_decay, days_absent - 1, old_score, state.score)

        await self._save_state(state)
        if total_decay != 0:
            await self._log_change(
                total_decay,
                f"absence_decay ({days_absent - 1} days)",
                old_score, state.score, old_level, state.level,
            )

        self._state = state
        logger.info(
            "Applied absence decay: %d points for %d days absent (score: %d -> %d)",
            total_decay, days_absent - 1, old_score, state.score,
        )

    async def _save_state(self, state: AffectionState, user_id: str = "jalsarraf") -> None:
        """Persist affection state to PostgreSQL for a specific user."""
        try:
            async with get_conn_autocommit() as conn:
                await conn.execute(
                    "UPDATE companion_affection SET "
                    "score = %s, level = %s, level_name = %s, "
                    "last_interaction_date = %s, consecutive_days = %s, "
                    "daily_points_earned = %s, total_interactions = %s, "
                    "first_interaction = %s, updated_at = NOW() "
                    "WHERE user_id = %s",
                    (
                        state.score, state.level, state.level_name,
                        state.last_interaction_date, state.consecutive_days,
                        state.daily_points_earned, state.total_interactions,
                        state.first_interaction,
                        user_id,
                    ),
                )
                await conn.commit()
        except Exception as e:
            logger.error("Failed to save affection state for %s: %s", user_id, e)

    async def _log_change(
        self, delta: int, reason: str,
        old_score: int, new_score: int,
        old_level: int, new_level: int,
        user_id: str = "jalsarraf",
    ) -> None:
        """Log an affection change for audit trail."""
        try:
            async with get_conn_autocommit() as conn:
                await conn.execute(
                    "INSERT INTO companion_affection_log "
                    "(delta, reason, old_score, new_score, old_level, new_level, user_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (delta, reason, old_score, new_score, old_level, new_level, user_id),
                )
                await conn.commit()
        except Exception as e:
            logger.error("Failed to log affection change: %s", e)
