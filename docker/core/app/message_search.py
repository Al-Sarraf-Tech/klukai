"""Read-only, owner-scoped search and source windows of original messages."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from psycopg import AsyncConnection
from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    StrictStr,
    field_validator,
    model_validator,
)

from .db import get_pool


def _iso_instant(value: Any) -> Any:
    # Pydantic also accepts numeric epoch timestamps by default; this HTTP
    # contract accepts timezone-aware ISO instants only.
    if isinstance(value, str):
        # Parse ISO syntax before AwareDatetime can treat numeric strings as
        # Unix epochs. The downstream validator still rejects naive instants.
        return datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise ValueError("Expected an ISO instant")
    return value


Instant = Annotated[AwareDatetime, BeforeValidator(_iso_instant)]


class Cursor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    created_at: Instant
    id: UUID


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: StrictStr
    speaker: Literal["all", "user", "assistant"] = "all"
    start: Instant | None = None
    end: Instant | None = None
    cursor: Cursor | None = None

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        value = value.strip()
        if not 2 <= len(value) <= 200 or "\0" in value:
            raise ValueError("Expected 2–200 characters without NUL")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> SearchRequest:
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("Start must precede end")
        return self


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchor_id: UUID
    before: Cursor | None = None
    after: Cursor | None = None

    @model_validator(mode="after")
    def validate_direction(self) -> ContextRequest:
        if self.before is not None and self.after is not None:
            raise ValueError("Choose one direction")
        return self


def make_excerpt(content: str, match_start: int) -> str:
    """Slice original code points around the database-computed match offset."""
    start = max(0, match_start - 80)
    end = min(len(content), start + 400)
    return (
        ("…" if start else "")
        + content[start:end]
        + ("…" if end < len(content) else "")
    )


def _cursor(row: tuple[Any, ...]) -> dict[str, str]:
    return {"created_at": row[3].isoformat(), "id": str(row[0])}


def _message(row: tuple[Any, ...]) -> dict[str, str]:
    return {
        "id": str(row[0]),
        "role": row[1],
        "content": row[2],
        "created_at": row[3].isoformat(),
    }


async def _read_transaction(conn: AsyncConnection[tuple[Any, ...]]) -> None:
    # Explicitly read-only, scoped to the surrounding transaction. Never leave
    # a changed timeout on a reused pool session, even after cancellation.
    await conn.execute("SET TRANSACTION READ ONLY")
    await conn.execute("SELECT set_config('statement_timeout', %s, true)", ("3000",))


async def search_messages(user_id: str, request: SearchRequest) -> dict[str, Any]:
    pattern = (
        "%"
        + request.query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        + "%"
    )
    conditions = [
        "user_id = %s",
        "role IN ('user', 'assistant')",
        "content ILIKE %s ESCAPE E'\\\\'",
    ]
    params: list[Any] = [user_id, pattern]
    if request.speaker != "all":
        conditions.append("role = %s")
        params.append(request.speaker)
    if request.start is not None:
        conditions.append("created_at >= %s")
        params.append(request.start)
    if request.end is not None:
        conditions.append("created_at < %s")
        params.append(request.end)
    if request.cursor is not None:
        conditions.append("(created_at, id) < (%s, %s)")
        params.extend((request.cursor.created_at, request.cursor.id))
    params.append(21)
    async with get_pool().connection() as conn, conn.transaction():
        await _read_transaction(conn)
        # Locate the match with PostgreSQL case semantics; Python casefold or
        # regex can pick different Unicode matches and shift source offsets.
        rows = await (
            await conn.execute(
                "SELECT id, role, content, created_at, "
                "strpos(lower(content), lower(%s)) - 1 FROM companion_messages WHERE "
                + " AND ".join(conditions)
                + " ORDER BY created_at DESC, id DESC LIMIT %s",
                [request.query, *params],
                # Common and selective phrases need different plans; keep bound
                # values while avoiding a cached generic prepared query plan.
                prepare=False,
            )
        ).fetchall()
    return {
        "messages": [
            {
                "id": str(row[0]),
                "role": row[1],
                "excerpt": make_excerpt(row[2], row[4]),
                "created_at": row[3].isoformat(),
            }
            for row in rows[:20]
        ],
        "next_cursor": _cursor(rows[19]) if len(rows) > 20 else None,
    }


async def _side(
    conn: AsyncConnection[tuple[Any, ...]],
    user_id: str,
    conversation_id: UUID,
    cursor: Cursor,
    *,
    older: bool,
    limit: int,
) -> list[tuple[Any, ...]]:
    comparison, order = ("<", "DESC") if older else (">", "ASC")
    return await (
        await conn.execute(
            "SELECT id, role, content, created_at FROM companion_messages "
            "WHERE user_id = %s AND conversation_id = %s AND role IN ('user', 'assistant') "
            f"AND (created_at, id) {comparison} (%s, %s) "
            f"ORDER BY created_at {order}, id {order} LIMIT %s",
            (user_id, conversation_id, cursor.created_at, cursor.id, limit),
            prepare=False,
        )
    ).fetchall()


async def _has_side(
    conn: AsyncConnection[tuple[Any, ...]],
    user_id: str,
    conversation_id: UUID,
    row: tuple[Any, ...],
    *,
    older: bool,
) -> bool:
    comparison = "<" if older else ">"
    found = await (
        await conn.execute(
            "SELECT EXISTS (SELECT 1 FROM companion_messages "
            "WHERE user_id = %s AND conversation_id = %s AND role IN ('user', 'assistant') "
            f"AND (created_at, id) {comparison} (%s, %s))",
            (user_id, conversation_id, row[3], row[0]),
            prepare=False,
        )
    ).fetchone()
    assert found is not None
    return bool(found[0])


async def get_message_context(
    user_id: str, request: ContextRequest
) -> dict[str, Any] | None:
    async with get_pool().connection() as conn, conn.transaction():
        await _read_transaction(conn)
        anchor = await (
            await conn.execute(
                "SELECT id, role, content, created_at, conversation_id FROM companion_messages "
                "WHERE user_id = %s AND id = %s AND role IN ('user', 'assistant')",
                (user_id, request.anchor_id),
            )
        ).fetchone()
        if anchor is None:
            return None
        conversation_id = anchor[4]
        if request.before is None and request.after is None:
            center = Cursor(created_at=anchor[3], id=anchor[0])
            left = await _side(
                conn, user_id, conversation_id, center, older=True, limit=11
            )
            right = await _side(
                conn, user_id, conversation_id, center, older=False, limit=11
            )
            rows = list(reversed(left[:10])) + [anchor] + right[:10]
            older, newer = len(left) > 10, len(right) > 10
        else:
            is_older = request.before is not None
            edge = request.before if is_older else request.after
            assert edge is not None
            fetched = await _side(
                conn, user_id, conversation_id, edge, older=is_older, limit=21
            )
            rows = list(reversed(fetched[:20])) if is_older else fetched[:20]
            older = newer = False
            if rows:
                if is_older:
                    older = len(fetched) > 20
                    newer = await _has_side(
                        conn, user_id, conversation_id, rows[-1], older=False
                    )
                else:
                    older = await _has_side(
                        conn, user_id, conversation_id, rows[0], older=True
                    )
                    newer = len(fetched) > 20
        return {
            "anchor_id": str(request.anchor_id),
            "messages": [_message(row) for row in rows],
            "older_cursor": _cursor(rows[0]) if older else None,
            "newer_cursor": _cursor(rows[-1]) if newer else None,
        }
