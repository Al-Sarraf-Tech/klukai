"""Journal races preserve ownership and never turn recovery into a fresh render."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app import account_images as journal
from app import agy_image_provider as provider

REQUEST_ID = "eb27cd6f-487e-4da7-a06c-b55a89e4975a"


def saved_row():
    return dict(
        id=REQUEST_ID,
        user_id="alice",
        prompt="original private prompt",
        width=832,
        height=1216,
        include_commander=False,
        phase="accepted",
        archive_context={"conversation_id": "original"},
    )


@pytest.fixture
def worker_calls(monkeypatch):
    submit = AsyncMock(side_effect=AssertionError("unexpected fresh render"))
    recover = AsyncMock(return_value={"status": "pending", "image": None})
    update = AsyncMock()
    monkeypatch.setattr(provider, "submit_agy_image", submit)
    monkeypatch.setattr(provider, "recover_agy_image", recover)
    monkeypatch.setattr(journal, "update_request", update)
    return submit, recover, update


async def fulfill():
    return await journal.fulfill_account_image(
        "alice",
        "changed input",
        width=832,
        height=1216,
        include_commander=False,
        request_id=REQUEST_ID,
    )


@pytest.mark.parametrize("during_insert", [False, True])
async def test_foreign_request_id_is_opaque_even_after_insert_race(
    monkeypatch, worker_calls, during_insert
):
    row = saved_row()
    row["user_id"] = "bob"
    fetch = AsyncMock(side_effect=[None, row] if during_insert else [row])
    insert = AsyncMock(side_effect=RuntimeError("duplicate key"))
    monkeypatch.setattr(journal, "fetch_request", fetch)
    monkeypatch.setattr(journal, "insert_request", insert)
    assert await fulfill() == {
        "request_id": REQUEST_ID,
        "phase": "unknown",
        "image": None,
        "generated": False,
        "archived": False,
        "memory_id": None,
        "needs_archive": False,
    }
    for call in worker_calls:
        call.assert_not_awaited()
    assert insert.await_count == int(during_insert)


async def test_own_insert_race_recovers_journaled_payload_without_submitting(
    monkeypatch, worker_calls
):
    monkeypatch.setattr(
        journal, "fetch_request", AsyncMock(side_effect=[None, saved_row()])
    )
    insert = AsyncMock(side_effect=RuntimeError("duplicate key"))
    monkeypatch.setattr(journal, "insert_request", insert)
    result = await fulfill()
    assert result["phase"] == "pending" and result["generated"] is False
    worker_calls[0].assert_not_awaited()
    worker_calls[1].assert_awaited_once_with(
        "original private prompt",
        user_id="alice",
        width=832,
        height=1216,
        include_commander=False,
        request_id=REQUEST_ID,
        nudity=False,
    )
    worker_calls[2].assert_not_awaited()


async def test_insert_failure_without_committed_row_propagates_without_render(
    monkeypatch, worker_calls
):
    monkeypatch.setattr(journal, "fetch_request", AsyncMock(return_value=None))
    monkeypatch.setattr(
        journal,
        "insert_request",
        AsyncMock(side_effect=RuntimeError("database write failed")),
    )
    with pytest.raises(RuntimeError, match="database write failed"):
        await fulfill()
    for call in worker_calls:
        call.assert_not_awaited()


async def test_lost_busy_claim_returns_pending_without_overwriting_owner(
    monkeypatch, worker_calls
):
    row = saved_row()
    row["phase"] = "busy"
    claim = AsyncMock(return_value=False)
    monkeypatch.setattr(journal, "claim_unsubmitted_request", claim)
    result = await journal._recover_saved(row)
    assert result["phase"] == "pending" and result["image"] is None
    claim.assert_awaited_once_with(REQUEST_ID, "alice")
    for call in worker_calls:
        call.assert_not_awaited()


@pytest.mark.parametrize("existing_bytes", [None, b"not a PNG archive"])
async def test_missing_or_corrupt_archive_is_recovered_without_new_submission(
    monkeypatch, tmp_path, worker_calls, existing_bytes
):
    from app import memory_archive

    monkeypatch.setattr(memory_archive, "IMAGES_DIR", str(tmp_path))
    if existing_bytes is not None:
        (tmp_path / "memory-1.png").write_bytes(existing_bytes)
    row = saved_row()
    row.update(phase="archived", memory_id="memory-1")
    worker_calls[1].return_value = {"status": "completed", "image": b"recovered PNG"}
    result = await journal._recover_saved(row)
    assert result["image"] == b"recovered PNG" and result["needs_archive"] is True
    assert result["archived"] is False and result["memory_id"] is None
    assert result["archive_metadata"] == {
        "prompt": "original private prompt",
        "conversation_id": "original",
        "mood": "composed",
        "affection_level": 0,
    }
    worker_calls[0].assert_not_awaited()
    worker_calls[1].assert_awaited_once()
    worker_calls[2].assert_awaited_once_with(REQUEST_ID, phase="completed")


async def test_unknown_recovery_status_does_not_rewrite_a_durable_claim(worker_calls):
    worker_calls[1].return_value = {"status": "provider-extension", "image": None}
    row = saved_row()
    before = row.copy()
    result = await journal._recover_saved(row)
    assert result["phase"] == "unknown" and result["generated"] is False
    assert row == before
    worker_calls[0].assert_not_awaited()
    worker_calls[2].assert_not_awaited()


async def test_empty_completed_submission_cannot_mark_image_generated(worker_calls):
    worker_calls[0].side_effect = None
    worker_calls[0].return_value = {"status": "completed", "image": None}
    result = await journal._submit_saved(saved_row())
    assert result["phase"] == "failed" and result["generated"] is False
    worker_calls[2].assert_awaited_once_with(
        REQUEST_ID, phase="failed", failure="failed"
    )
    worker_calls[1].assert_not_awaited()


def test_journal_rejects_noncanonical_request_identity():
    with pytest.raises(ValueError, match="Invalid image request id"):
        journal._normalize_request_id(REQUEST_ID.replace("-", ""))
