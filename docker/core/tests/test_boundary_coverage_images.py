"""Account-worker admission, bounded health bodies, and recovery-only failures."""

from __future__ import annotations

import io
from unittest.mock import AsyncMock
import uuid

import httpx
from PIL import Image
import pytest

from app import agy_image_provider as agy


@pytest.fixture
def worker(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("boundary-only-token")
    token.chmod(0o600)
    monkeypatch.setenv("KLUKAI_CHAT_IMAGE_PROVIDER", "agy")
    monkeypatch.setenv("KLUKAI_AGY_ALLOWED_USERS", "alice")
    monkeypatch.setenv("KLUKAI_AGY_SOCKET", str(tmp_path / "image.sock"))
    monkeypatch.setenv("KLUKAI_AGY_TOKEN_FILE", str(token))
    client_type = httpx.AsyncClient

    def connect(handler):
        monkeypatch.setattr(
            agy.httpx,
            "AsyncHTTPTransport",
            lambda **kwargs: httpx.MockTransport(handler),
        )
        monkeypatch.setattr(
            agy.httpx, "AsyncClient", lambda **kwargs: client_type(**kwargs)
        )

    return connect


@pytest.mark.parametrize(
    "status,body",
    [
        (503, b"private worker failure"),
        (200, b"x" * 8193),
        (200, b"not json"),
        (200, b"[]"),
        (200, b'{"status":"ready","provider":"agy","busy":0}'),
    ],
)
async def test_worker_health_requires_bounded_typed_ready_state(worker, status, body):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/health" and request.method == "GET"
        assert request.headers["Authorization"] == "Bearer boundary-only-token"
        return httpx.Response(status, content=body)

    worker(handler)
    assert await agy.agy_images_ready() is False
    assert len(requests) == 1


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (404, b"{}", "unknown"),
        (403, b"{}", "refused"),
        (502, b"[]", "unavailable"),
        (200, b"\xff", "unavailable"),
        (200, b'{"status":"unexpected"}', "unavailable"),
        (200, b'{"status":"pending"}', "pending"),
    ],
)
def test_recovery_http_fallback_never_reclassifies_a_miss_as_new_work(
    status, body, expected
):
    assert agy._interpret_worker("/recover", status, body, "application/json") == {
        "status": expected,
        "image": None,
    }


@pytest.mark.parametrize("canonical", [False, True])
def test_request_identity_requires_canonical_uuid_spelling(canonical):
    value = uuid.UUID("eb27cd6f-487e-4da7-a06c-b55a89e4975a")
    if canonical:
        assert agy._request_id(str(value).upper()) == str(value)
    else:
        with pytest.raises(ValueError, match="Invalid Agy image request"):
            agy._request_id("{" + str(value) + "}")


def test_transparent_palette_image_is_normalized_to_rgba_without_source_metadata():
    image = Image.new("P", (2, 2), color=0)
    image.putpalette([0, 255, 0] + [0] * 765)
    raw = io.BytesIO()
    image.save(raw, format="PNG", transparency=0)
    image.close()
    normalized = agy._normalize_png(raw.getvalue())
    with Image.open(io.BytesIO(normalized)) as clean:
        assert clean.mode == "RGBA" and clean.getpixel((0, 0)) == (0, 255, 0, 0)
        assert clean.info == {} and clean.size == (2, 2)


async def test_recovery_of_unallowed_identity_does_not_contact_worker(monkeypatch):
    monkeypatch.setenv("KLUKAI_AGY_ALLOWED_USERS", "alice")
    post = AsyncMock(side_effect=AssertionError("unauthorized transport"))
    monkeypatch.setattr(agy, "_post_worker", post)
    assert await agy.recover_agy_image(
        "private scene",
        user_id="mallory",
        width=832,
        height=1216,
        include_commander=False,
        request_id=str(uuid.uuid4()),
    ) == {"status": "refused", "image": None}
    post.assert_not_awaited()


async def test_recovery_transport_failure_remains_unknown_and_redacted(
    worker, monkeypatch, caplog
):
    post = AsyncMock(
        side_effect=httpx.ConnectError("boundary-only-token private scene")
    )
    monkeypatch.setattr(agy, "_post_worker", post)
    result = await agy.recover_agy_image(
        "private scene",
        user_id="alice",
        width=832,
        height=1216,
        include_commander=False,
        request_id=str(uuid.uuid4()),
    )
    assert result == {"status": "unknown", "image": None}
    post.assert_awaited_once()
    assert post.await_args.args[0] == "/recover"
    assert (
        "private scene" not in caplog.text and "boundary-only-token" not in caplog.text
    )


async def test_oversized_worker_body_is_rejected_without_second_submission(
    worker, monkeypatch
):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/generate"
        return httpx.Response(
            200, headers={"content-type": "image/png"}, content=b"x" * 65
        )

    monkeypatch.setattr(agy, "MAX_IMAGE_BYTES", 64)
    worker(handler)
    assert await agy.submit_agy_image("scene", user_id="alice") == {
        "status": "unknown",
        "image": None,
    }
    assert len(requests) == 1
