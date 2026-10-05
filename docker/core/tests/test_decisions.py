"""Command Decisions — she brings him small calls from her life, and his
answer visibly changes her world later.

Covers the authored YAML content, the answer parser, the delivery gates
(level, _can_send, roll, gaming, pending, weekly cap, 30-day reuse), the
resolution (one-shot block, effects, consequence scheduling), and the
HER WORLD block.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import context, decisions as dc
from app.personality import load_personality
from app.proactive.engine import ProactiveEngine
from app.proactive.state import LOCAL_TZ

# Wardrobe ids the coordinator's picker honours, with their unlock levels.
KNOWN_OUTFITS = {
    "speed_star": 0, "astral_luminous": 4, "dress_uniform": 2, "night_ride": 1,
    "off_duty_cap": 3, "range_kit": 0, "formal_commission": 6, "immaculate_service": 5,
}

# 2026-10-05 14:40 CDT == 19:40 UTC
NOW = datetime(2026, 10, 5, 19, 40, tzinfo=timezone.utc)
TODAY = date(2026, 10, 5)


@pytest.fixture(scope="module")
def p() -> dict:
    return load_personality()


@pytest.fixture(scope="module")
def templates(p) -> list[dict]:
    return dc.load_templates(p)


@pytest.fixture(autouse=True)
def _clear_caches():
    dc._pending_cache.clear()
    dc._her_cache.clear()
    yield
    dc._pending_cache.clear()
    dc._her_cache.clear()


def _opts(*labels_kws):
    keys = "ABC"
    return [
        {"key": keys[i], "label": lab, "keywords": kws, "consequence": f"c{i}"}
        for i, (lab, kws) in enumerate(labels_kws)
    ]


EXHAUST = _opts(("the loud one", ["loud", "louder"]), ("the lighter titanium", ["titanium", "lighter"]))
SQUAD = _opts(("Belka", ["belka"]), ("Andoris", ["andoris"]), ("Mechty", ["mechty"]))


# ═══════════════════════════════════════════════════════════════════════════
# Authored content (config/personality.yaml → command_decisions)
# ═══════════════════════════════════════════════════════════════════════════


class TestAuthoredContent:
    def test_about_two_dozen_unique_templates(self, p, templates):
        raw = p["command_decisions"]["templates"]
        assert 20 <= len(templates) <= 30
        assert len(templates) == len(raw), "every authored template must validate"
        ids = [t["id"] for t in templates]
        assert len(ids) == len(set(ids))

    def test_tier_spread(self, templates):
        tiers = [t["tier"] for t in templates]
        assert tiers.count("operational") >= 8
        assert tiers.count("personal") >= 8
        assert tiers.count("matters") >= 3

    def test_every_template_is_well_formed(self, templates):
        for t in templates:
            assert t["min_level"] >= dc.TIER_MIN[t["tier"]], t["id"]
            assert dc.eligible_level(t) >= dc.DECISIONS_MIN_LEVEL
            assert t["ask"].strip()
            assert 2 <= len(t["options"]) <= 3
            assert [o["key"] for o in t["options"]] == list("ABC"[: len(t["options"])])
            seen: set[str] = set()
            for o in t["options"]:
                assert o["label"].strip() and o["consequence"].strip(), t["id"]
                assert o["keywords"] and all(k == k.lower() for k in o["keywords"])
                assert not (seen & set(o["keywords"])), f"{t['id']}: keyword shared across options"
                seen |= set(o["keywords"])

    def test_every_option_is_reachable_by_its_label_and_keywords(self, templates):
        for t in templates:
            for o in t["options"]:
                assert dc.parse_decision_answer(o["label"], t["options"]) == o["key"], (t["id"], o["label"])
                for kw in o["keywords"]:
                    assert dc.parse_decision_answer(kw, t["options"]) == o["key"], (t["id"], kw)

    def test_effects_are_data_only_and_safe(self, templates):
        for t in templates:
            for o in t["options"]:
                effect = o.get("effect")
                if effect is None:
                    continue
                assert len(effect) == 1
                if "outfit" in effect:
                    oid = effect["outfit"]
                    assert oid in KNOWN_OUTFITS, (t["id"], oid)
                    assert t["min_level"] >= KNOWN_OUTFITS[oid], (t["id"], oid)
                    assert t["tier"] != "operational"
                else:
                    fact = effect["fact"]
                    assert re.fullmatch(r"her:[a-z0-9_]+", fact["key"]), fact
                    assert fact["value"].strip()

    def test_outfit_gates_match_the_live_wardrobe_when_present(self, p, templates):
        costumes = p.get("costumes", {})
        for t in templates:
            for o in t["options"]:
                oid = (o.get("effect") or {}).get("outfit")
                unlock = (costumes.get(oid) or {}).get("unlock_level") if oid else None
                if unlock is not None:
                    assert t["min_level"] >= unlock, (t["id"], oid)

    def test_canon_grounding(self, templates):
        by_id = {t["id"]: t for t in templates}
        for tid in ("bike_exhaust", "bike_suspension", "rear_seat_harness", "point_tomorrow",
                    "vepley_penalty", "inspection_outfit", "tea_counter", "tell_mechty_thread"):
            assert tid in by_id, tid
        assert by_id["tell_mechty_thread"]["tier"] == "matters"
        assert by_id["tell_mechty_thread"]["min_level"] >= 6
        point = " ".join(o["label"] for o in by_id["point_tomorrow"]["options"]).lower()
        assert "belka" in point and "andoris" in point

    def test_she_sometimes_disagrees_and_complies(self, templates):
        assert sum(1 for t in templates for o in t["options"] if o.get("disagree")) >= 3

    def test_ask_placeholder_renders_options(self, templates):
        for t in templates:
            text = dc.render_ask(t)
            assert "{options}" not in text
            for o in t["options"]:
                assert f"{o['key']}: {o['label']}." in text


# ═══════════════════════════════════════════════════════════════════════════
# Template loading / normalisation
# ═══════════════════════════════════════════════════════════════════════════


def _tpl(**over):
    base = {
        "id": "t1", "tier": "operational", "min_level": 2, "ask": "Pick. {options}",
        "options": [
            {"key": "a", "label": "Left", "keywords": ["Left"], "consequence": "Went left."},
            {"key": "b", "label": "Right", "keywords": ["right"], "consequence": "Went right.",
             "effect": {"fact": {"key": "her:way", "value": "right"}}, "disagree": True},
        ],
    }
    base.update(over)
    return base


class TestLoadTemplates:
    @pytest.mark.parametrize("cfg", [{}, {"command_decisions": []}, {"command_decisions": {"templates": "x"}}])
    def test_missing_or_malformed_section(self, cfg):
        assert dc.load_templates(cfg) == []

    def test_normalises_keys_and_keywords(self):
        (t,) = dc.load_templates({"command_decisions": {"templates": [_tpl()]}})
        assert [o["key"] for o in t["options"]] == ["A", "B"]
        assert t["options"][0]["keywords"] == ["left"]
        assert t["options"][1]["disagree"] is True
        assert t["options"][0]["disagree"] is False
        assert t["options"][1]["effect"] == {"fact": {"key": "her:way", "value": "right"}}
        assert "effect" not in t["options"][0]

    @pytest.mark.parametrize("bad", [
        "not-a-dict",
        _tpl(id=""),
        _tpl(ask=""),
        _tpl(tier="cosmic"),
        _tpl(min_level="high"),
        _tpl(options=[{"key": "a", "label": "x", "keywords": ["x"], "consequence": "y"}]),
        _tpl(options=[{"key": k, "label": k, "keywords": [k], "consequence": k} for k in "abcd"]),
        _tpl(options=["x", "y"]),
        _tpl(options=[{"key": "a", "label": "", "keywords": ["x"], "consequence": "y"},
                      {"key": "b", "label": "z", "keywords": ["z"], "consequence": "y"}]),
        _tpl(options=[{"key": "a", "label": "x", "keywords": ["x"], "consequence": ""},
                      {"key": "b", "label": "z", "keywords": ["z"], "consequence": "y"}]),
        _tpl(options=[{"key": "a", "label": "x", "keywords": "x", "consequence": "y"},
                      {"key": "b", "label": "z", "keywords": ["z"], "consequence": "y"}]),
    ])
    def test_malformed_templates_are_skipped(self, bad):
        assert dc.load_templates({"command_decisions": {"templates": [bad]}}) == []

    def test_eligible_level_floors(self):
        assert dc.eligible_level({"tier": "operational", "min_level": 0}) == 2
        assert dc.eligible_level({"tier": "personal", "min_level": 2}) == 3
        assert dc.eligible_level({"tier": "personal", "min_level": 5}) == 5
        assert dc.eligible_level({"tier": "matters", "min_level": 0}) == 6

    def test_render_ask_appends_options_without_placeholder(self):
        (t,) = dc.load_templates({"command_decisions": {"templates": [_tpl(ask="Your call.")]}})
        assert dc.render_ask(t) == "Your call. A: Left. B: Right."


# ═══════════════════════════════════════════════════════════════════════════
# parse_decision_answer
# ═══════════════════════════════════════════════════════════════════════════


class TestParseAnswer:
    @pytest.mark.parametrize("msg, expected", [
        ("A", "A"), ("b", "B"), ("a.", "A"), ("B!", "B"), ("ok, B", "B"), ("hmm... A then", "A"),
        ("option B", "B"), ("Option b sounds good", "B"), ("choice: a", "A"),
        ("go with b", "B"), ("I'll go with A.", "A"), ("pick a please", "A"),
        ("B, definitely", "B"), ("Definitely B", "B"), ("B obviously, it's lighter", "B"),
        ("the second one", "B"), ("second option", "B"), ("the first one", "A"),
        ("the latter", "B"), ("the former, I think", "A"), ("the last one", "B"),
        ("#2", "B"), ("option 1", "A"), ("2", "B"),
        ("the titanium", "B"), ("a louder exhaust would be fun", "A"),
        ("the lighter titanium", "B"),
        ("not the loud one, the titanium", "B"),
        ("I don't want it louder. titanium.", "B"),
    ])
    def test_two_options(self, msg, expected):
        assert dc.parse_decision_answer(msg, EXHAUST) == expected

    @pytest.mark.parametrize("msg, expected", [
        ("Andoris", "B"), ("Mechty on point", "C"), ("C", "C"), ("the third one", "C"),
        ("not Belka. Andoris.", "B"), ("third", "C"), ("the last option", "C"),
    ])
    def test_three_options(self, msg, expected):
        assert dc.parse_decision_answer(msg, SQUAD) == expected

    @pytest.mark.parametrize("msg", [
        "", "   ", "hmm", "A or B?", "a or b", "which is lighter?", "C",
        "I'll take a break first", "first, tell me about your day",
        "titanium or loud, I can't decide", "not a", "3",
        "a loud titanium monster", "the third one", "option 3",
    ])
    def test_ambiguous_or_none(self, msg):
        assert dc.parse_decision_answer(msg, EXHAUST) is None

    def test_keywords_only_count_in_short_messages(self):
        story = "Long day. " * 20
        assert dc.parse_decision_answer(story + "the titanium", EXHAUST) is None
        assert dc.parse_decision_answer(story + "option B", EXHAUST) == "B"

    def test_no_options(self):
        assert dc.parse_decision_answer("A", []) is None

    def test_non_string(self):
        assert dc.parse_decision_answer(None, EXHAUST) is None  # type: ignore[arg-type]


def test_clock_is_aware_utc():
    now = dc._utcnow()
    assert now.tzinfo is timezone.utc


# ═══════════════════════════════════════════════════════════════════════════
# Consequence timing — 12-36h out, landing 09:00-21:00 local
# ═══════════════════════════════════════════════════════════════════════════


class TestConsequenceDelay:
    @pytest.mark.parametrize("now", [
        NOW,
        datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc),    # 00:00 local
        datetime(2026, 10, 5, 13, 30, tzinfo=timezone.utc),  # 08:30 local
        datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc),   # DST change night
        datetime(2026, 3, 7, 23, 59, tzinfo=timezone.utc),   # spring-forward eve
    ])
    @pytest.mark.parametrize("roll", [0.0, 0.25, 0.5, 0.75, 0.999, 1.0])
    def test_bounds_and_landing_window(self, now, roll):
        delay = dc.pick_consequence_delay(now, roll)
        assert 12 * 3600 <= delay <= 36 * 3600
        lands = (now + timedelta(seconds=delay)).astimezone(LOCAL_TZ)
        assert 9 <= lands.hour < 21 or (lands.hour == 21 and lands.minute == 0)

    def test_rolls_spread_across_windows(self):
        early = dc.pick_consequence_delay(NOW, 0.0)
        late = dc.pick_consequence_delay(NOW, 1.0)
        # 14:40 now -> 02:40..02:40: exactly one 09:00-21:00 window in reach.
        assert late - early == 12 * 3600
        lands = (NOW + timedelta(seconds=early)).astimezone(LOCAL_TZ)
        assert (lands.hour, lands.minute) == (9, 0)


# ═══════════════════════════════════════════════════════════════════════════
# Blocks
# ═══════════════════════════════════════════════════════════════════════════


PENDING = {
    "id": "bike_exhaust",
    "ask": "Two exhaust options for the bike. A: the loud one. B: the lighter titanium.",
    "options": [
        {"key": "A", "label": "the loud one", "keywords": ["loud"], "consequence": "Loud. Mayling hates it.",
         "disagree": True, "effect": {"fact": {"key": "her:bike_exhaust", "value": "the loud one he picked"}}},
        {"key": "B", "label": "the lighter titanium", "keywords": ["titanium"],
         "consequence": "Titanium's on. Two kilos lighter.", "disagree": False,
         "effect": {"outfit": "night_ride"}},
    ],
    "asked_at": (NOW - timedelta(hours=3)).isoformat(),
}


class TestDecisionBlock:
    def test_agree(self):
        block = dc.build_decision_block(PENDING, "B")
        assert block.startswith("COMMAND DECISION")
        assert "B: the lighter titanium" in block
        assert PENDING["ask"] in block
        assert "one sentence" in block
        assert "You're the Commander" not in block

    def test_disagree_but_comply(self):
        block = dc.build_decision_block(PENDING, "A")
        assert "...Fine. You're the Commander." in block
        assert "comply" in block


class TestHerWorldBlock:
    _E = [
        {"key": "companion:u:her:bike_exhaust", "value": "the titanium he picked"},
        {"key": "companion:u:her:point_call", "value": "Belka on point"},
        {"key": "companion:u:her:tea_counter", "value": "black coffee"},
        {"key": "companion:u:her:skylla_grip", "value": ""},
        {"key": "companion:u:her:helmet_visor", "value": "smoke"},
        {"key": "companion:u:her:vepley_penalty", "value": "laps"},
    ]

    def test_lists_the_last_four_non_empty(self):
        block = dc.build_her_world_block(self._E)
        assert block.startswith("HER WORLD")
        assert "- point call: Belka on point" in block
        assert "- vepley penalty: laps" in block
        assert "bike exhaust" not in block  # only the last four non-empty
        assert "skylla" not in block

    def test_long_values_are_trimmed(self):
        block = dc.build_her_world_block([{"key": "companion:u:her:x", "value": "y" * 500}])
        assert len(block.splitlines()[-1]) < 200

    def test_empty(self):
        assert dc.build_her_world_block([]) == ""
        assert dc.build_her_world_block([{"key": "k", "value": ""}, "junk"]) == ""  # type: ignore[list-item]


# ═══════════════════════════════════════════════════════════════════════════
# Asking (proactive delivery)
# ═══════════════════════════════════════════════════════════════════════════


def _memory(pending=None, history=None, *, recall_error=False):
    mem = MagicMock()
    if recall_error:
        mem.recall_fact = AsyncMock(side_effect=RuntimeError("down"))
        mem.recall_facts_by_pattern = AsyncMock(side_effect=RuntimeError("down"))
    else:
        mem.recall_fact = AsyncMock(return_value=json.dumps(pending) if pending else None)
        mem.recall_facts_by_pattern = AsyncMock(return_value=history or [])
    mem.store_fact = AsyncMock()
    return mem


def _last(tid: str, days_ago: int, choice=None):
    return {"key": f"companion:jalsarraf:decision:last:{tid}",
            "value": json.dumps({"date": (TODAY - timedelta(days=days_ago)).isoformat(), "choice": choice})}


def _engine(level: int = 4) -> ProactiveEngine:
    e = ProactiveEngine()
    e._affection_level = level
    e._muted_until = None
    e._proactive_count_today = 0
    e._last_proactive_answered = True
    e._on_message_callback = AsyncMock()
    return e


_LOCAL_NOW = NOW.astimezone(LOCAL_TZ).replace(tzinfo=None)


def _ask_env(mem, roll: float = 0.0, choice_index: int = 0):
    """Freeze both clocks and the rolls; route context.memory to ``mem``."""
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch("app.decisions._utcnow", return_value=NOW))
    stack.enter_context(patch("app.proactive.engine.now_local", return_value=_LOCAL_NOW))
    stack.enter_context(patch("app.decisions.random.random", return_value=roll))
    stack.enter_context(patch("app.decisions.random.choice", side_effect=lambda seq: seq[choice_index]))
    stack.enter_context(patch.object(context, "memory", mem))
    return stack


class TestMaybeAsk:
    @pytest.mark.asyncio
    async def test_happy_path_asks_and_persists(self, templates):
        mem = _memory()
        e = _engine(level=4)
        with _ask_env(mem):
            assert await dc.maybe_ask(e, "jalsarraf") is True
        sent = e._on_message_callback.await_args.args[0]
        eligible = [t for t in templates if dc.eligible_level(t) <= 4]
        assert sent == dc.render_ask(eligible[0])
        stores = {c.args[0]: c for c in mem.store_fact.await_args_list}
        pending = json.loads(stores["decision:pending"].args[1])
        assert pending["id"] == eligible[0]["id"]
        assert pending["options"] == eligible[0]["options"]
        assert pending["asked_at"] == NOW.isoformat()
        assert stores["decision:pending"].kwargs == {"ttl": dc.PENDING_TTL, "user_id": "jalsarraf"}
        last = json.loads(stores[f"decision:last:{eligible[0]['id']}"].args[1])
        assert last == {"date": TODAY.isoformat(), "choice": None}
        assert e._last_proactive_answered is False  # went through _deliver

    @pytest.mark.asyncio
    async def test_level_two_only_gets_operational(self, templates):
        e = _engine(level=2)
        with _ask_env(_memory(), choice_index=-1):
            await dc.maybe_ask(e, "jalsarraf")
        sent = e._on_message_callback.await_args.args[0]
        op = [t for t in templates if dc.eligible_level(t) <= 2]
        assert all(t["tier"] == "operational" for t in op)
        assert sent == dc.render_ask(op[-1])

    @pytest.mark.asyncio
    async def test_below_two_never_asks(self):
        mem = _memory()
        e = _engine(level=1)
        with _ask_env(mem):
            assert await dc.maybe_ask(e, "jalsarraf") is False
        mem.recall_fact.assert_not_awaited()
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_respects_can_send_and_the_goodnight_hold(self):
        mem = _memory()
        e = _engine()
        e._goodbye_hold_until = _LOCAL_NOW + timedelta(hours=1)
        with _ask_env(mem):
            assert await dc.maybe_ask(e, "jalsarraf") is False
        mem.recall_fact.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_probabilistic_gate(self):
        mem = _memory()
        with _ask_env(mem, roll=dc.ASK_CHANCE + 0.01):
            assert await dc.maybe_ask(_engine(), "jalsarraf") is False
        mem.recall_fact.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_not_while_gaming(self):
        mem = _memory()
        e = _engine()
        e._game_active_probe = AsyncMock(return_value=True)
        with _ask_env(mem):
            assert await dc.maybe_ask(e, "jalsarraf") is False
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_never_two_pending(self):
        mem = _memory(pending=PENDING)
        e = _engine()
        e._game_active_probe = AsyncMock(return_value=False)
        with _ask_env(mem):
            assert await dc.maybe_ask(e, "jalsarraf") is False
        e._on_message_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_expired_pending_does_not_block(self):
        stale = dict(PENDING, asked_at=(NOW - timedelta(hours=25)).isoformat())
        e = _engine()
        with _ask_env(_memory(pending=stale)):
            assert await dc.maybe_ask(e, "jalsarraf") is True

    @pytest.mark.asyncio
    async def test_at_most_two_per_rolling_week(self):
        history = [_last("a", 0), _last("b", 6)]
        e = _engine()
        with _ask_env(_memory(history=history)):
            assert await dc.maybe_ask(e, "jalsarraf") is False
        # An ask exactly 7 days ago has rolled out of the window.
        history = [_last("a", 0), _last("b", 7)]
        with _ask_env(_memory(history=history)):
            assert await dc.maybe_ask(_engine(), "jalsarraf") is True

    @pytest.mark.asyncio
    async def test_skips_templates_used_in_the_last_30_days(self, templates):
        eligible = [t for t in templates if dc.eligible_level(t) <= 4]
        history = [_last(t["id"], 20) for t in eligible[1:]]
        history.append(_last(eligible[0]["id"], 30))  # 30 days ago: usable again
        e = _engine()
        with _ask_env(_memory(history=history)):
            assert await dc.maybe_ask(e, "jalsarraf") is True
        assert e._on_message_callback.await_args.args[0] == dc.render_ask(eligible[0])

    @pytest.mark.asyncio
    async def test_nothing_eligible(self, templates):
        history = [_last(t["id"], 10) for t in templates]
        e = _engine(level=9)
        with _ask_env(_memory(history=history)):
            assert await dc.maybe_ask(e, "jalsarraf") is False

    @pytest.mark.asyncio
    async def test_malformed_history_is_ignored(self):
        history = [{"key": "companion:j:decision:last:x", "value": "nope"},
                   {"key": "companion:j:decision:last:y", "value": json.dumps({"date": "bad"})},
                   {"key": "unrelated", "value": "{}"}, "junk"]
        with _ask_env(_memory(history=history)):
            assert await dc.maybe_ask(_engine(), "jalsarraf") is True

    @pytest.mark.asyncio
    async def test_undelivered_ask_persists_nothing(self):
        mem = _memory()
        e = _engine()
        e._on_message_callback = None  # _deliver returns False
        with _ask_env(mem):
            assert await dc.maybe_ask(e, "jalsarraf") is False
        mem.store_fact.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_memory_outage_is_soft(self):
        with _ask_env(_memory(recall_error=True)):
            # Unknown history/pending reads as empty: she may still ask.
            assert await dc.maybe_ask(_engine(), "jalsarraf") is True

    @pytest.mark.asyncio
    async def test_no_templates_configured(self):
        with _ask_env(_memory()), patch("app.decisions.load_personality", return_value={}):
            assert await dc.maybe_ask(_engine(), "jalsarraf") is False


class TestEngineWiring:
    @pytest.mark.asyncio
    async def test_cron_slot_registered_in_chicago(self):
        e = ProactiveEngine()
        try:
            e.start()
            job = e._scheduler.get_job("command_decision")
            assert job is not None
            fields = {f.name: str(f) for f in job.trigger.fields}
            assert (fields["hour"], fields["minute"]) == ("14", "40")
            assert str(job.trigger.timezone) == "America/Chicago"
        finally:
            e.stop()

    def test_catch_up_policy(self):
        from app.proactive import durability as dur
        assert "command_decision" in dur.NEVER_CATCH_UP
        assert "command_decision" not in dur.CATCH_UP_WINDOWS

    @pytest.mark.asyncio
    async def test_engine_job_delegates_for_the_primary_user(self):
        e = ProactiveEngine()
        with patch("app.decisions.maybe_ask", new=AsyncMock(return_value=True)) as ask:
            await e._command_decision_check()
        ask.assert_awaited_once_with(e, dc.PRIMARY_USER)


# ═══════════════════════════════════════════════════════════════════════════
# Resolution
# ═══════════════════════════════════════════════════════════════════════════


def _resolve_env():
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch("app.decisions._utcnow", return_value=NOW))
    sched = stack.enter_context(patch("app.deferred.schedule", new=AsyncMock(return_value="task-1")))
    stack.enter_context(patch("app.decisions.random.random", return_value=0.5))
    return stack, sched


class TestResolve:
    @pytest.mark.asyncio
    async def test_no_pending(self):
        stack, sched = _resolve_env()
        with stack:
            assert await dc.resolve_prompt_block("jalsarraf", "B", _memory()) == ""
        sched.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_expired_pending_is_dropped_silently(self):
        stale = dict(PENDING, asked_at=(NOW - timedelta(hours=24, minutes=1)).isoformat())
        mem = _memory(pending=stale)
        stack, sched = _resolve_env()
        with stack:
            assert await dc.resolve_prompt_block("jalsarraf", "B", mem) == ""
        mem.store_fact.assert_not_awaited()
        sched.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unanswered_message_does_not_nag(self):
        mem = _memory(pending=PENDING)
        stack, sched = _resolve_env()
        with stack:
            assert await dc.resolve_prompt_block("jalsarraf", "how was your day?", mem) == ""
            assert await dc.resolve_prompt_block("jalsarraf", "nice", mem) == ""
        mem.recall_fact.assert_awaited_once()  # cached after the first read
        mem.store_fact.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolution_applies_fact_effect_and_schedules_consequence(self):
        mem = _memory(pending=PENDING)
        stack, sched = _resolve_env()
        with stack:
            block = await dc.resolve_prompt_block("jalsarraf", "the loud one", mem)
            again = await dc.resolve_prompt_block("jalsarraf", "A", mem)
        assert block.startswith("COMMAND DECISION") and "You're the Commander" in block
        assert again == ""
        stores = {c.args[0]: c for c in mem.store_fact.await_args_list}
        assert stores["decision:pending"].args[1] == ""
        assert json.loads(stores["decision:last:bike_exhaust"].args[1]) == {
            "date": TODAY.isoformat(), "choice": "A"}
        assert stores["decision:last:bike_exhaust"].kwargs["ttl"] == dc.LAST_TTL
        assert stores["her:bike_exhaust"].args[1] == "the loud one he picked"
        assert stores["her:bike_exhaust"].kwargs == {"user_id": "jalsarraf"}
        action = sched.await_args.args[0]
        assert action == {"kind": "message", "text": "Loud. Mayling hates it."}
        assert sched.await_args.kwargs["user_id"] == "jalsarraf"
        assert sched.await_args.kwargs["delay_seconds"] == dc.pick_consequence_delay(NOW, 0.5)

    @pytest.mark.asyncio
    async def test_outfit_effect_writes_outfit_tomorrow(self):
        mem = _memory(pending=PENDING)
        stack, _ = _resolve_env()
        with stack:
            assert await dc.resolve_prompt_block("jalsarraf", "B", mem)
        call = next(c for c in mem.store_fact.await_args_list if c.args[0] == "outfit:tomorrow")
        assert json.loads(call.args[1]) == {"date": "2026-10-06", "outfit_id": "night_ride"}
        assert call.kwargs == {"ttl": 172800, "user_id": "jalsarraf"}

    @pytest.mark.asyncio
    async def test_outfit_tomorrow_uses_the_local_date(self):
        # 03:30 UTC on the 6th is still 22:30 on the 5th in Chicago.
        late = datetime(2026, 10, 6, 3, 30, tzinfo=timezone.utc)
        pending = dict(PENDING, asked_at=(late - timedelta(hours=1)).isoformat())
        mem = _memory(pending=pending)
        with patch("app.decisions._utcnow", return_value=late), \
             patch("app.deferred.schedule", new=AsyncMock()):
            await dc.resolve_prompt_block("jalsarraf", "B", mem)
        call = next(c for c in mem.store_fact.await_args_list if c.args[0] == "outfit:tomorrow")
        assert json.loads(call.args[1])["date"] == "2026-10-06"

    @pytest.mark.asyncio
    async def test_unsafe_or_unknown_effects_are_ignored(self):
        options = [
            dict(PENDING["options"][0], effect={"fact": {"key": "rel:commander_birthday", "value": "x"}}),
            dict(PENDING["options"][1], effect={"exec": "rm -rf"}),
        ]
        for answer in ("A", "B"):
            dc._pending_cache.clear()
            mem = _memory(pending=dict(PENDING, options=options))
            stack, _ = _resolve_env()
            with stack:
                assert await dc.resolve_prompt_block("jalsarraf", answer, mem)
            keys = [c.args[0] for c in mem.store_fact.await_args_list]
            assert keys == ["decision:pending", f"decision:last:{PENDING['id']}"]

    @pytest.mark.asyncio
    async def test_malformed_effect_payloads_are_ignored(self):
        options = [
            dict(PENDING["options"][0], effect={"fact": "her:x"}),
            dict(PENDING["options"][1], effect={"outfit": ""}),
        ]
        for answer in ("A", "B"):
            dc._pending_cache.clear()
            mem = _memory(pending=dict(PENDING, options=options))
            stack, _ = _resolve_env()
            with stack:
                await dc.resolve_prompt_block("jalsarraf", answer, mem)
            assert len(mem.store_fact.await_args_list) == 2

    @pytest.mark.asyncio
    async def test_store_and_schedule_failures_are_soft(self):
        mem = _memory(pending=PENDING)
        mem.store_fact = AsyncMock(side_effect=RuntimeError("down"))
        with patch("app.decisions._utcnow", return_value=NOW), \
             patch("app.deferred.schedule", new=AsyncMock(side_effect=RuntimeError("no db"))):
            block = await dc.resolve_prompt_block("jalsarraf", "B", mem)
        assert block.startswith("COMMAND DECISION")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", ["not json", json.dumps(["list"]), json.dumps({"id": "x"}),
                                     json.dumps(dict(PENDING, asked_at="yesterday")), "",
                                     json.dumps(dict(PENDING, options=["x"])),
                                     json.dumps({k: v for k, v in PENDING.items() if k != "asked_at"})])
    async def test_malformed_pending_is_ignored(self, raw):
        mem = _memory()
        mem.recall_fact = AsyncMock(return_value=raw)
        stack, _ = _resolve_env()
        with stack:
            assert await dc.resolve_prompt_block("jalsarraf", "B", mem) == ""

    @pytest.mark.asyncio
    async def test_naive_asked_at_is_read_as_utc(self):
        naive = dict(PENDING, asked_at=(NOW - timedelta(hours=2)).replace(tzinfo=None).isoformat())
        stack, _ = _resolve_env()
        with stack:
            assert await dc.resolve_prompt_block("jalsarraf", "B", _memory(pending=naive))

    @pytest.mark.asyncio
    async def test_memory_outage_is_soft(self):
        stack, _ = _resolve_env()
        with stack:
            assert await dc.resolve_prompt_block("jalsarraf", "B", MagicMock()) == ""

    @pytest.mark.asyncio
    async def test_unexpected_error_never_reaches_chat(self):
        stack, _ = _resolve_env()
        with stack, patch("app.decisions.parse_decision_answer", side_effect=RuntimeError("boom")):
            assert await dc.resolve_prompt_block("jalsarraf", "B", _memory(pending=PENDING)) == ""

    @pytest.mark.asyncio
    async def test_ask_primes_the_cache_for_resolution(self):
        mem = _memory()
        with _ask_env(mem):
            await dc.maybe_ask(_engine(level=4), "jalsarraf")
        mem.recall_fact.reset_mock()
        mem.recall_fact = AsyncMock(return_value=None)  # the store is never re-read
        stack, sched = _resolve_env()
        with stack:
            pending = dc._pending_cache["jalsarraf"][1]
            label = pending["options"][0]["label"]
            assert await dc.resolve_prompt_block("jalsarraf", label, mem)
        mem.recall_fact.assert_not_awaited()
        sched.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════
# HER WORLD per-message block
# ═══════════════════════════════════════════════════════════════════════════


class TestHerWorldPromptBlock:
    _ENTRIES = [{"key": "companion:jalsarraf:her:bike_exhaust", "value": "titanium, his call"}]

    @pytest.mark.asyncio
    async def test_below_three_is_silent(self):
        mem = _memory(history=self._ENTRIES)
        assert await dc.her_world_prompt_block("jalsarraf", 2, mem) == ""
        mem.recall_facts_by_pattern.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lists_and_caches(self):
        mem = _memory(history=self._ENTRIES)
        first = await dc.her_world_prompt_block("jalsarraf", 3, mem)
        second = await dc.her_world_prompt_block("jalsarraf", 3, mem)
        assert "- bike exhaust: titanium, his call" in first and first == second
        mem.recall_facts_by_pattern.assert_awaited_once_with("her:%", user_id="jalsarraf")

    @pytest.mark.asyncio
    async def test_a_new_fact_refreshes_the_cache(self):
        mem = _memory(pending=PENDING, history=self._ENTRIES)
        await dc.her_world_prompt_block("jalsarraf", 5, mem)
        stack, _ = _resolve_env()
        with stack:
            await dc.resolve_prompt_block("jalsarraf", "A", mem)
        await dc.her_world_prompt_block("jalsarraf", 5, mem)
        assert mem.recall_facts_by_pattern.await_count == 2

    @pytest.mark.asyncio
    async def test_outage_is_soft(self):
        assert await dc.her_world_prompt_block("jalsarraf", 5, MagicMock()) == ""
