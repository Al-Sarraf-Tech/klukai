"""Durable image requests must carry human origin, including after restart."""

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import her_pov_storage, job_handoff, memory_her_pov as hp

JOB = "55555555-5555-5555-5555-555555555555"


def connection():
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone = AsyncMock(return_value=None)
    cursor.fetchall = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value=cursor)

    @asynccontextmanager
    async def opened():
        yield conn

    return conn, opened


async def test_direct_tap_records_origin_before_queue_publication(monkeypatch):
    monkeypatch.setenv("HER_POV_EXECUTION", "queue")
    monkeypatch.setattr(hp, "_JOBS", {})
    monkeypatch.setattr(hp, "_ACTIVE_JOB_BY_USER", {})
    with patch.object(hp, "_cloud_generation_enabled", return_value=True), patch.object(
        hp, "_claim_active_from_db", AsyncMock(return_value=None)
    ), patch.object(hp, "_persist_job", AsyncMock()) as persist, patch.object(
        hp, "_enqueue_job", AsyncMock()
    ):
        await hp.start_her_pov("claude")
    assert persist.await_args.args[1]["requested_by_user"] is True


@pytest.mark.parametrize("human", [False, True])
async def test_persistence_origin_is_insert_only(human):
    conn, opened = connection()
    with patch("app.db.get_pool"), patch("app.db.get_conn_autocommit", opened):
        await her_pov_storage._persist_job(
            JOB, {"user_id": "claude", "requested_by_user": human}, strict=True
        )
    sql, params = conn.execute.await_args.args
    insert, update = sql.split("ON CONFLICT")
    assert "requested_by_user" in insert
    assert "requested_by_user" not in update
    assert params[-2] is human


@pytest.mark.parametrize("cloud", [False, True])
async def test_worker_claim_requires_human_origin_even_for_local_images(cloud):
    conn, opened = connection()
    with patch("app.db.get_conn_autocommit", opened), patch.object(
        hp, "_cloud_generation_enabled", return_value=cloud
    ), patch.object(hp, "run_her_pov", AsyncMock()) as run:
        assert await hp.run_job_from_queue(JOB) is True
    sql = conn.execute.await_args.args[0]
    assert "requested_by_user IS TRUE" in sql
    run.assert_not_awaited()


@pytest.mark.parametrize("human", [None, False, True])
async def test_local_db_outage_requires_explicit_origin_on_memory_board(monkeypatch, human):
    job = {"id": JOB, "user_id": "claude", "status": "queued"}
    if human is not None:
        job["requested_by_user"] = human
    monkeypatch.setattr(hp, "_JOBS", {JOB: job})
    monkeypatch.setattr(hp, "_ACTIVE_JOB_BY_USER", {})
    with patch("app.db.get_conn_autocommit", side_effect=RuntimeError("offline")), patch.object(
        hp, "_cloud_generation_enabled", return_value=False
    ), patch.object(hp, "run_her_pov", AsyncMock()) as run:
        assert await hp.run_job_from_queue(JOB) is True
    assert run.await_count == int(human is True)


async def test_reconciler_only_republishes_human_requests():
    conn, opened = connection()
    with patch("app.db.get_conn_autocommit", opened):
        assert await job_handoff.reconcile_once() == 0
    assert "requested_by_user IS TRUE" in conn.execute.await_args.args[0]


def test_migration_parks_only_unstarted_unconfirmed_jobs_without_deleting_data():
    migration = Path(__file__).parents[1] / "migrations/250_on_demand_image_origin.sql"
    sql = migration.read_text()
    assert "requested_by_user BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "status = 'failed'" in sql and "phase = 'confirmation_required'" in sql
    assert "status = 'queued'" in sql and "attempts = 0" in sql
    assert "claimed_at IS NULL" in sql and "finished_at IS NULL" in sql
    assert "requested_by_user IS FALSE" in sql
    assert "DELETE" not in sql and "DROP" not in sql
