"""Durable explicit account pictures.

The journal row is written before the worker is contacted. A later call for
the same request reads that row and, when needed, the worker's read-only
recovery. Only an explicit worker busy rejection (no claim was accepted)
may be submitted later, using the originally journaled payload.

Background pictures (a missing Live Portrait, the album seeder) use the same
journal, tagged ``archive_context["kind"]``, and render inside
:func:`background_render`. The worker has one slot: while a background job
holds it, an explicit picture that is answered "busy" waits for that job and
is then submitted once with its journalled payload, so a background render
never makes his own picture fail.
"""

from __future__ import annotations
from typing import Any


import asyncio
import contextvars
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Background allowances (portraits per day) turn over at his local midnight.
LOCAL_TZ = ZoneInfo("America/Chicago")
BACKGROUND_WAIT_POLL_S = 5.0
_MARKER_NAME = "background-render.json"
_in_background: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "klukai_background_render", default=False
)


def canonical_payload(
    request_id: str,
    user_id: str,
    prompt: str,
    width: int,
    height: int,
    include_commander: bool,
) -> dict[str, Any]:
    return {
        "height": height,
        "include_commander": include_commander,
        "prompt": prompt,
        "request_id": request_id,
        "user_id": user_id,
        "width": width,
    }


def payload_digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(raw).hexdigest()


_EXPLICIT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "klukai:explicit-account-image:v1")

# Asking again with the same words within this window finds the picture already
# made (he closed the tab, or didn't see it arrive); after it, the same words
# make a new picture instead of returning the first one forever.
REPEAT_WINDOW_SECONDS = 600


def repeat_window(now: float | None = None) -> int:
    """The repeat window ``now`` (default: the current time) falls in."""
    return int((time.time() if now is None else now) // REPEAT_WINDOW_SECONDS)


def explicit_request_id(
    user_id: str,
    prompt: str,
    width: int,
    height: int,
    include_commander: bool,
    scene: str | None = None,
    window: int | None = None,
) -> str:
    """One id for one explicit picture, stable across a closed tab or a new process.

    ``scene`` is the place the picture is set in when it came from outside
    the sentence (his earlier words, where she is): "take a picture of us" in
    his room and at the beach are two pictures. ``window`` is the
    :func:`repeat_window` it was asked in, so the same words later make a new
    picture. None for either keeps the original id.
    """
    identity: dict[str, Any] = {
        "height": height,
        "include_commander": include_commander,
        "prompt": prompt,
        "user_id": user_id,
        "width": width,
    }
    if scene is not None:
        identity["scene"] = scene
    if window is not None:
        identity["window"] = window
    raw = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return str(uuid.uuid5(_EXPLICIT_NAMESPACE, raw))


def _normalize_request_id(request_id: str | None) -> str:
    if request_id is None:
        return str(uuid.uuid4())
    parsed = uuid.UUID(str(request_id))
    if str(parsed) != str(request_id).lower():
        raise ValueError("Invalid image request id")
    return str(parsed)


def _result(
    request_id: str,
    *,
    phase: str,
    image: bytes | None = None,
    archived: bool = False,
    memory_id: str | None = None,
    needs_archive: bool = False,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "phase": phase,
        "image": image,
        "generated": image is not None,
        "archived": archived,
        "memory_id": memory_id,
        "needs_archive": needs_archive,
    }


def _archived_png(memory_id: str) -> bytes | None:
    from .memory_archive import IMAGES_DIR

    path = Path(IMAGES_DIR) / f"{memory_id}.png"
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    return data


async def fetch_request(request_id: str) -> dict[str, Any] | None:
    from .db import get_conn_autocommit

    async with get_conn_autocommit() as conn:
        row = await (
            await conn.execute(
                "SELECT id, user_id, payload_digest, prompt, width, height, "
                "include_commander, phase, memory_id, archive_context "
                "FROM companion_account_image_requests WHERE id = %s",
                (request_id,),
            )
        ).fetchone()
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "user_id": row[1],
        "payload_digest": row[2],
        "prompt": row[3],
        "width": row[4],
        "height": row[5],
        "include_commander": row[6],
        "phase": row[7],
        "memory_id": row[8],
        "archive_context": row[9],
    }


