#!/usr/bin/env python3
"""Copy Klukai's memories out of Qdrant into PostgreSQL, once.

Owner decision 2026-10-26: Klukai runs on cloud AI only, so memory recall now
reads PostgreSQL (full-text candidates, then Claude Haiku picks; see
docs/superpowers/specs/2026-10-26-cloud-only-ai-design.md). Until then most
exchanges, and many episodes, lived only as Qdrant point payloads. This copies
those payloads into ``companion_exchanges`` and ``companion_episodes`` so no
memory stops being recallable. No embedding model is involved: it is a plain
data copy.

Chat memories are sacred: this is additive and idempotent. Qdrant is only
read (scroll, payloads, no vectors); a PostgreSQL row is only ever inserted,
never updated or deleted, and a row that already exists (same id, or an
episode whose ``embedding_id`` is the point id) is left alone. Original
timestamps are kept so recency ranking stays right. Memory text is never
printed. Dry run unless ``--apply``.

Run inside the core container, which has the database and the aichat network:

    docker exec -i companion-core python - --apply \\
        < ops/amarillo/scripts/klukai-memory-backfill-from-qdrant.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

DEFAULT_QDRANT = "http://aichat-vector:6333"
PAGE = 256
DEFAULT_USER = "jalsarraf"

EXCHANGE_SQL = (
    "INSERT INTO companion_exchanges "
    "(id, conversation_id, user_content, assistant_content, topics, mood, "
    "importance, user_id, created_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, NOW())) "
    "ON CONFLICT (id) DO NOTHING"
)
EPISODE_SQL = (
    "INSERT INTO companion_episodes "
    "(id, conversation_id, summary, keywords, emotion_tags, importance, "
    "embedding_id, user_id, created_at) "
    "SELECT %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, NOW()) "
    "WHERE NOT EXISTS (SELECT 1 FROM companion_episodes WHERE embedding_id = %s) "
    "ON CONFLICT (id) DO NOTHING"
)

Fetch = Callable[[str, dict[str, Any]], dict[str, Any]]


@dataclass
class Report:
    read: int = 0
    invalid: int = 0
    inserted: int = 0
    existing: int = 0
    users: dict[str, int] = field(default_factory=dict)


def _uuid(value: object) -> str | None:
    if not isinstance(value, str | uuid.UUID):
        return None
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _texts(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _importance(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.5
    return min(max(float(value), 0.0), 1.0)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    # The legacy core used datetime.now().isoformat() in its UTC container.
    # Its offset-free values are valid historical dates, not missing dates.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _user(value: object) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else DEFAULT_USER


def exchange_row(point: dict[str, Any]) -> tuple[Any, ...] | None:
    """The companion_exchanges row for one Qdrant point, or None if unusable."""
    point_id = _uuid(point.get("id"))
    payload = point.get("payload")
    if point_id is None or not isinstance(payload, dict):
        return None
    user_content = _text(payload.get("user_content"))
    assistant_content = _text(payload.get("assistant_content"))
    if user_content is None or assistant_content is None:
        return None
    mood = _text(payload.get("mood")) or "composed"
    return (
        point_id,
        _uuid(payload.get("conversation_id")),
        user_content,
        assistant_content,
        _texts(payload.get("topics")),
        mood,
        _importance(payload.get("importance")),
        _user(payload.get("user_id")),
        _timestamp(payload.get("created_at")),
    )


def episode_row(point: dict[str, Any]) -> tuple[Any, ...] | None:
    """The companion_episodes row (plus the dedupe key) for one point."""
    point_id = _uuid(point.get("id"))
    payload = point.get("payload")
    if point_id is None or not isinstance(payload, dict):
        return None
    summary = _text(payload.get("summary"))
    if summary is None:
        return None
    return (
        point_id,
        _uuid(payload.get("conversation_id")),
        summary,
        _texts(payload.get("keywords")),
        _texts(payload.get("emotion_tags")),
        _importance(payload.get("importance")),
        point_id,  # embedding_id: provenance, and the rerun dedupe key
        _user(payload.get("user_id")),
        _timestamp(payload.get("created_at")),
        point_id,
    )


def http_fetch(url: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        data = json.load(response)
    if not isinstance(data, dict):
        raise ValueError("Qdrant answered with a non-object")
    return data


def points(qdrant: str, collection: str, fetch: Fetch) -> Iterator[dict[str, Any]]:
    """Every point's id and payload, page by page (vectors are never read)."""
    url = f"{qdrant.rstrip('/')}/collections/{collection}/points/scroll"
    offset: object = None
    while True:
        body: dict[str, Any] = {
            "limit": PAGE,
            "with_payload": True,
            "with_vector": False,
        }
        if offset is not None:
            body["offset"] = offset
        result = fetch(url, body).get("result")
        if not isinstance(result, dict):
            raise ValueError(f"Malformed scroll reply for {collection}")
        page = result.get("points")
        if not isinstance(page, list):
            raise ValueError(f"Malformed scroll page for {collection}")
        yield from (point for point in page if isinstance(point, dict))
        offset = result.get("next_page_offset")
        if offset is None:
            return


def copy(
    rows: Iterator[tuple[Any, ...] | None],
    sql: str,
    cursor: Any | None,
    user_index: int,
) -> Report:
    """Insert each usable row; count what happened (no text is kept)."""
    report = Report()
    for row in rows:
        report.read += 1
        if row is None:
            report.invalid += 1
            continue
        user = row[user_index]
        report.users[user] = report.users.get(user, 0) + 1
        if cursor is None:
            continue
        cursor.execute(sql, row)
        if cursor.rowcount == 1:
            report.inserted += 1
        else:
            report.existing += 1
    return report


def main(argv: list[str] | None = None, fetch: Fetch = http_fetch) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--qdrant", default=DEFAULT_QDRANT)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = parser.parse_args(argv)

    jobs = (
        ("companion_exchanges", exchange_row, EXCHANGE_SQL, 7),
        ("companion_episodes", episode_row, EPISODE_SQL, 7),
    )
    if not args.apply:
        for collection, to_row, sql, user_index in jobs:
            rows = (to_row(p) for p in points(args.qdrant, collection, fetch))
            _print(collection, copy(rows, sql, None, user_index), dry_run=True)
        return 0
    if not args.database_url:
        print("DATABASE_URL is required with --apply", file=sys.stderr)
        return 2

    import psycopg

    with psycopg.connect(args.database_url) as conn:
        for collection, to_row, sql, user_index in jobs:
            rows = (to_row(p) for p in points(args.qdrant, collection, fetch))
            with conn.transaction(), conn.cursor() as cursor:
                report = copy(rows, sql, cursor, user_index)
            _print(collection, report, dry_run=False)
    return 0


def _print(collection: str, report: Report, *, dry_run: bool) -> None:
    mode = "dry run" if dry_run else "applied"
    users = ", ".join(f"{user}={count}" for user, count in sorted(report.users.items()))
    print(
        f"{collection} ({mode}): read={report.read} invalid={report.invalid} "
        f"inserted={report.inserted} already_present={report.existing} "
        f"users[{users}]"
    )


if __name__ == "__main__":
    sys.exit(main())
