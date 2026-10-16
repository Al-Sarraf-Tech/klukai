"""Explicit disposable SQL proof for cancellation of accepted image work."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from app import account_images, agy_image_provider, db, main
from app.migrations import apply_migrations
from tests.runtime_backend_support import backends as backends, clients as clients
from tests.test_application_shutdown import application as application


@pytest.mark.asyncio
async def test_shutdown_retains_accepted_journal_and_next_lifespan_only_recovers(
    backends, clients, application, monkeypatch
):
    manager, state, _ = application
    pool = db.get_pool()
    await apply_migrations(pool, Path(__file__).resolve().parents[1] / "migrations")
    request_id = "b828505d-4ddb-48b8-a2e8-a1bdd43832f3"
    entered = asyncio.Event()
    calls = []
    finalizer_rows = []

    async def submit(prompt, **kwargs):
        calls.append(("generate", kwargs["request_id"], prompt))
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            # This executes the real DB boundary from the cancelled task.
            row = await account_images.fetch_request(request_id)
            finalizer_rows.append((row["id"], row["phase"], state["database_closed"]))

    async def recover(prompt, **kwargs):
        calls.append(("recover", kwargs["request_id"], prompt))
        return {"status": "pending", "image": None}

    monkeypatch.setattr(agy_image_provider, "submit_agy_image", submit)
    monkeypatch.setattr(agy_image_provider, "recover_agy_image", recover)
    task = None
    try:
        async with main.lifespan(main.app):
            task = asyncio.create_task(
                account_images.fulfill_account_image(
                    "owner",
                    "original accepted picture",
                    width=832,
                    height=1216,
                    include_commander=False,
                    request_id=request_id,
                )
            )
            manager.track_task("owner", task, survive_disconnect=True)
            await entered.wait()
        assert task.done()
        assert finalizer_rows == [(request_id, "accepted", False)]
        saved = await account_images.fetch_request(request_id)
        assert saved["phase"] == "accepted" and saved["id"] == request_id
        async with main.lifespan(main.app):
            result = await account_images.fulfill_account_image(
                "owner",
                "changed retry context",
                width=832,
                height=1216,
                include_commander=False,
                request_id=request_id,
            )
        assert result["request_id"] == request_id and result["phase"] == "pending"
        assert calls == [
            ("generate", request_id, "original accepted picture"),
            ("recover", request_id, "original accepted picture"),
        ]
        assert (await account_images.fetch_request(request_id))["phase"] == "accepted"
        sources = [
            "app/runtime_tasks.py",
            "app/ws_manager.py",
            "app/main.py",
            "app/account_images.py",
            "tests/lifecycle_runtime_backends.py",
            "tests/test_application_shutdown.py",
        ]
        root = Path(__file__).resolve().parents[1]
        (backends.evidence / "accepted-image-shutdown.json").write_text(
            json.dumps(
                {
                    "request_id": request_id,
                    "journal_phase_after_shutdown": "accepted",
                    "generate_calls": 1,
                    "recovery_calls": 1,
                    "finalizer_read_before_database_close": True,
                    "source_sha256": {
                        source: hashlib.sha256((root / source).read_bytes()).hexdigest()
                        for source in sources
                    },
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
