"""Durable explicit account pictures.

The journal row is written before the worker is contacted. A later call for
the same request reads that row and, when needed, the worker's read-only
recovery. Only an explicit worker busy rejection (no claim was accepted)
may be submitted later, using the originally journaled payload.
"""

from __future__ import annotations
from typing import Any


import hashlib
import json
import uuid
from pathlib import Path


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


def explicit_request_id(
    user_id: str,
    prompt: str,
    width: int,
    height: int,
    include_commander: bool,
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
    """
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