async def insert_request(
    request_id: str,
    user_id: str,
    prompt: str,
    width: int,
    height: int,
    include_commander: bool,
    digest: str,
    archive_context: dict[str, Any] | None = None,
) -> None:
    from .db import get_conn_autocommit

    async with get_conn_autocommit() as conn:
        await conn.execute(
            "INSERT INTO companion_account_image_requests "
            "(id, user_id, payload_digest, prompt, width, height, include_commander, phase, archive_context) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'accepted', %s::jsonb)",
            (
                request_id,
                user_id,
                digest,
                prompt,
                width,
                height,
                include_commander,
                json.dumps(archive_context or {}),
            ),
        )


async def update_request(
    request_id: str,
    *,
    phase: str,
    memory_id: str | None = None,
    failure: str | None = None,
) -> None:
    from .db import get_conn_autocommit

    async with get_conn_autocommit() as conn:
        await conn.execute(
            "UPDATE companion_account_image_requests "
            "SET phase = %s, memory_id = COALESCE(%s, memory_id), failure = %s, updated_at = NOW() "
            "WHERE id = %s AND (phase <> 'archived' OR %s = 'archived')",
            (phase, memory_id, failure, request_id, phase),
        )


def _archive_metadata(row: dict[str, Any]) -> dict[str, Any]:
    """The generation's original context, never the retry's live session.

    Older journal rows have no recorded context. Keep that absence explicit
    with neutral defaults rather than infer historical state from a new turn.
    """
    context = row.get("archive_context") or {}
    return {
        "prompt": row["prompt"],
        "conversation_id": context.get("conversation_id", "unknown"),
        "mood": context.get("mood", "composed"),
        "affection_level": context.get("affection_level", 0),
    }


async def claim_unsubmitted_request(request_id: str, user_id: str) -> bool:
    """CAS: only a proven 429/no-claim rejection can be resubmitted once.

    Persist the accepted/uncertain boundary before calling the worker. A
    concurrent retry or process interruption must not authorize another send.
    """
    from .db import get_conn_autocommit

    async with get_conn_autocommit() as conn:
        row = await (
            await conn.execute(
                "UPDATE companion_account_image_requests "
                "SET phase = 'accepted', failure = NULL, updated_at = NOW() "
                "WHERE id = %s AND user_id = %s AND phase = 'busy' RETURNING id",
                (request_id, user_id),
            )
        ).fetchone()
    return row is not None


def _granted_nudity(row: dict[str, Any]) -> bool:
    """The grant recorded with the request, so recovery resends the same payload."""
    context = row.get("archive_context") or {}
    return context.get("nudity") is True and not row["include_commander"]


async def _submit_saved(row: dict[str, Any]) -> dict[str, Any]:
    from . import agy_image_provider

    request_id = row["id"]
    submitted = await agy_image_provider.submit_agy_image(
        row["prompt"],
        user_id=row["user_id"],
        width=row["width"],
        height=row["height"],
        include_commander=row["include_commander"],
        request_id=request_id,
        nudity=_granted_nudity(row),
    )
    status = submitted.get("status")
    if status == "completed" and submitted.get("image"):
        await update_request(request_id, phase="completed")
        return {
            **_result(
                request_id,
                phase="completed",
                image=submitted["image"],
                needs_archive=True,
            ),
            "archive_metadata": _archive_metadata(row),
        }
    if status not in {"refused", "failed", "pending", "unknown", "unavailable", "busy"}:
        status = "failed"
    await update_request(request_id, phase=status, failure=status)
    if status not in {"pending", "busy"}:  # busy: yielded, and retried or logged
        # He is told the visualization failed; say why here (never the prompt).
        logger.warning(
            "Account picture %s for %s not drawn: %s",
            request_id[:8],
            row["user_id"],
            status,
        )
    return _result(request_id, phase=status)


