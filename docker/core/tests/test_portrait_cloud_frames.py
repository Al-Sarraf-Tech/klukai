import base64
import hashlib
import io
from unittest.mock import AsyncMock
import pytest
from PIL import Image
from app import portrait_cloud_frames as cloud, portrait_account as account
from app.portrait_assets import frame_dir, frame_file


def png(color):
    b = io.BytesIO()
    Image.new("RGB", (832, 1216), color).save(b, "PNG")
    return b.getvalue()


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGES_DIR", str(tmp_path))
    monkeypatch.setattr(account, "enabled_for", lambda user: user == "claude")
    monkeypatch.setattr(account, "draw_base", AsyncMock(return_value=True))
    folder = frame_dir("claude", "blazing_star")
    folder.mkdir(parents=True)
    (folder / "base.png").write_bytes(png("gray"))
    frame_file("claude", "blazing_star", "base").write_bytes(b"keep-original-base")
    return folder


async def test_only_missing_frames_and_base_unchanged(setup, monkeypatch):
    request = AsyncMock(return_value={"status": "completed", "image": png("black")})
    monkeypatch.setattr(cloud.provider, "submit_agy_image", request)
    assert await cloud.draw_missing("claude", "blazing_star", 0)
    assert request.await_count == 2
    for call in request.call_args_list:
        src = call.kwargs["portrait_source"]
        raw = base64.b64decode(src["image"])
        assert len(raw) <= 42000 and hashlib.sha256(raw).hexdigest() == src["sha256"]
        assert call.kwargs["include_commander"] is False
    assert (
        frame_file("claude", "blazing_star", "base").read_bytes()
        == b"keep-original-base"
    )
    request.reset_mock()
    assert await cloud.draw_missing("claude", "blazing_star", 0)
    request.assert_not_awaited()


async def test_pending_is_observed_without_new_paid_submission(setup, monkeypatch):
    submit = AsyncMock(return_value={"status": "pending"})
    recover = AsyncMock(return_value={"status": "pending"})
    monkeypatch.setattr(cloud.provider, "submit_agy_image", submit)
    monkeypatch.setattr(cloud.provider, "recover_agy_image", recover)
    assert not await cloud.draw_missing("claude", "blazing_star", 0)
    assert submit.await_count == recover.await_count == 1
    assert (
        submit.call_args.kwargs["request_id"] == recover.call_args.kwargs["request_id"]
    )


async def test_unauthorized_user_cannot_draw(setup, monkeypatch):
    request = AsyncMock(side_effect=AssertionError("no spend"))
    monkeypatch.setattr(cloud.provider, "submit_agy_image", request)
    assert not await cloud.draw_missing("other", "blazing_star", 0)
