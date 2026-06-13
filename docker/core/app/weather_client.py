"""Open-Meteo weather client — keyless, fail-soft.

Fetches current conditions for the Commander's location so Klukai can shade her
mood and greeting by the weather where he actually is. The client NEVER raises:
any network error, timeout, non-200, or malformed payload returns ``None`` and
the caller simply keeps the current mood. Mirrors the lazy httpx singleton in
``app/image_gen.py``.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

# Open-Meteo current-weather endpoint — keyless, no auth, generous free tier.
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Default location: Chicago (matches America/Chicago wall clock used elsewhere).
DEFAULT_LATITUDE = 41.8781
DEFAULT_LONGITUDE = -87.6298

# WMO weather interpretation codes → coarse condition buckets.
# https://open-meteo.com/en/docs — code ranges grouped into 7 classes.
_WMO_CONDITION: dict[int, str] = {
    0: "clear",   # clear sky
    1: "clear",   # mainly clear
    2: "cloudy",  # partly cloudy
    3: "cloudy",  # overcast
    45: "fog",
    48: "fog",    # depositing rime fog
    51: "drizzle", 53: "drizzle", 55: "drizzle",
    56: "drizzle", 57: "drizzle",  # freezing drizzle
    61: "rain", 63: "rain", 65: "rain",
    66: "rain", 67: "rain",  # freezing rain
    80: "rain", 81: "rain", 82: "rain",  # rain showers
    71: "snow", 73: "snow", 75: "snow", 77: "snow",  # snow + grains
    85: "snow", 86: "snow",  # snow showers
    95: "storm",  # thunderstorm
    96: "storm", 99: "storm",  # thunderstorm with hail
}

_http: httpx.AsyncClient | None = None


def _get_http() -> httpx.AsyncClient:
    """Lazily create (and reuse) a single httpx.AsyncClient.

    Mirrors ``app.image_gen._get_http`` — recreates the client if a previous one
    was closed.
    """
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=10.0)
    return _http


def commander_coords() -> tuple[float, float]:
    """Return the Commander's (latitude, longitude).

    Reads ``COMMANDER_LATITUDE`` / ``COMMANDER_LONGITUDE``; falls back to Chicago
    on missing, empty, or unparseable values (robust to bad env).
    """
    lat = _coerce_float(os.environ.get("COMMANDER_LATITUDE"), DEFAULT_LATITUDE)
    lon = _coerce_float(os.environ.get("COMMANDER_LONGITUDE"), DEFAULT_LONGITUDE)
    return lat, lon


def _coerce_float(raw: str | None, default: float) -> float:
    """Parse ``raw`` as a float, returning ``default`` on any failure."""
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except (ValueError, AttributeError):
        return default


def _code_to_condition(code: int) -> str:
    """Map a WMO weather code to a coarse condition string.

    Unknown codes fall back to ``"clear"`` (safe, non-alarming default).
    """
    return _WMO_CONDITION.get(code, "clear")


async def fetch_weather(
    lat: float | None = None, lon: float | None = None
) -> dict | None:
    """Fetch current weather from Open-Meteo. FAIL-SOFT: returns None on any error.

    Args:
        lat: Latitude; defaults to the Commander's coords when None.
        lon: Longitude; defaults to the Commander's coords when None.

    Returns:
        A normalized dict ``{"temp_c": float, "code": int, "condition": str,
        "is_day": bool}``, or ``None`` if the request fails, returns non-200, or
        the payload is malformed. Never raises.
    """
    if lat is None or lon is None:
        d_lat, d_lon = commander_coords()
        lat = d_lat if lat is None else lat
        lon = d_lon if lon is None else lon

    try:
        client = _get_http()
        resp = await client.get(
            OPEN_METEO_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,weather_code,is_day",
            },
        )
        if resp.status_code != 200:
            logger.warning("Open-Meteo returned HTTP %s — skipping weather", resp.status_code)
            return None

        data = resp.json()
        current = data.get("current")
        if not isinstance(current, dict):
            logger.warning("Open-Meteo payload missing 'current' block — skipping weather")
            return None

        code = int(current.get("weather_code", 0))
        return {
            "temp_c": float(current.get("temperature_2m", 0.0)),
            "code": code,
            "condition": _code_to_condition(code),
            "is_day": bool(current.get("is_day", 0)),
        }
    except Exception as e:  # noqa: BLE001 — fail-soft: weather must never break chat.
        logger.warning("Weather fetch failed (%s) — skipping weather", e)
        return None
