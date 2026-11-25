"""Her POV journal lines stay off the chat transcript."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.her_pov_pipeline import HerPOVPipeline


@pytest.mark.asyncio
async def test_journal_line_is_not_persisted_as_a_chat_reply():
    jobs: dict[str, dict] = {}

    async def set_job(job_id, **fields):
        jobs.setdefault(job_id, {"id": job_id}).update(fields)

    pipeline = HerPOVPipeline(
        set_job,
        AsyncMock(
            return_value={
                "user_content": "I waited at the hangar.",
                "assistant_content": "You did not have to.",
                "mood": "tender",
            }
        ),
        AsyncMock(
            return_value={
                "annotation": "I kept the light on.",
                "scene_tags": "hangar, night",
                "couple": False,
                "mood": "tender",
                "title": "The light",
            }
        ),
        AsyncMock(),
    )
    ws = MagicMock()
    ws.send = AsyncMock()
    ws.send_thinking = AsyncMock()
    ws.send_proactive = AsyncMock()
    aff = MagicMock()
    aff.get_state = AsyncMock(return_value=MagicMock(level=3))
    with (
        patch("app.memory_her_pov.get_job", AsyncMock(return_value=None)),
        patch("app.context.ws", ws),
        patch("app.context.affection", aff),
        patch("app.image_gen.generate_image", AsyncMock(return_value=b"\x89PNG")),
        patch("app.image_gen.build_prompt", MagicMock(return_value="prompt")),
        patch("app.memory_archive.save_image", AsyncMock(return_value="mem-1")),
        patch("app.her_day.render_costume", AsyncMock(return_value=None)),
        patch("app.wardrobe.granted_request_for", return_value=None),
        patch("app.proactive.state.now_local", return_value=datetime(2026, 11, 24, 21, 0)),
        patch("app.image_provider.selected_provider", return_value="agy"),
        patch("app.her_pov_pipeline.asyncio.sleep", new=AsyncMock()),
    ):
        await pipeline.run("claude", "job-1")

    assert jobs["job-1"]["status"] == "done"
    journal = [
        call
        for call in ws.send_proactive.await_args_list
        if call.args[1] == "I kept the light on."
    ]
    assert len(journal) == 1
    assert journal[0].kwargs == {"persist": False}
    done = [
        call.args[1]
        for call in ws.send.await_args_list
        if isinstance(call.args[1], dict)
        and call.args[1].get("type") == "her_pov"
        and call.args[1].get("status") == "done"
    ]
    assert done[0]["annotation"] == "I kept the light on."
