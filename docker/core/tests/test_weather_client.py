"""Tests for app/weather_client.py — Open-Meteo client, fail-soft everywhere.

No real network: httpx.AsyncClient is patched with a FakeClient (mirrors the
FakeClient + patch pattern in tests/test_caches.py).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _fake_client(resp=None, raise_exc=None):
    """Build a FakeClient usable both as a singleton and as a context manager."""

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        @property
        def is_closed(self):
            return False

        async def get(self, *a, **kw):
            if raise_exc is not None:
                raise raise_exc
            return resp

    return FakeClient()


def _resp(status_code=200, json_data=None):
    r = MagicMock()
    r.status_code = status_code
    r.json = MagicMock(return_value=json_data or {})
    return r


def _reset_singleton():
    import app.weather_client as wc

    wc._http = None


# ── commander_coords ──────────────────────────────────────────────────────


class TestCommanderCoords:
    def test_default_is_chicago(self, monkeypatch):
        from app.weather_client import commander_coords

        monkeypatch.delenv("COMMANDER_LATITUDE", raising=False)
        monkeypatch.delenv("COMMANDER_LONGITUDE", raising=False)
        lat, lon = commander_coords()
        assert lat == pytest.approx(41.8781)
        assert lon == pytest.approx(-87.6298)

    def test_env_override(self, monkeypatch):
        from app.weather_client import commander_coords

        monkeypatch.setenv("COMMANDER_LATITUDE", "35.6895")
        monkeypatch.setenv("COMMANDER_LONGITUDE", "139.6917")
        lat, lon = commander_coords()
        assert lat == pytest.approx(35.6895)
        assert lon == pytest.approx(139.6917)

    def test_bad_env_falls_back_to_default(self, monkeypatch):
        from app.weather_client import commander_coords

        monkeypatch.setenv("COMMANDER_LATITUDE", "not-a-number")
        monkeypatch.setenv("COMMANDER_LONGITUDE", "")
        lat, lon = commander_coords()
        assert lat == pytest.approx(41.8781)
        assert lon == pytest.approx(-87.6298)


# ── fetch_weather success ───────────────────────────────────────────────────


class TestFetchWeatherSuccess:
    @pytest.mark.asyncio
    async def test_parses_normalized_dict(self, monkeypatch):
        from app.weather_client import fetch_weather

        _reset_singleton()
        payload = {
            "current": {
                "temperature_2m": 12.5,
                "weather_code": 61,  # rain
                "is_day": 1,
            }
        }
        client = _fake_client(resp=_resp(200, payload))
        with patch("app.weather_client.httpx.AsyncClient", return_value=client):
            result = await fetch_weather(41.0, -87.0)
        assert result is not None
        assert result["temp_c"] == pytest.approx(12.5)
        assert result["code"] == 61
        assert result["condition"] == "rain"
        assert result["is_day"] is True

    @pytest.mark.asyncio
    async def test_uses_commander_coords_when_none(self, monkeypatch):
        from app.weather_client import fetch_weather

        _reset_singleton()
        monkeypatch.delenv("COMMANDER_LATITUDE", raising=False)
        monkeypatch.delenv("COMMANDER_LONGITUDE", raising=False)
        payload = {"current": {"temperature_2m": 0.0, "weather_code": 0, "is_day": 0}}
        captured = {}

        class CapturingClient:
            @property
            def is_closed(self):
                return False

            async def get(self, url, *a, **kw):
                captured["params"] = kw.get("params", {})
                return _resp(200, payload)

        with patch("app.weather_client.httpx.AsyncClient", return_value=CapturingClient()):
            result = await fetch_weather()
        assert result is not None
        assert result["condition"] == "clear"
        assert result["is_day"] is False
        # Default Chicago coords forwarded to the API.
        assert captured["params"]["latitude"] == pytest.approx(41.8781)
        assert captured["params"]["longitude"] == pytest.approx(-87.6298)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "code,expected",
        [
            (0, "clear"),
            (1, "clear"),
            (2, "cloudy"),
            (3, "cloudy"),
            (45, "fog"),
            (48, "fog"),
            (51, "drizzle"),
            (55, "drizzle"),
            (61, "rain"),
            (65, "rain"),
            (80, "rain"),
            (71, "snow"),
            (75, "snow"),
            (85, "snow"),
            (95, "storm"),
            (99, "storm"),
            (12345, "clear"),  # unknown code → safe default
        ],
    )
    async def test_wmo_code_maps_to_condition(self, code, expected):
        from app.weather_client import fetch_weather

        _reset_singleton()
        payload = {"current": {"temperature_2m": 5.0, "weather_code": code, "is_day": 1}}
        client = _fake_client(resp=_resp(200, payload))
        with patch("app.weather_client.httpx.AsyncClient", return_value=client):
            result = await fetch_weather(1.0, 2.0)
        assert result is not None
        assert result["condition"] == expected


# ── fetch_weather fail-soft ─────────────────────────────────────────────────


class TestFetchWeatherFailSoft:
    @pytest.mark.asyncio
    async def test_non_200_returns_none(self):
        from app.weather_client import fetch_weather

        _reset_singleton()
        client = _fake_client(resp=_resp(503, {}))
        with patch("app.weather_client.httpx.AsyncClient", return_value=client):
            result = await fetch_weather(1.0, 2.0)
        assert result is None

    @pytest.mark.asyncio
    async def test_exception_returns_none(self):
        from app.weather_client import fetch_weather

        _reset_singleton()
        client = _fake_client(raise_exc=RuntimeError("network down"))
        with patch("app.weather_client.httpx.AsyncClient", return_value=client):
            result = await fetch_weather(1.0, 2.0)
        assert result is None

    @pytest.mark.asyncio
    async def test_malformed_payload_returns_none(self):
        """200 but missing 'current' block — must not raise, returns None."""
        from app.weather_client import fetch_weather

        _reset_singleton()
        client = _fake_client(resp=_resp(200, {"unexpected": "shape"}))
        with patch("app.weather_client.httpx.AsyncClient", return_value=client):
            result = await fetch_weather(1.0, 2.0)
        assert result is None


# ── _get_http singleton ─────────────────────────────────────────────────────


class TestGetHttpSingleton:
    def test_lazy_init_and_reuse(self):
        import app.weather_client as wc

        _reset_singleton()
        with patch("app.weather_client.httpx.AsyncClient", return_value=_fake_client()) as ctor:
            c1 = wc._get_http()
            c2 = wc._get_http()
        assert c1 is c2
        ctor.assert_called_once()

    def test_recreates_when_closed(self):
        import app.weather_client as wc

        _reset_singleton()

        class ClosedClient:
            is_closed = True

        wc._http = ClosedClient()
        with patch("app.weather_client.httpx.AsyncClient", return_value=_fake_client()):
            c = wc._get_http()
        assert not getattr(c, "is_closed", False)
