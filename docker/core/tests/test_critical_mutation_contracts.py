"""Independent model-wire, persistence-owner and scoring policy contracts."""

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
import json
import logging
from unittest.mock import AsyncMock

import httpx
import pytest

from app import affection
from app import extraction_llm as xl
from app.affection import AffectionManager
from app.affection_classifier import classify_interaction
from app.affection_types import compute_level
from app.circuit_breakers import State, get_breaker

TODAY = date(2026, 5, 20)
FIRST = datetime(2026, 1, 1, 12)
LEVELS = [
    {"index": 0, "threshold": 0, "name": "Cold"},
    {"index": 1, "threshold": 100, "name": "Warm"},
]


def _haiku_message(text):
    return {
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }


@pytest.fixture
def haiku_wire(monkeypatch):
    """The real background transport, against an in-process Grok turn.

    ``server["handler"]`` sees the prompt that would have been the model call.
    """
    from app.grok_chat import GrokChatError, Turn

    monkeypatch.setattr("app.grok_chat.configured", lambda: True)
    monkeypatch.delenv("KLUKAI_EXTRACTION_MODEL", raising=False)
    monkeypatch.setattr(xl, "reserve_attempt", AsyncMock(return_value="attempt-1"))
    monkeypatch.setattr(xl, "record_usage", AsyncMock())
    server = {"handler": None}

    async def complete_turn(system, prompt, *, timeout=60, role="chat"):
        class Request:
            method = "POST"
            url = "http://klukai-agy/complete"
            headers = {}

        request = Request()
        request.content = json.dumps({
            "model": "grok-4.7", "effort": "low", "system": system, "prompt": prompt,
        }).encode()
        try:
            response = server["handler"](request)
        except Exception as exc:
            raise GrokChatError("failed") from exc
        body = json.loads(response.content)
        return Turn(body["content"][0]["text"], "grok-4.7", "low", "stream")

    monkeypatch.setattr("app.grok_chat.complete_turn", complete_turn)
    return server


@pytest.mark.parametrize(
    "content,expected",
    [
        ('{"type":"compliment","intensity":7}', ("compliment", 7)),
        ('```json\n{"type":"remembering","intensity":11}\n```', ("remembering", 10)),
        ('  {"type":"greeting","intensity":-3}  ', ("greeting", 1)),
        ('{"type":"personal_sharing"}', ("personal_sharing", 5)),
        ('{"intensity":6}', ("neutral", 6)),
    ],
)
async def test_classifier_obeys_complete_wire_contract(haiku_wire, content, expected):
    sent = []
    message = "A" * 499 + "Z" + "PRIVATE_TAIL"

    def model_server(request):
        sent.append(request)
        return httpx.Response(200, json=_haiku_message(content))

    haiku_wire["handler"] = model_server
    result = await classify_interaction(
        message, prompt_template="ONLY COMMANDER: {user_message}"
    )
    assert result == expected
    assert len(sent) == 1
    request = sent[0]
    assert request.method == "POST"
    assert "authorization" not in request.headers
    body = json.loads(request.content)
    assert body["model"] == "grok-4.7" and body["effort"] == "low"
    assert body["system"] == xl.SYSTEM_PROMPT
    assert body["prompt"] == "ONLY COMMANDER: " + "A" * 499 + "Z"
    assert "PRIVATE_TAIL" not in body["prompt"]


async def test_classifier_failures_open_haiku_breaker_without_secret_logs(
    haiku_wire, caplog
):
    caplog.set_level(logging.DEBUG)
    attempts = []
    secret = "server-secret-must-not-leak"

    def unavailable(request):
        attempts.append(request)
        raise httpx.ConnectError(secret, request=request)

    haiku_wire["handler"] = unavailable
    for _ in range(3):
        assert await classify_interaction("hi", prompt_template="{user_message}") == (
            "neutral",
            5,
        )
    assert len(attempts) == 3
    # Background failures never trip the paid chat breaker, and the secret
    # from the transport does not reach the log.
    assert get_breaker("anthropic").state == State.CLOSED
    assert secret not in caplog.text


