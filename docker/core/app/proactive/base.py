"""Shared typing surface for the ProactiveEngine mixins.

The engine's behavior is split across cooperating mixins (mission, events,
milestones) that all operate on the same instance state defined in
``ProactiveEngine.__init__``. This module declares that shared state and the
cross-mixin method signatures as *annotations only* so a static checker can
resolve ``self.*`` references inside each mixin.

Nothing here executes or creates instance attributes at runtime: the class body
contains only ``name: type`` annotations (stored in ``__annotations__``), never
assignments. The real values come exclusively from ``ProactiveEngine.__init__``
and the concrete methods resolved via the MRO. This is a type-only scaffold —
it introduces zero behavior change.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .mission import MissionTimer


class _EngineBase:
    """Annotation-only base describing the shared ProactiveEngine instance state.

    Mixins inherit from this purely so ``self.<shared attr/method>`` type-checks.
    """

    # ── Callbacks / getters (assigned in ProactiveEngine.__init__) ─────────
    _on_message_callback: Any
    _on_recap_callback: Any
    _session_getter: Any

    # ── Per-user state maps ────────────────────────────────────────────────
    _affection_levels: dict[str, int]
    _mission_timers: dict[str, MissionTimer]

    # ── Shared scalar / legacy-compat state ───────────────────────────────
    _muted_until: datetime | None
    _last_random_event: datetime | None
    _last_message_time: datetime | None
    _proactive_count_today: int
    _last_proactive_answered: bool
    _random_events_today: int
    _last_mood: str
    _affection_level: int
    _mission_timer: MissionTimer | None
    _romance_delivered_today: bool
    _dream_delivered_today: bool
    _user_messaged_today: bool

    # ── Cross-mixin methods / properties (concrete impls live on
    #    ProactiveEngine / MissionMixin and are resolved via the MRO) ────────
    _pick_message: Callable[[dict[int, list[str]]], str]
    _deliver: Callable[[str], Any]
    _can_send: Callable[[], bool]

    @property
    def mission_active(self) -> bool:  # pragma: no cover - real impl on MissionMixin
        raise NotImplementedError
