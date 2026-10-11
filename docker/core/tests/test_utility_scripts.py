"""Safety contracts of the standalone utility scripts outside the app package:

- tools/load-test/probe.py counts every 4xx/5xx as an error and keeps timed-out
  requests in the latency sample.
- scripts/regen_images.py restores the SERVED files and never replaces one.
- scripts/canon_probe.py probes as a test user with a minted token it revokes,
  and refuses the Commander's account without the explicit override.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

REPO = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ── probe.py ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def probe() -> ModuleType:
    return _load("klukai_load_probe", REPO / "tools" / "load-test" / "probe.py")


def test_probe_targets_only_unauthenticated_paths(probe: ModuleType) -> None:
    paths = [path for _label, _method, path in probe.TARGETS]
    assert paths == ["/health", "/api/health/live"]
    assert "/api/health/subsystems" not in paths


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "errors"),
    [(200, 0), (401, 1), (404, 1), (503, 1)],
)
async def test_probe_counts_any_4xx_or_5xx_as_error(
    probe: ModuleType, status: int, errors: int
) -> None:
    result = probe.EndpointResult(label="t", path="/x", method="GET", requests=1, errors=0)
    client = MagicMock()
    client.request = AsyncMock(return_value=MagicMock(status_code=status))

    await probe._hit(client, "GET", "/x", result)

    assert result.errors == errors
    assert len(result.latencies_ms) == 1


@pytest.mark.asyncio
async def test_probe_timeout_is_an_error_sampled_at_the_timeout(probe: ModuleType) -> None:
    result = probe.EndpointResult(label="t", path="/x", method="GET", requests=1, errors=0)
    client = MagicMock()
    client.request = AsyncMock(side_effect=httpx.ReadTimeout("slow"))

    await probe._hit(client, "GET", "/x", result)

    assert result.errors == 1
    assert result.latencies_ms == [probe.REQUEST_TIMEOUT_S * 1000]
    assert result.p99 == probe.REQUEST_TIMEOUT_S * 1000


# ── regen_images.py ─────────────────────────────────────────────────────────


def _png(color: tuple[int, int, int]) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 40), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def regen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    module = _load("klukai_regen_images", REPO / "scripts" / "regen_images.py")
    monkeypatch.setattr(module, "IMAGES_DIR", tmp_path)
    monkeypatch.setattr(module, "DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    return module


@pytest.mark.asyncio
async def test_regen_restores_served_files_and_never_replaces_one(
    regen: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    served = _png((10, 20, 30))
    (tmp_path / "served.webp").write_bytes(served)  # row A: served file present
    (tmp_path / "frompng.png").write_bytes(_png((200, 0, 0)))  # row B: PNG only
    rows = [
        {"id": "served", "prompt": "p", "filename": "served.webp", "thumb_filename": "served_thumb.webp"},
        {"id": "frompng", "prompt": "p", "filename": "frompng.webp", "thumb_filename": "frompng_thumb.webp"},
        {"id": "fresh", "prompt": "p", "filename": "fresh.webp", "thumb_filename": "fresh_thumb.webp"},
    ]
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(return_value=rows)
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=cursor)
    conn.close = AsyncMock()
    monkeypatch.setattr(regen.psycopg.AsyncConnection, "connect", AsyncMock(return_value=conn))
    generate = AsyncMock(return_value=_png((0, 0, 200)))
    monkeypatch.setattr(regen, "_generate_image", generate)

    await regen.main()

    sql = conn.execute.await_args.args[0]
    assert "filename, thumb_filename" in sql and "WHERE kept = true" in sql

    # A: untouched, byte for byte.
    assert (tmp_path / "served.webp").read_bytes() == served
    # B: re-encoded from its archive PNG without a GPU render.
    assert (tmp_path / "frompng.webp").exists()
    assert (tmp_path / "frompng_thumb.webp").exists()
    # C: generated once, then PNG + served WebP + thumbnail written.
    generate.assert_awaited_once_with("p")
    assert (tmp_path / "fresh.png").exists()
    assert (tmp_path / "fresh.webp").exists()
    assert (tmp_path / "fresh_thumb.webp").exists()

    from PIL import Image

    with Image.open(tmp_path / "fresh.webp") as img:
        assert img.format == "WEBP" and img.size == (64, 40)
    with Image.open(tmp_path / "fresh_thumb.webp") as thumb:
        assert thumb.format == "WEBP" and thumb.width == 320  # same grid width as save_image
    conn.close.assert_awaited_once()


# ── canon_probe.py ──────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def canon() -> ModuleType:
    return _load("klukai_canon_probe", REPO / "scripts" / "canon_probe.py")


def test_canon_probe_refuses_the_commander_without_override(
    canon: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = MagicMock()
    monkeypatch.setattr(canon, "mint_token", mint)

    assert canon.main(["--user", "jalsarraf"]) == 2

    mint.assert_not_called()


def test_canon_probe_defaults_to_test_user_and_always_revokes(
    canon: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert canon.DEFAULT_USER != "jalsarraf"
    mint = MagicMock(return_value="tok")
    revoke = MagicMock()
    monkeypatch.setattr(canon, "mint_token", mint)
    monkeypatch.setattr(canon, "revoke_token", revoke)

    async def boom(token: str) -> None:
        assert token == "tok"
        raise RuntimeError("socket died")

    monkeypatch.setattr(canon, "probe", boom)

    with pytest.raises(RuntimeError):
        canon.main([])

    mint.assert_called_once_with(canon.DEFAULT_USER)
    revoke.assert_called_once_with("tok")


def test_canon_probe_ws_url_authenticates_by_query_param(canon: ModuleType) -> None:
    assert canon.ws_url("abc").endswith("/ws?token=abc")
    assert canon.ws_url("abc").startswith(("ws://", "wss://"))