class Rows:
    def __init__(self, row):
        self.row, self.statements, self.commits = row, [], 0

    async def execute(self, statement, params):
        # The migration schema defines these columns and their binding order.
        # SQL keyword case and whitespace are deliberately outside the oracle.
        canonical = "".join(statement.casefold().split())
        allowed = [
            "SELECT score, level, level_name, last_interaction_date, consecutive_days, daily_points_earned, total_interactions, first_interaction FROM companion_affection WHERE user_id = %s",
            "UPDATE companion_affection SET score=%s, level=%s, level_name=%s WHERE user_id=%s",
            "UPDATE companion_affection SET score = %s, level = %s, level_name = %s, last_interaction_date = %s, consecutive_days = %s, daily_points_earned = %s, total_interactions = %s, first_interaction = %s, updated_at = NOW() WHERE user_id = %s",
            "INSERT INTO companion_affection_log (delta, reason, old_score, new_score, old_level, new_level, user_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        ]
        assert canonical in {"".join(sql.casefold().split()) for sql in allowed}
        self.statements.append((statement, params))
        return self

    async def fetchone(self):
        return self.row

    async def commit(self):
        self.commits += 1

    @asynccontextmanager
    async def connection(self):
        yield self

    def updates(self):
        return [
            (sql, params) for sql, params in self.statements if sql.startswith("UPDATE")
        ]


@pytest.fixture
def configured_manager(monkeypatch):
    config = {
        "affection": {
            "levels": LEVELS,
            "scoring": {
                "compliment": [4, 4],
                "daily_points_cap": 100,
                "daily_consistency_bonus": 11,
            },
        }
    }
    monkeypatch.setattr(affection, "load_personality", lambda: config)
    monkeypatch.setattr(
        affection, "now_local", lambda: datetime.combine(TODAY, datetime.min.time())
    )
    rows = Rows((97, 0, "Cold", TODAY - timedelta(days=1), 2, 9, 7, FIRST))
    monkeypatch.setattr(affection, "get_conn", rows.connection)
    monkeypatch.setattr(affection, "get_conn_autocommit", rows.connection)
    manager = AffectionManager()
    manager._levels = LEVELS
    return manager, rows


async def test_daily_reward_crosses_level_and_persists_every_owner_field(
    configured_manager, monkeypatch
):
    manager, rows = configured_manager

    async def publish(*args, **kwargs):
        return None

    monkeypatch.setattr(affection, "publish_event", publish)
    change = await manager.apply_classification("compliment", 8, user_id="alice")
    assert change.model_dump() == {
        "delta": 15,
        "reason": "compliment",
        "new_score": 112,
        "new_level": 1,
        "new_level_name": "Warm",
        "level_changed": True,
        "level_direction": "up",
    }
    state = manager._states["alice"]
    assert (
        state.last_interaction_date,
        state.consecutive_days,
        state.daily_points_earned,
        state.total_interactions,
        state.first_interaction,
    ) == (TODAY, 3, 15, 8, FIRST)
    updates = rows.updates()
    assert len(updates) == 1
    sql, parameters = updates[0]
    assert "WHERE user_id = %s" in sql
    assert parameters == (112, 1, "Warm", TODAY, 3, 15, 8, FIRST, "alice")
    logs = [
        (sql, params) for sql, params in rows.statements if sql.startswith("INSERT")
    ]
    assert len(logs) == 1
    assert logs[0][1] == (15, "compliment", 97, 112, 0, 1, "alice")
    assert rows.commits == 2


async def test_flat_reward_persists_result_under_requested_owner(configured_manager):
    manager, rows = configured_manager
    state = await manager.add_score(8, user_id="alice")
    assert (state.score, state.level, state.level_name) == (105, 1, "Warm")
    assert rows.updates()[0][1] == (
        105,
        1,
        "Warm",
        TODAY - timedelta(days=1),
        2,
        9,
        7,
        FIRST,
        "alice",
    )
    assert rows.commits == 1


@pytest.mark.parametrize(
    "method", ["add_score", "apply_classification", "classify_and_adjust"]
)
async def test_legacy_default_owner_is_commander(
    configured_manager, haiku_wire, method
):
    manager, rows = configured_manager
    if method == "classify_and_adjust":
        haiku_wire["handler"] = lambda _: httpx.Response(
            200, json=_haiku_message('{"type":"neutral","intensity":5}')
        )
        await manager.classify_and_adjust("Hi Commander", "Hi")
    elif method == "add_score":
        await manager.add_score(-9)
    else:
        await manager.apply_classification("neutral", 5)
    assert len(rows.updates()) == 1
    assert rows.updates()[0][1][-1] == "jalsarraf"
    assert rows.updates()[0][1][0] == 1000
    assert "alice" not in manager._states


@pytest.mark.parametrize("config", [{}, {"affection": {}}])
async def test_optional_affection_configuration_initializes_safely(monkeypatch, config):
    rows = Rows(None)
    monkeypatch.setattr(affection, "get_conn", rows.connection)
    monkeypatch.setattr(affection, "load_personality", lambda: config)
    manager = AffectionManager()
    try:
        await manager.init()
        assert manager._compute_level(50) == (0, "Cold Assessment")
        assert manager._levels == []
    finally:
        await manager.close()
    # Classification uses the shared background route: no client to own.
    assert not hasattr(manager, "_http")


@pytest.mark.parametrize(
    "levels,score,expected",
    [
        ([], 200, (0, "Cold Assessment")),
        ([{}], -1, (0, "Cold Assessment")),
        ([{}], 0, (0, "Unknown")),
        ([{"threshold": 5}], 5, (0, "Unknown")),
        ([{"index": 7, "name": "Configured"}], 0, (7, "Configured")),
        (LEVELS, 99, (0, "Cold")),
        (LEVELS, 100, (1, "Warm")),
    ],
)
def test_level_boundaries_have_explicit_defaults(levels, score, expected):
    assert compute_level(levels, score) == expected


async def test_database_projection_preserves_each_independent_state_field(
    configured_manager,
):
    manager, rows = configured_manager
    state = await manager.get_state("alice")
    assert state.model_dump() == {
        "score": 97,
        "level": 0,
        "level_name": "Cold",
        "last_interaction_date": TODAY - timedelta(days=1),
        "consecutive_days": 2,
        "daily_points_earned": 9,
        "total_interactions": 7,
        "first_interaction": FIRST,
    }
    assert rows.statements[0][1] == ("alice",)


def test_audit_hash_matches_independent_canonical_protocol_vector(monkeypatch):
    import hashlib
    import hmac
    from app.audit_chain import compute_row_hash, verify_chain

    monkeypatch.setenv("AUDIT_CHAIN_SECRET", "critical-known-key")
    canonical = b'{"created_at": "2026-05-20T12:00:00Z", "event_type": "login.success", "id": 7, "ip_address": "192.0.2.7", "metadata": {"a": 1, "z": "\\u00e9"}, "prev": "", "request_id": "req-7", "user_id": "alice"}'
    expected = hmac.new(b"critical-known-key", canonical, hashlib.sha256).hexdigest()
    row = {
        "id": 7,
        "event_type": "login.success",
        "user_id": "alice",
        "ip_address": "192.0.2.7",
        "request_id": "req-7",
        "metadata": {"z": "é", "a": 1},
        "created_at": "2026-05-20T12:00:00Z",
    }
    assert (
        compute_row_hash(
            row_id=7,
            event_type=row["event_type"],
            user_id="alice",
            ip_address="192.0.2.7",
            request_id="req-7",
            metadata=row["metadata"],
            created_at=row["created_at"],
            prev_hash=None,
        )
        == expected
    )
    row["chain_hash"] = expected
    assert verify_chain([row]) == {
        "valid": True,
        "break_at_id": None,
        "checked": 1,
        "expected_hash": expected,
    }
    row["chain_hash"] = "invalid"
    assert verify_chain([row]) == {
        "valid": False,
        "break_at_id": 7,
        "checked": 0,
        "expected_hash": expected,
        "reason": "hash_mismatch",
    }
    row["chain_hash"] = None
    assert verify_chain([row]) == {
        "valid": False,
        "break_at_id": 7,
        "checked": 0,
        "expected_hash": expected,
        "reason": "missing_chain_hash",
    }


@pytest.mark.parametrize("configured_bonus,expected", [(None, 7), (0, 4), (11, 15)])
async def test_consistency_reward_uses_configured_value_or_default(
    configured_manager, monkeypatch, configured_bonus, expected
):
    manager, rows = configured_manager
    scoring = {"compliment": [4, 4]}
    if configured_bonus is not None:
        scoring["daily_consistency_bonus"] = configured_bonus
    monkeypatch.setattr(
        affection, "load_personality", lambda: {"affection": {"scoring": scoring}}
    )

    async def publish(*args, **kwargs):
        return None

    monkeypatch.setattr(affection, "publish_event", publish)
    change = await manager.apply_classification("compliment", 7, user_id="alice")
    assert change.delta == expected
    assert change.new_score == 97 + expected
    assert rows.updates()[0][1][5] == expected


async def test_absence_decay_default_is_one_point_per_missed_full_day(
    configured_manager,
):
    manager, rows = configured_manager
    rows.row = (97, 0, "Cold", TODAY - timedelta(days=4), 2, 9, 7, FIRST)
    await manager._apply_absence_decay("alice")
    assert manager._states["alice"].score == 94
    assert rows.updates()[0][1][0] == 94
    logs = [params for sql, params in rows.statements if sql.startswith("INSERT")]
    assert logs == [(-3, "absence_decay (3 days)", 97, 94, 0, 0, "alice")]


@pytest.mark.parametrize(
    "db_score,expected,restored",
    [(150, 150, False), (149, 200, True), (210, 210, False)],
)
async def test_cached_progress_floor_is_owner_scoped_and_committed(
    configured_manager, db_score, expected, restored
):
    from app.affection import AffectionState

    manager, rows = configured_manager
    manager._states["alice"] = AffectionState(score=200, level=1, level_name="Warm")
    rows.row = (db_score, 1, "Warm", TODAY, 1, 3, 9, FIRST)
    await manager._load_state("alice")
    assert manager._states["alice"].score == expected
    updates = rows.updates()
    if restored:
        assert len(updates) == 1
        assert updates[0][1] == (200, 1, "Warm", "alice")
        assert rows.commits == 1
    else:
        assert updates == []
        assert rows.commits == 0


async def test_loading_without_an_owner_keeps_commander_compatibility(
    configured_manager,
):
    manager, rows = configured_manager
    await manager._load_state()
    assert set(manager._states) == {"jalsarraf"}
    assert rows.statements[0][1] == ("jalsarraf",)
