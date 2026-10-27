"""The one-off Qdrant -> PostgreSQL memory copy is additive and text-free."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "klukai-memory-backfill-from-qdrant.py"


def _load():
    spec = importlib.util.spec_from_file_location("memory_backfill", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module here
    spec.loader.exec_module(module)
    return module


backfill = _load()

EXCHANGE_ID = "6f1c2b9e-1d0a-4f3e-9c55-0b7f2a4d8e11"
EPISODE_ID = "0c9a5b3e-7f21-4a6b-8d4e-5f1a2b3c4d5e"
CONVERSATION = "2b8e7c1a-9d3f-4e5b-a6c7-d8e9f0a1b2c3"


def _exchange(**payload):
    base = {
        "user_content": "Remember the rooftop?",
        "assistant_content": "I do.",
        "topics": ["rooftop", 3, ""],
        "mood": "warm",
        "importance": 0.8,
        "user_id": "tj",
        "conversation_id": CONVERSATION,
        "created_at": "2026-09-01T20:15:00+00:00",
    }
    base.update(payload)
    return {"id": EXCHANGE_ID, "payload": base}


def _episode(**payload):
    base = {
        "summary": "A quiet night on watch.",
        "keywords": ["watch"],
        "emotion_tags": ["calm"],
        "importance": 0.6,
        "conversation_id": "not-a-uuid",
        "created_at": "2026-08-02T01:00:00Z",
        "user_id": "jalsarraf",
    }
    base.update(payload)
    return {"id": EPISODE_ID, "payload": base}


def test_exchange_row_maps_every_column_and_keeps_the_timestamp():
    row = backfill.exchange_row(_exchange())
    assert row == (
        EXCHANGE_ID,
        CONVERSATION,
        "Remember the rooftop?",
        "I do.",
        ["rooftop"],
        "warm",
        0.8,
        "tj",
        datetime(2026, 9, 1, 20, 15, tzinfo=timezone.utc),
    )


def test_episode_row_uses_the_point_id_for_provenance_and_dedupe():
    row = backfill.episode_row(_episode())
    assert row[0] == row[6] == row[9] == EPISODE_ID
    assert row[1] is None  # a non-UUID conversation id becomes NULL
    assert row[2:6] == ("A quiet night on watch.", ["watch"], ["calm"], 0.6)
    assert row[7] == "jalsarraf"
    assert row[8] == datetime(2026, 8, 2, 1, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "point",
    [
        {"id": "not-a-uuid", "payload": {"user_content": "a", "assistant_content": "b"}},
        {"id": EXCHANGE_ID, "payload": None},
        _exchange(user_content="   ", assistant_content=""),
        _exchange(user_content=None, assistant_content=None),
    ],
)
def test_unusable_exchanges_are_skipped(point):
    assert backfill.exchange_row(point) is None


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_interrupted_turn_preserves_its_existing_side(missing):
    row = backfill.exchange_row(_exchange(assistant_content=missing))
    assert row[2:4] == ("Remember the rooftop?", "")
    row = backfill.exchange_row(_exchange(user_content=missing))
    assert row[2:4] == ("", "I do.")


def test_unusable_episodes_are_skipped():
    assert backfill.episode_row(_episode(summary="")) is None
    assert backfill.episode_row({"id": 7, "payload": {"summary": "x"}}) is None


@pytest.mark.parametrize(
    ("payload", "importance", "user", "created"),
    [
        ({"importance": 7, "user_id": " "}, 1.0, "jalsarraf", datetime(2026, 9, 1, 20, 15, tzinfo=timezone.utc)),
        ({"importance": -1, "created_at": "yesterday"}, 0.0, "tj", None),
        ({"importance": True, "created_at": "2026-09-01T20:15:00"}, 0.5, "tj", datetime(2026, 9, 1, 20, 15, tzinfo=timezone.utc)),
        ({"importance": "high", "mood": ""}, 0.5, "tj", datetime(2026, 9, 1, 20, 15, tzinfo=timezone.utc)),
    ],
)
def test_defaults_for_odd_payload_values(payload, importance, user, created):
    row = backfill.exchange_row(_exchange(**payload))
    assert row[6] == importance and row[7] == user and row[8] == created
    if "mood" in payload:
        assert row[5] == "composed"


@pytest.mark.parametrize("value", ["2026-08-02T01:00:00", "2026-08-02T01:00:00Z", "2026-08-02T03:00:00+02:00"])
def test_legacy_episode_and_exchange_dates_keep_the_same_instant(value):
    expected = datetime(2026, 8, 2, 1, 0, tzinfo=timezone.utc)
    assert backfill.exchange_row(_exchange(created_at=value))[8] == expected
    assert backfill.episode_row(_episode(created_at=value))[8] == expected


def test_scroll_follows_pages_and_never_asks_for_vectors():
    calls = []
    pages = [
        {"result": {"points": [_exchange(), "junk"], "next_page_offset": "next"}},
        {"result": {"points": [_exchange()], "next_page_offset": None}},
    ]

    def fetch(url, body):
        calls.append((url, body))
        return pages[len(calls) - 1]

    got = list(backfill.points("http://q:6333/", "companion_exchanges", fetch))
    assert len(got) == 2
    assert calls[0][0] == "http://q:6333/collections/companion_exchanges/points/scroll"
    assert all(body["with_vector"] is False for _, body in calls)
    assert "offset" not in calls[0][1] and calls[1][1]["offset"] == "next"


@pytest.mark.parametrize("reply", [{}, {"result": {"points": "x"}}, {"result": []}])
def test_malformed_scroll_replies_stop_the_copy(reply):
    with pytest.raises(ValueError):
        list(backfill.points("http://q", "companion_episodes", lambda url, body: reply))


class _Cursor:
    def __init__(self, inserted):
        self._inserted = iter(inserted)
        self.rowcount = 0
        self.executed = []

    def execute(self, sql, row):
        self.executed.append((sql, row))
        self.rowcount = next(self._inserted)


def test_copy_inserts_only_and_counts_existing_rows():
    cursor = _Cursor([1, 0])
    rows = iter([backfill.exchange_row(_exchange()), None, backfill.exchange_row(_exchange())])
    report = backfill.copy(rows, backfill.EXCHANGE_SQL, cursor, 7)
    assert (report.read, report.invalid, report.inserted, report.existing) == (3, 1, 1, 1)
    assert report.users == {"tj": 2}
    for sql, _ in cursor.executed:
        assert sql.startswith("INSERT INTO companion_exchanges")
        assert "ON CONFLICT (id) DO NOTHING" in sql
        assert "UPDATE" not in sql and "DELETE" not in sql


def test_episode_sql_skips_points_already_linked_by_embedding_id():
    assert "WHERE NOT EXISTS (SELECT 1 FROM companion_episodes WHERE embedding_id = %s)" in backfill.EPISODE_SQL
    assert backfill.EPISODE_SQL.count("%s") == len(backfill.episode_row(_episode()))
    assert backfill.EXCHANGE_SQL.count("%s") == len(backfill.exchange_row(_exchange()))


def test_dry_run_reads_but_never_connects_and_prints_no_memory_text(capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)  # importing it would fail
    pages = {
        "companion_exchanges": [_exchange()],
        "companion_episodes": [_episode()],
    }

    def fetch(url, body):
        collection = url.split("/collections/")[1].split("/")[0]
        return {"result": {"points": pages[collection], "next_page_offset": None}}

    assert backfill.main(["--qdrant", "http://q"], fetch=fetch) == 0
    out = capsys.readouterr().out
    assert "companion_exchanges (dry run): read=1 invalid=0" in out
    assert "companion_episodes (dry run): read=1 invalid=0" in out
    assert "rooftop" not in out and "quiet night" not in out


def test_apply_needs_a_database_url(capsys):
    assert backfill.main(["--apply", "--database-url", ""], fetch=lambda url, body: {}) == 2
    assert "DATABASE_URL is required" in capsys.readouterr().err
