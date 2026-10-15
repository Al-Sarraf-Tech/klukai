"""Password saturation is retryable and does not masquerade as bad credentials."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.passwords import PasswordWorkBusy
from app.routes import register_routes


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/auth/login", {"username": "alice", "password": "fixture-password"}),
        (
            "/api/user/change-password",
            {
                "old_password": "fixture-password",
                "new_password": "new-fixture-password",
            },
        ),
    ],
)
def test_saturated_authentication_returns_retryable_service_status(
    monkeypatch, path, payload
):
    monkeypatch.setattr("app.auth.check_ip_banned", AsyncMock(return_value=False))
    monkeypatch.setattr(
        "app.auth.authenticate", AsyncMock(side_effect=PasswordWorkBusy())
    )
    monkeypatch.setattr(
        "app.auth.change_password", AsyncMock(side_effect=PasswordWorkBusy())
    )
    monkeypatch.setattr(
        "app.routes_extras._get_user_id", AsyncMock(return_value="alice")
    )
    app = FastAPI()
    register_routes(app)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(path, json=payload)
    assert response.status_code == 503
    assert int(response.headers["retry-after"]) > 0
    assert "fixture-password" not in response.text


def test_password_change_rejects_oversized_utf8_before_hashing(monkeypatch):
    change = AsyncMock(return_value=False)
    monkeypatch.setattr("app.auth.change_password", change)
    monkeypatch.setattr(
        "app.routes_extras._get_user_id", AsyncMock(return_value="alice")
    )
    app = FastAPI()
    register_routes(app)
    with TestClient(app) as client:
        response = client.post(
            "/api/user/change-password",
            json={
                "old_password": "fixture-password",
                "new_password": "é" * 37,
            },
        )
    assert response.status_code == 422
    change.assert_not_awaited()
