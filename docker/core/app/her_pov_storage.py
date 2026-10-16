"""Durable Her POV board storage; strictness is supplied by the coordinator."""

from __future__ import annotations

import logging
from typing import Any
import uuid

logger = logging.getLogger(__name__)


async def _load_job_from_db(job_id: str) -> dict[str, Any] | None:
    try:
        uuid.UUID(str(job_id))
    except (ValueError, AttributeError, TypeError):
        return None
    try:
        from .db import get_pool

        get_pool()  # raises if not initialized
    except Exception:
        return None
    try:
        from .db import get_conn

        async with get_conn() as conn:
            row = await (
                await conn.execute(
                    "SELECT id, user_id, status, phase, message, error, title, "
                    "annotation, mood, memory_id, has_image, exchange_preview, "
                    "created_at, updated_at "
                    "FROM companion_her_pov_jobs WHERE id = %s",
                    (job_id,),
                )
            ).fetchone()
        if not row:
            return None
        preview = row[11]
        if isinstance(preview, str):
            import json as _json

            try:
                preview = _json.loads(preview)
            except Exception:
                pass

        def _iso(v: Any) -> Any:
            return v.isoformat() if v is not None and hasattr(v, "isoformat") else v

        return {
            "id": str(row[0]),
            "user_id": row[1],
            "status": row[2],
            "phase": row[3],
            "message": row[4],
            "error": row[5],
            "title": row[6],
            "annotation": row[7],
            "mood": row[8],
            "memory_id": row[9],
            "has_image": bool(row[10]),
            "exchange_preview": preview,
            "created_at": _iso(row[12]),
            "updated_at": _iso(row[13]),
        }
    except Exception as e:
        logger.debug("her_pov load from db failed: %s", e)
        return None


async def _persist_job(job_id: str, snapshot: dict[str, Any], *, strict: bool) -> None:
    """Queue acceptance/progress must be durable; inline local work is best-effort."""
    try:
        from .db import get_pool

        get_pool()
    except Exception:
        if strict:
            raise RuntimeError("Memory durable state is unavailable") from None
        return
    try:
        import json as _json
        from .db import get_conn_autocommit

        status = snapshot.get("status") or "queued"
        phase = snapshot.get("phase") or status
        terminal = status in ("done", "failed")
        preview = snapshot.get("exchange_preview")
        preview_json = _json.dumps(preview) if preview is not None else None
        async with get_conn_autocommit() as conn:
            await conn.execute(
                "INSERT INTO companion_her_pov_jobs "
                "(id, user_id, status, phase, message, error, title, annotation, "
                " mood, memory_id, has_image, exchange_preview, created_at, updated_at, "
                " finished_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, "
                "        COALESCE(%s::timestamptz, NOW()), NOW(), "
                "        CASE WHEN %s THEN NOW() ELSE NULL END) "
                "ON CONFLICT (id) DO UPDATE SET "
                " status = EXCLUDED.status, "
                " phase = EXCLUDED.phase, "
                " message = EXCLUDED.message, "
                " error = EXCLUDED.error, "
                " title = COALESCE(EXCLUDED.title, companion_her_pov_jobs.title), "
                " annotation = COALESCE(EXCLUDED.annotation, companion_her_pov_jobs.annotation), "
                " mood = COALESCE(EXCLUDED.mood, companion_her_pov_jobs.mood), "
                " memory_id = COALESCE(EXCLUDED.memory_id, companion_her_pov_jobs.memory_id), "
                " has_image = EXCLUDED.has_image OR companion_her_pov_jobs.has_image, "
                " exchange_preview = COALESCE(EXCLUDED.exchange_preview, "
                "                             companion_her_pov_jobs.exchange_preview), "
                " updated_at = NOW(), "
                " finished_at = CASE WHEN %s THEN COALESCE(companion_her_pov_jobs.finished_at, NOW()) "
                "                    ELSE companion_her_pov_jobs.finished_at END",
                (
                    job_id,
                    snapshot.get("user_id") or "",
                    status,
                    phase,
                    snapshot.get("message"),
                    snapshot.get("error"),
                    snapshot.get("title"),
                    snapshot.get("annotation"),
                    snapshot.get("mood"),
                    snapshot.get("memory_id"),
                    bool(snapshot.get("has_image")),
                    preview_json,
                    snapshot.get("created_at"),
                    terminal,
                    terminal,
                ),
            )
    except Exception as e:
        if strict:
            raise RuntimeError("Memory durable state could not be saved") from None
        logger.debug("her_pov persist failed: %s", e)
