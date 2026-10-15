"""Durable explicit account pictures.

The journal row is written before the worker is contacted. A later call for
the same request reads that row and, when needed, the worker's read-only
recovery. It does not submit the picture again.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

_OPEN = {"accepted", "pending", "unknown", "completed", "archive_failed"}
_CLOSED = {"refused", "failed", "conflict", "unavailable"}


def canonical_payload(
    request_id: str, user_id: str, prompt: str, width: int, height: int,
    include_commander: bool,
) -> dict:
    return {
        "height": height,
        "include_commander": include_commander,
        "prompt": prompt,
        "request_id": request_id,
        "user_id": user_id,
        "width": width,
    }


def payload_digest(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()


_EXPLICIT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "klukai:explicit-account-image:v1")


def explicit_request_id(
    user_id: str, prompt: str, width: int, height: int, include_commander: bool,
) -> str:
    """One id for one explicit picture, stable across a closed tab or a new process."""
    raw = json.dumps(
        {
            "height": height,
            "include_commander": include_commander,
            "prompt": prompt,
            "user_id": user_id,
            "width": width,
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
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
    request_id: str, *, phase: str, image: bytes | None = None,
    archived: bool = False, memory_id: str | None = None, needs_archive: bool = False,
) -> dict:
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


async def fetch_request(request_id: str) -> dict | None:
    from .db import get_conn_autocommit

    async with get_conn_autocommit() as conn:
        row = await (await conn.execute(
            "SELECT id, user_id, payload_digest, prompt, width, height, "
            "include_commander, phase, memory_id "
            "FROM companion_account_image_requests WHERE id = %s",
            (request_id,),
        )).fetchone()
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
    }


async def insert_request(request_id: str, user_id: str, prompt: str, width: int, height: int, include_commander: bool, digest: str) -> None:
    from .db import get_conn_autocommit

    async with get_conn_autocommit() as conn:
        await conn.execute(
            "INSERT INTO companion_account_image_requests "
            "(id, user_id, payload_digest, prompt, width, height, include_commander, phase) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'accepted')",
            (request_id, user_id, digest, prompt, width, height, include_commander),
        )


async def update_request(request_id: str, *, phase: str, memory_id: str | None = None, failure: str | None = None) -> None:
    from .db import get_conn_autocommit

    async with get_conn_autocommit() as conn:
        await conn.execute(
            "UPDATE companion_account_image_requests "
            "SET phase = %s, memory_id = COALESCE(%s, memory_id), failure = %s, updated_at = NOW() "
            "WHERE id = %s",
            (phase, memory_id, failure, request_id),
        )


async def complete_archive(request_id: str, memory_id: str | None, image: bytes, user_id: str) -> dict:
    """Record the save outcome. None is an archive failure, not a saved picture."""
    if memory_id:
        await update_request(request_id, phase="archived", memory_id=str(memory_id))
        return _result(request_id, phase="archived", image=image, archived=True, memory_id=str(memory_id))
    await update_request(request_id, phase="archive_failed", failure="archive")
    return _result(request_id, phase="archive_failed", image=image, archived=False)


async def _recover_saved(row: dict) -> dict:
    from . import agy_image_provider

    request_id = row["id"]
    phase = row["phase"]
    if phase == "archived" and row.get("memory_id"):
        image = _archived_png(row["memory_id"])
        if image is not None:
            return _result(request_id, phase="archived", image=image, archived=True, memory_id=row["memory_id"])
    if phase == "refused":
        return _result(request_id, phase="refused")
    recovered = await agy_image_provider.recover_agy_image(
        row["prompt"], user_id=row["user_id"], width=row["width"], height=row["height"],
        include_commander=row["include_commander"], request_id=request_id,
    )
    if recovered.get("status") == "completed" and recovered.get("image"):
        await update_request(request_id, phase="completed")
        return _result(request_id, phase="completed", image=recovered["image"], needs_archive=True)
    status = recovered.get("status")
    if status not in {"pending", "unknown", "refused", "failed", "conflict", "unavailable"}:
        status = "unknown"
    if phase != "refused":
        await update_request(request_id, phase=status if status in _OPEN or status in _CLOSED else "unknown", failure=status)
    return _result(request_id, phase=status)


async def fulfill_account_image(
    user_id: str, prompt: str, *, width: int, height: int, include_commander: bool,
    request_id: str | None = None,
) -> dict:
    """Accept one explicit picture, or return the image already stored for that id."""
    request_id = _normalize_request_id(request_id)
    digest = payload_digest(canonical_payload(
        request_id, user_id, prompt, width, height, include_commander,
    ))
    current = await fetch_request(request_id)
    if current is not None:
        if current["user_id"] != user_id:
            return _result(request_id, phase="unknown")
        # The id is the picture. Chat rebuilds the prompt from the current
        # affection level and hour-block outfit, so a digest of that new text
        # must not hide the image already stored for this id.
        return await _recover_saved(current)

    try:
        await insert_request(request_id, user_id, prompt, width, height, include_commander, digest)
    except Exception:
        current = await fetch_request(request_id)
        if current is None:
            raise
        if current["user_id"] != user_id:
            return _result(request_id, phase="unknown")
        return await _recover_saved(current)

    from . import agy_image_provider

    submitted = await agy_image_provider.submit_agy_image(
        prompt, user_id=user_id, width=width, height=height,
        include_commander=include_commander, request_id=request_id,
    )
    status = submitted.get("status")
    if status == "completed" and submitted.get("image"):
        await update_request(request_id, phase="completed")
        return _result(request_id, phase="completed", image=submitted["image"], needs_archive=True)
    if status not in {"refused", "failed", "pending", "unknown", "unavailable"}:
        status = "failed"
    await update_request(request_id, phase=status, failure=status)
    return _result(request_id, phase=status)
