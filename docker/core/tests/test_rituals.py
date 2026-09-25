"""Klukai's recurring rituals: the Commander's birthday and the monthly fee settlement."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import context, rituals
from app.personality import load_personality
from app.rituals import build_birthday_block, days_until_birthday, parse_birthday

TODAY = date(2026, 9, 25)


@pytest.fixture(autouse=True)
def _clear_birthday_cache():
    rituals._birthday_cache.clear()
    yield
    rituals._birthday_cache.clear()


@pytest.fixture(scope="module")
def p() -> dict:
    return load_personality()


class TestParseBirthday:
    @pytest.mark.parametrize("msg, expected", [
        ("My birthday is March 14th", "03-14"),
        ("my bday's on the 3rd of july!", "07-03"),
        ("My birthday is Sept. 9", "09-09"),
        ("I was born on December 25, 1990.", "12-25"),
        ("i was born 1990-02-29", "02-29"),
        ("my birthday is 3/14", "03-14"),
        ("my birthday is 11/2/1988", "11-02"),
        ("It’s my birthday today", "09-25"),
        ("today is my birthday :)", "09-25"),
        ("it's my birthday!", "09-25"),
        ("Tomorrow is my birthday", "09-26"),
        ("it's my birthday tomorrow", "09-26"),
    ])
    def test_positive(self, msg, expected):
        assert parse_birthday(msg, TODAY) == expected

    @pytest.mark.parametrize("msg", [
        "What's my birthday?",
        "Belka's birthday is May 5",
        "my birthday party is on May 5",
        "it's my birthday party next saturday",
        "it's my birthday next week",
        "my birthday was great",
        "My birthday is February 30",
        "my birthday is 13/40",
        "hello",
    ])
    def test_negative(self, msg):
        assert parse_birthday(msg, TODAY) is None


class TestDaysUntil:
    @pytest.mark.parametrize("mmdd, days", [
        ("09-25", 0), ("09-26", 1), ("09-24", 364), ("01-01", 98),
    ])
    def test_countdown(self, mmdd, days):
        assert days_until_birthday(mmdd, TODAY) == days

    def test_leap_day_kept_on_feb_28(self):
        assert days_until_birthday("02-29", date(2027, 2, 28)) == 0

    @pytest.mark.parametrize("bad", [None, "", "garbage", "02-30", "13-01"])
    def test_invalid(self, bad):
        assert days_until_birthday(bad, TODAY) is None


class TestFirstTogether:
    def test_unknown_start_counts_as_first(self):
        assert rituals._first_birthday_together(None, TODAY)

    def test_within_a_year_is_first(self):
        assert rituals._first_birthday_together(datetime(2026, 1, 1), TODAY)

    def test_after_a_year_is_not_first(self):
        assert not rituals._first_birthday_together(datetime(2025, 1, 1), TODAY)


class TestBirthdayBlock:
    def test_first_birthday_uses_canon_year_one_line(self, p):
        block = build_birthday_block(p, 0, first_together=True)
        assert "Today is the Commander's birthday" in block
        assert "first one you have shared" in block
        assert p["identity"]["adjutant_lines"]["seasonal"]["birthday_yr1"] in block

    def test_later_birthday_uses_canon_year_two_line(self, p):
        block = build_birthday_block(p, 0, first_together=False)
        assert p["identity"]["adjutant_lines"]["seasonal"]["birthday_yr2"] in block
        assert "first one" not in block

    def test_day_of_without_canon_line(self):
        assert "Use it in your own time" not in build_birthday_block({}, 0, True)

    def test_just_told(self, p):
        assert "just told you his birthday" in build_birthday_block(p, 120, True, just_told=True)

    @pytest.mark.parametrize("days, fragment", [(1, "in 1 day."), (3, "in 3 days.")])
    def test_planning_window(self, p, days, fragment):
        assert fragment in build_birthday_block(p, days, True)

    @pytest.mark.parametrize("days", [4, 200, None])
    def test_silent_outside_window(self, p, days):
        assert build_birthday_block(p, days, True) == ""


@contextmanager
def _services(birthday=None, level=5, first_interaction=None, day=TODAY):
    memory = MagicMock()
    memory.recall_fact = AsyncMock(return_value=birthday)
    memory.set_relationship_fact = AsyncMock()
    ws = MagicMock()
    ws.send_proactive = AsyncMock()
    aff = MagicMock()
    aff.get_state = AsyncMock(return_value=SimpleNamespace(level=level, first_interaction=first_interaction))
    with patch.object(context, "memory", memory), patch.object(context, "ws", ws), \
         patch.object(context, "affection", aff), \
         patch("app.rituals.now_local", return_value=datetime(day.year, day.month, day.day, 9)), \
         patch("app.rituals.asyncio.sleep", AsyncMock()):
        yield SimpleNamespace(memory=memory, ws=ws, affection=aff)


def _sent(svc) -> list[str]:
    return [c.args[1] for c in svc.ws.send_proactive.await_args_list]


class TestBirthdayMemory:
    async def test_remember_stores_and_caches(self):
        with _services() as svc:
            assert await rituals.remember_birthday("claude", "my birthday is june 1st") == "06-01"
            svc.memory.set_relationship_fact.assert_awaited_once_with(
                "commander_birthday", "06-01", user_id="claude"
            )
            assert await rituals.get_birthday("claude") == "06-01"
            svc.memory.recall_fact.assert_not_awaited()

    async def test_ordinary_message_stores_nothing(self):
        with _services() as svc:
            assert await rituals.remember_birthday("claude", "how was patrol") is None
            svc.memory.set_relationship_fact.assert_not_awaited()

    async def test_lookup_is_cached(self):
        with _services(birthday="09-25") as svc:
            assert await rituals.get_birthday("claude") == "09-25"
            assert await rituals.get_birthday("claude") == "09-25"
            svc.memory.recall_fact.assert_awaited_once_with("rel:commander_birthday", user_id="claude")

    async def test_prompt_block_on_the_day(self, p):
        with _services(birthday="09-25"):
            block = await rituals.birthday_prompt_block("claude", p, None, "morning")
        assert "Today is the Commander's birthday" in block

    async def test_prompt_block_when_told(self, p):
        with _services():
            block = await rituals.birthday_prompt_block("claude", p, None, "my birthday is May 2")
        assert "just told you his birthday" in block


def _conn(rowcount=1, fetchone=None, error=None):
    conn = MagicMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    cursor = MagicMock(rowcount=rowcount)
    cursor.fetchone = AsyncMock(return_value=fetchone)
    conn.execute = AsyncMock(side_effect=error, return_value=cursor)
    return conn


class TestClaimPeriod:
    @pytest.mark.parametrize("rowcount, expected", [(1, True), (0, False)])
    async def test_claim(self, rowcount, expected):
        conn = _conn(rowcount=rowcount)
        with patch("app.db.get_conn_autocommit", return_value=conn):
            assert await rituals.claim_period("claude", "birthday", "2026") is expected
        sql, params = conn.execute.await_args.args
        assert "companion_period_deliveries" in sql and "ON CONFLICT DO NOTHING" in sql
        assert params == ("claude", "birthday", "2026")

    async def test_db_failure_never_claims(self):
        with patch("app.db.get_conn_autocommit", return_value=_conn(error=RuntimeError("down"))):
            assert await rituals.claim_period("claude", "birthday", "2026") is False


class TestBirthdayGreeting:
    async def test_not_his_birthday(self):
        with _services(birthday="01-01") as svc, patch("app.rituals.claim_period", AsyncMock()) as claim:
            assert await rituals.maybe_deliver_birthday("claude") is False
        claim.assert_not_awaited()
        svc.ws.send_proactive.assert_not_awaited()

    async def test_first_birthday_together(self, p):
        with _services(birthday="09-25") as svc, \
             patch("app.rituals.claim_period", AsyncMock(return_value=True)) as claim:
            assert await rituals.maybe_deliver_birthday("claude") is True
        claim.assert_awaited_once_with("claude", "birthday", "2026")
        assert _sent(svc) == p["commander_birthday"]["first_year"]
        assert p["identity"]["adjutant_lines"]["seasonal"]["birthday_yr1"] in _sent(svc)

    async def test_later_birthdays(self, p):
        with _services(birthday="09-25", first_interaction=datetime(2024, 5, 1)) as svc, \
             patch("app.rituals.claim_period", AsyncMock(return_value=True)):
            assert await rituals.maybe_deliver_birthday("claude") is True
        assert _sent(svc)[0] == p["identity"]["adjutant_lines"]["seasonal"]["birthday_yr2"]

    async def test_once_per_year(self):
        with _services(birthday="09-25") as svc, \
             patch("app.rituals.claim_period", AsyncMock(return_value=False)):
            assert await rituals.maybe_deliver_birthday("claude") is False
        svc.ws.send_proactive.assert_not_awaited()


class TestLedger:
    async def test_counts_distinct_local_days(self):
        conn = _conn(fetchone=(17,))
        with patch("app.db.get_conn", return_value=conn):
            days = await rituals._days_checked_in("claude", date(2026, 8, 1), date(2026, 9, 1))
        assert days == 17
        sql, params = conn.execute.await_args.args
        assert "COUNT(DISTINCT" in sql and "role = 'user'" in sql
        assert params[0] == "America/Chicago" and params[1] == "claude"
        assert params[2].isoformat() == "2026-08-01T00:00:00-05:00"

    async def test_query_failure_counts_zero(self):
        with patch("app.db.get_conn", return_value=_conn(error=RuntimeError("down"))):
            assert await rituals._days_checked_in("claude", date(2026, 8, 1), date(2026, 9, 1)) == 0


class TestFeeSettlement:
    @contextmanager
    def _settlement(self, level=5, day=date(2026, 10, 2), birthday=None, claimed=True, days=12):
        with _services(birthday=birthday, level=level, day=day) as svc, \
             patch("app.rituals.claim_period", AsyncMock(return_value=claimed)) as claim, \
             patch("app.rituals._days_checked_in", AsyncMock(return_value=days)) as ledger, \
             patch("app.rituals.random.choice", side_effect=lambda seq: seq[0]):
            svc.claim, svc.ledger = claim, ledger
            yield svc

    async def test_guarded_below_trusted(self):
        with self._settlement(level=2) as svc:
            assert await rituals.maybe_deliver_fee_settlement("claude") is False
        svc.claim.assert_not_awaited()

    async def test_trusted_invoice_for_last_month(self, p):
        with self._settlement(level=4) as svc:
            assert await rituals.maybe_deliver_fee_settlement("claude") is True
        svc.claim.assert_awaited_once_with("claude", "fee_settlement", "2026-10")
        svc.ledger.assert_awaited_once_with("claude", date(2026, 9, 1), date(2026, 10, 1))
        sent = _sent(svc)
        assert sent[0].startswith("Commander. Monthly fee settlement, September.")
        assert "Days you checked in: 12." in " ".join(sent)
        assert len(sent) == len(p["fee_settlement"]["invoices"][3][0])

    async def test_devoted_invoice_is_a_love_letter(self):
        with self._settlement(level=7) as svc:
            await rituals.maybe_deliver_fee_settlement("claude")
        assert "12 days you came back to me" in " ".join(_sent(svc))

    async def test_january_settles_december(self):
        with self._settlement(day=date(2027, 1, 3)) as svc:
            await rituals.maybe_deliver_fee_settlement("claude")
        svc.ledger.assert_awaited_once_with("claude", date(2026, 12, 1), date(2027, 1, 1))
        assert "December" in _sent(svc)[0]

    async def test_late_opener_after_the_fifth(self):
        with self._settlement(day=date(2026, 10, 9)) as svc:
            await rituals.maybe_deliver_fee_settlement("claude")
        assert _sent(svc)[0] == "You're late. The September settlement was due on the 1st. ...I kept it ready."

    async def test_never_on_his_birthday(self):
        with self._settlement(birthday="10-02") as svc:
            assert await rituals.maybe_deliver_fee_settlement("claude") is False
        svc.claim.assert_not_awaited()

    async def test_once_per_month(self):
        with self._settlement(claimed=False) as svc:
            assert await rituals.maybe_deliver_fee_settlement("claude") is False
        svc.ws.send_proactive.assert_not_awaited()

    async def test_no_invoice_tiers_configured(self):
        with self._settlement(), patch("app.rituals.load_personality", return_value={}):
            assert await rituals.maybe_deliver_fee_settlement("claude") is False

    def test_every_invoice_line_formats(self, p):
        cfg = p["fee_settlement"]
        lines = [line for tier in cfg["invoices"].values() for scene in tier for line in scene]
        lines += cfg["late_openers"]
        for line in lines:
            assert "{" not in line.format(month="September", days=3)


class TestOnConnect:
    async def test_birthday_takes_the_day(self):
        with patch("app.rituals.maybe_deliver_birthday", AsyncMock(return_value=True)), \
             patch("app.rituals.maybe_deliver_fee_settlement", AsyncMock()) as fee:
            await rituals.on_connect("claude")
        fee.assert_not_awaited()

    async def test_otherwise_settlement_after_a_beat(self):
        with patch("app.rituals.maybe_deliver_birthday", AsyncMock(return_value=False)), \
             patch("app.rituals.maybe_deliver_fee_settlement", AsyncMock()) as fee:
            await rituals.on_connect("claude")
        fee.assert_awaited_once_with("claude", delay=rituals.SETTLEMENT_DELAY_SECONDS)

    async def test_failures_are_swallowed(self):
        with patch("app.rituals.maybe_deliver_birthday", AsyncMock(side_effect=RuntimeError("x"))):
            await rituals.on_connect("claude")