async def complete_archive(
    request_id: str, memory_id: str | None, image: bytes, user_id: str
) -> dict[str, Any]:
    """Record the save outcome. None is an archive failure, not a saved picture."""
    if memory_id:
        await update_request(request_id, phase="archived", memory_id=str(memory_id))
        return _result(
            request_id,
            phase="archived",
            image=image,
            archived=True,
            memory_id=str(memory_id),
        )
    await update_request(request_id, phase="archive_failed", failure="archive")
    return _result(request_id, phase="archive_failed", image=image, archived=False)


async def _recover_saved(row: dict[str, Any]) -> dict[str, Any]:
    from . import agy_image_provider

    request_id = row["id"]
    phase = row["phase"]
    if phase == "busy":
        if await claim_unsubmitted_request(request_id, row["user_id"]):
            return await _submit_saved(row)
        # Another caller owns submission. Do not overwrite its phase or send.
        return _result(request_id, phase="pending")
    if phase == "archived" and row.get("memory_id"):
        image = _archived_png(row["memory_id"])
        if image is not None:
            return _result(
                request_id,
                phase="archived",
                image=image,
                archived=True,
                memory_id=row["memory_id"],
            )
    if phase == "refused":
        return _result(request_id, phase="refused")
    recovered = await agy_image_provider.recover_agy_image(
        row["prompt"],
        user_id=row["user_id"],
        width=row["width"],
        height=row["height"],
        include_commander=row["include_commander"],
        request_id=request_id,
        nudity=_granted_nudity(row),
    )
    if recovered.get("status") == "completed" and recovered.get("image"):
        await update_request(request_id, phase="completed")
        return {
            **_result(
                request_id,
                phase="completed",
                image=recovered["image"],
                needs_archive=True,
            ),
            "archive_metadata": _archive_metadata(row),
        }
    status = recovered.get("status")
    if status not in {
        "pending",
        "unknown",
        "refused",
        "failed",
        "conflict",
        "unavailable",
    }:
        status = "unknown"
    # Recovery is an observation, not submission ownership. In particular a
    # stale /recover miss must not erase a concurrent definitive busy rejection.
    # Keep the journal unchanged until bytes can be archived.
    return _result(request_id, phase=status)


# ── Background renders yield to his own pictures ─────────────────────────────


def _marker_path() -> Path:
    return Path(os.environ.get("IMAGES_DIR", "/images")) / ".creative" / _MARKER_NAME


def background_wait_limit() -> float:
    """How long an explicit picture waits for one background render: one full
    worker request plus slack, after which it is submitted (or stays busy)."""
    from .agy_image_provider import REQUEST_DEADLINE_SECONDS

    return float(REQUEST_DEADLINE_SECONDS + 30)


def in_background_render() -> bool:
    """True inside :func:`background_render` (this task is the background job)."""
    return _in_background.get()


