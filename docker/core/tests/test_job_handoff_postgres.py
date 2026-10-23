"""Opt-in SQL proofs in the existing disposable-schema migration fixture."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import uuid

import pytest

from app import db, memory_her_pov as hp
from tests.test_migrations_postgres import migration_pool  # noqa: F401


@pytest.fixture
async def board(migration_pool, monkeypatch):  # noqa: F811
    migrations = Path(__file__).parents[1] / 'migrations'
    async with migration_pool.connection() as conn:
        await conn.execute((migrations / '170_her_pov_jobs.sql').read_text())
        await conn.execute((migrations / '200_her_pov_dispatch.sql').read_text())
        await conn.execute((migrations / '250_on_demand_image_origin.sql').read_text())

    @asynccontextmanager
    async def connection():
        async with migration_pool.connection() as conn:
            yield conn

    monkeypatch.setattr(db, 'get_conn_autocommit', connection)
    monkeypatch.setattr(db, 'get_pool', lambda: migration_pool)
    monkeypatch.setenv('HER_POV_EXECUTION', 'queue')
    return migration_pool


async def insert(pool, status='queued', attempts=0, claimed=False, finished=False, human=True):
    jid = str(uuid.uuid4())
    async with pool.connection() as conn:
        await conn.execute(
            'INSERT INTO companion_her_pov_jobs (id,user_id,status,attempts,claimed_at,finished_at,requested_by_user) '
            'VALUES (%s,%s,%s,%s,%s,%s,%s)',
            (jid, jid, status, attempts, datetime.now(timezone.utc) if claimed else None,
             datetime.now(timezone.utc) if finished else None, human),
        )
    return jid


async def snapshot(pool):
    async with pool.connection() as conn:
        return await (await conn.execute(
            'SELECT id,status,attempts,claimed_at,updated_at,finished_at FROM companion_her_pov_jobs ORDER BY id'
        )).fetchall()


async def test_only_unstarted_queued_rows_republish_same_id_and_throttle(board, monkeypatch):
    from app import job_handoff
    queued = await insert(board)
    for status in ('searching', 'thinking', 'drawing', 'done', 'failed'):
        await insert(board, status)
    await insert(board, attempts=1)
    await insert(board, claimed=True)
    await insert(board, finished=True)
    before = await snapshot(board)
    hints = []

    async def publish(jid):
        hints.append(jid)
        return 1  # Redis subscriber receipt does not prove broker acceptance.

    monkeypatch.setattr(hp, '_enqueue_job', publish)
    assert await job_handoff.reconcile_once() == 1
    assert hints == [queued]
    assert await job_handoff.reconcile_once() == 0
    assert await snapshot(board) == before
    # Simulate retry interval passing after bridge failed downstream publish.
    async with board.connection() as conn:
        await conn.execute("UPDATE companion_her_pov_jobs SET last_enqueued_at = NOW() - INTERVAL '2 minutes'")
    assert await job_handoff.reconcile_once() == 1
    assert hints == [queued, queued]
    assert await snapshot(board) == before


async def test_failed_publication_and_crash_after_mark_are_recovered(board, monkeypatch):
    from app import job_handoff
    jid = await insert(board)
    async def crash(_jid):
        raise asyncio.CancelledError
    monkeypatch.setattr(hp, '_enqueue_job', crash)
    with pytest.raises(asyncio.CancelledError):
        await job_handoff.reconcile_once()
    # Cancellation after transaction commit must leave a dispatch mark.
    async with board.connection() as conn:
        row = await (await conn.execute('SELECT last_enqueued_at FROM companion_her_pov_jobs WHERE id=%s', (jid,))).fetchone()
        assert row[0] is not None
    assert await job_handoff.reconcile_once() == 0
    async with board.connection() as conn:
        await conn.execute("UPDATE companion_her_pov_jobs SET last_enqueued_at = NOW() - INTERVAL '2 minutes'")
    received = []
    async def failed(jid):
        received.append(jid)
        return False
    monkeypatch.setattr(hp, '_enqueue_job', failed)
    assert await job_handoff.reconcile_once() == 1
    assert received == [jid]
    assert await job_handoff.reconcile_once() == 0


async def test_concurrent_reconcilers_claim_disjoint_bounded_batches(board, monkeypatch):
    from app import job_handoff
    ids = {await insert(board) for _ in range(9)}
    hints = []
    async def publish(jid):
        hints.append(jid)
        await asyncio.sleep(0)
        return False
    monkeypatch.setattr(hp, '_enqueue_job', publish)
    counts = await asyncio.gather(*(job_handoff.reconcile_once(batch_size=2) for _ in range(3)))
    assert counts == [2, 2, 2]
    assert len(hints) == len(set(hints)) == 6
    assert set(hints) <= ids
    assert await job_handoff.reconcile_once() == 3
    assert set(hints) == ids
    assert len(hints) == 9


async def test_startup_recovers_insert_without_any_initial_publication(board, monkeypatch):
    from app import job_handoff
    jid = await insert(board)
    hints = []
    async def publish(value):
        hints.append(value)
        return False
    monkeypatch.setattr(hp, '_enqueue_job', publish)
    async with job_handoff.running():
        assert hints == [jid]


async def test_repeated_cloud_delivery_never_reclaims_started_or_ambiguous_job(board, monkeypatch):
    jid = await insert(board)
    ambiguous = await insert(board, attempts=1, claimed=True)
    monkeypatch.setattr(hp, '_cloud_generation_enabled', lambda: True)
    monkeypatch.setattr(hp, '_JOBS', {})
    monkeypatch.setattr(hp, '_ACTIVE_JOB_BY_USER', {})
    starts = []
    async def run(user_id, job_id):
        starts.append(job_id)
    monkeypatch.setattr(hp, 'run_her_pov', run)
    assert await hp.run_job_from_queue(jid)
    assert await hp.run_job_from_queue(jid)
    assert await hp.run_job_from_queue(ambiguous)
    assert starts == [jid]
    async with board.connection() as conn:
        assert await (await conn.execute('SELECT attempts FROM companion_her_pov_jobs WHERE id=%s', (jid,))).fetchone() == (1,)


async def test_old_unstarted_queued_job_is_not_retired_when_user_checks_again(board, monkeypatch):
    jid = await insert(board)
    async with board.connection() as conn:
        await conn.execute("UPDATE companion_her_pov_jobs SET updated_at = NOW() - INTERVAL '1 day'")
    monkeypatch.setattr(hp, '_cloud_generation_enabled', lambda: False)
    assert await hp._claim_active_from_db(jid) == {'job_id': jid, 'status': 'queued', 'reused': True}


async def test_missing_subscriber_keeps_the_queued_job_without_generation(board, monkeypatch):
    """Publish receipt of nobody leaves the accepted row queued and does not generate."""
    from app import events

    hp._JOBS.clear()
    hp._ACTIVE_JOB_BY_USER.clear()
    receipts = []
    generations = []
    real_publish = events.publish

    async def publish(event_type, data="", **kwargs):
        received = await real_publish(event_type, data, **kwargs)
        receipts.append((event_type, data, kwargs.get("job_id"), received))
        return received

    async def run(user_id, job_id):
        generations.append((user_id, job_id))

    monkeypatch.setattr(events, "publish", publish)
    monkeypatch.setattr(hp, "run_her_pov", run)
    try:
        out = await hp.start_her_pov("durable-proof")
        await asyncio.sleep(0)
        assert generations == []
        assert out["status"] == "queued" and out["reused"] is False
        job_id = out["job_id"]
        assert receipts == [("job.enqueue", job_id, job_id, 0)]
        async with board.connection() as conn:
            row = await (await conn.execute(
                "SELECT status, attempts, claimed_at, finished_at "
                "FROM companion_her_pov_jobs WHERE id = %s",
                (job_id,),
            )).fetchone()
            count = await (await conn.execute(
                "SELECT count(*) FROM companion_her_pov_jobs"
            )).fetchone()
        assert row == ("queued", 0, None, None)
        assert count == (1,)

        hp._JOBS.clear()
        hp._ACTIVE_JOB_BY_USER.clear()
        receipts.clear()
        again = await hp.start_her_pov("durable-proof")
        await asyncio.sleep(0)
        assert again == {"job_id": job_id, "status": "queued", "reused": True}
        assert receipts == []
        assert generations == []
        async with board.connection() as conn:
            row = await (await conn.execute(
                "SELECT status, attempts, claimed_at, finished_at "
                "FROM companion_her_pov_jobs WHERE id = %s",
                (job_id,),
            )).fetchone()
            count = await (await conn.execute(
                "SELECT count(*) FROM companion_her_pov_jobs"
            )).fetchone()
        assert row == ("queued", 0, None, None)
        assert count == (1,)
    finally:
        hp._JOBS.clear()
        hp._ACTIVE_JOB_BY_USER.clear()


async def test_dispatch_migration_preserves_existing_rows(migration_pool):  # noqa: F811
    migrations = Path(__file__).parents[1] / 'migrations'
    async with migration_pool.connection() as conn:
        await conn.execute((migrations / '170_her_pov_jobs.sql').read_text())
    async with migration_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO companion_her_pov_jobs (id,user_id,status,attempts,claimed_at) "
            "VALUES (%s,'claude','drawing',1,NOW())", (str(uuid.uuid4()),)
        )
    before = await snapshot(migration_pool)
    async with migration_pool.connection() as conn:
        await conn.execute((migrations / '200_her_pov_dispatch.sql').read_text())
    assert await snapshot(migration_pool) == before
    async with migration_pool.connection() as conn:
        assert await (await conn.execute('SELECT last_enqueued_at FROM companion_her_pov_jobs')).fetchall() == [(None,)]


async def test_unconfirmed_queue_row_never_republishes_or_renders(board, monkeypatch):
    from unittest.mock import AsyncMock
    from app import job_handoff

    jid = await insert(board, human=False)
    publish, render = AsyncMock(), AsyncMock()
    monkeypatch.setattr(hp, '_enqueue_job', publish)
    monkeypatch.setattr(hp, 'run_her_pov', render)
    assert await job_handoff.reconcile_once() == 0
    for cloud in (False, True):
        monkeypatch.setattr(hp, '_cloud_generation_enabled', lambda cloud=cloud: cloud)
        assert await hp.run_job_from_queue(jid) is True
    publish.assert_not_awaited()
    render.assert_not_awaited()


async def test_origin_migration_preserves_started_jobs_and_only_parks_unstarted(migration_pool):
    migrations = Path(__file__).parents[1] / 'migrations'
    async with migration_pool.connection() as conn:
        await conn.execute((migrations / '170_her_pov_jobs.sql').read_text())
        ids = []
        for status, attempts, claimed in (
            ('queued', 0, False), ('queued', 1, False), ('queued', 0, True),
            ('drawing', 1, True), ('done', 1, True), ('failed', 0, False),
        ):
            jid = str(uuid.uuid4())
            ids.append(jid)
            await conn.execute(
                "INSERT INTO companion_her_pov_jobs "
                "(id,user_id,status,attempts,claimed_at,annotation) VALUES (%s,%s,%s,%s,%s,%s)",
                (jid, jid, status, attempts, datetime.now(timezone.utc) if claimed else None,
                 'synthetic preserved annotation'),
            )
    before = {str(row[0]): row for row in await snapshot(migration_pool)}
    async with migration_pool.connection() as conn:
        await conn.execute((migrations / '250_on_demand_image_origin.sql').read_text())
        rows = await (await conn.execute(
            "SELECT id,status,phase,error,requested_by_user,annotation,finished_at "
            "FROM companion_her_pov_jobs"
        )).fetchall()
    assert len(rows) == len(ids)
    for row in rows:
        assert row[4] is False
        assert row[5] == 'synthetic preserved annotation'
        if str(row[0]) == ids[0]:
            assert row[1:4] == ('failed', 'confirmation_required', 'human_request_required')
            assert row[6] is not None
    after = {str(row[0]): row for row in await snapshot(migration_pool)}
    assert {k: v for k, v in after.items() if k != ids[0]} == {
        k: v for k, v in before.items() if k != ids[0]
    }


async def test_origin_cannot_be_escalated_or_lost_by_progress_upsert(board):
    from app import her_pov_storage

    legacy = await insert(board, human=False)
    human = await insert(board, human=True)
    await her_pov_storage._persist_job(
        legacy, {'user_id': legacy, 'requested_by_user': True}, strict=True
    )
    await her_pov_storage._persist_job(human, {'user_id': human}, strict=True)
    async with board.connection() as conn:
        rows = await (await conn.execute(
            "SELECT id,requested_by_user FROM companion_her_pov_jobs"
        )).fetchall()
    assert {str(row[0]): row[1] for row in rows} == {legacy: False, human: True}