def _read_marker() -> dict[str, Any] | None:
    try:
        data = json.loads(_marker_path().read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def background_render_active(now: float | None = None) -> bool:
    """A background job (any process in this container) holds the worker."""
    data = _read_marker()
    until = data.get("until") if data else None
    if isinstance(until, bool) or not isinstance(until, (int, float)):
        return False
    return float(until) > (time.time() if now is None else now)


def _write_marker(marker_id: str, kind: str) -> None:
    path = _marker_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(f"{path.name}.{marker_id}.tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(
                {
                    "id": marker_id,
                    "kind": kind,
                    "until": time.time() + background_wait_limit(),
                },
                handle,
            )
        os.replace(tmp, path)
    except OSError as error:
        # The job still runs; his picture just cannot see it and may get "busy".
        logger.warning(
            "Background render marker unavailable (%s)", type(error).__name__
        )


def _clear_marker(marker_id: str) -> None:
    data = _read_marker()
    if data is None or data.get("id") != marker_id:
        return  # another job took the slot over; its marker stays
    try:
        _marker_path().unlink()
    except OSError:
        pass


@asynccontextmanager
async def background_render(kind: str) -> AsyncIterator[None]:
    """Hold the account worker for one background picture (``kind``: portrait,
    album_seed). The marker is a file on the images volume with an expiry, so
    the album seeder (its own process in the same container) is seen too."""
    marker_id = uuid.uuid4().hex
    token = _in_background.set(True)
    _write_marker(marker_id, kind)
    try:
        yield
    finally:
        _in_background.reset(token)
        _clear_marker(marker_id)


async def wait_for_background_render() -> bool:
    """Wait (bounded) while a background render holds the worker. False when
    none did, so a plain "busy" is answered exactly as before."""
    if not background_render_active():
        return False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + background_wait_limit()
    while background_render_active() and loop.time() < deadline:
        await asyncio.sleep(BACKGROUND_WAIT_POLL_S)
    return True


def local_day_start(now: datetime | None = None) -> datetime:
    """Midnight of his local day (America/Chicago) containing ``now``."""
    moment = (now or datetime.now(timezone.utc)).astimezone(LOCAL_TZ)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


async def count_requests_since(kind: str, since: datetime) -> int:
    """Journalled background requests of ``kind`` (all users) since ``since``.

    Busy rejections and recoveries reuse their row, so this counts pictures
    started, not attempts."""
    from .db import get_conn_autocommit

    async with get_conn_autocommit() as conn:
        row = await (
            await conn.execute(
                "SELECT count(*) FROM companion_account_image_requests "
                "WHERE archive_context->>'kind' = %s AND created_at >= %s",
                (kind, since),
            )
        ).fetchone()
    return int(row[0]) if row else 0


async def fulfill_account_image(
    user_id: str,
    prompt: str,
    *,
    width: int,
    height: int,
    include_commander: bool,
    request_id: str | None = None,
    archive_context: dict[str, Any] | None = None,
    nudity: bool = False,
) -> dict[str, Any]:
    """Accept one explicit picture, or return the image already stored for that id.

    ``nudity`` (granted by the caller through image_provider.nudity_allowed) is
    journalled in ``archive_context`` with the request, never re-derived later.
    A "busy" answer caused by a background render is waited out once (see the
    module docstring); a background caller never waits.
    """
    outcome = await _fulfill(
        user_id,
        prompt,
        width=width,
        height=height,
        include_commander=include_commander,
        request_id=request_id,
        archive_context=archive_context,
        nudity=nudity,
    )
    if outcome["phase"] != "busy" or in_background_render():
        return outcome
    if not await wait_for_background_render():
        return outcome
    row = await fetch_request(outcome["request_id"])
    if row is None or row["user_id"] != user_id:
        return outcome
    return await _recover_saved(row)


async def _fulfill(
    user_id: str,
    prompt: str,
    *,
    width: int,
    height: int,
    include_commander: bool,
    request_id: str | None,
    archive_context: dict[str, Any] | None,
    nudity: bool,
) -> dict[str, Any]:
    archive_context = dict(archive_context or {})
    if nudity and not include_commander:
        archive_context["nudity"] = True
    request_id = _normalize_request_id(request_id)
    digest = payload_digest(
        canonical_payload(
            request_id,
            user_id,
            prompt,
            width,
            height,
            include_commander,
        )
    )
    current = await fetch_request(request_id)
    if current is not None:
        if current["user_id"] != user_id:
            return _result(request_id, phase="unknown")
        # The id is the picture. Chat rebuilds the prompt from the current
        # affection level and hour-block outfit, so a digest of that new text
        # must not hide the image already stored for this id.
        return await _recover_saved(current)

    try:
        await insert_request(
            request_id,
            user_id,
            prompt,
            width,
            height,
            include_commander,
            digest,
            archive_context,
        )
    except Exception:
        current = await fetch_request(request_id)
        if current is None:
            raise
        if current["user_id"] != user_id:
            return _result(request_id, phase="unknown")
        return await _recover_saved(current)

    return await _submit_saved(
        {
            "id": request_id,
            "user_id": user_id,
            "prompt": prompt,
            "width": width,
            "height": height,
            "include_commander": include_commander,
            "archive_context": archive_context or {},
        }
    )
