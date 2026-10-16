"""Deterministic SQL fixtures; connections only target the owned test stack."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
import uuid

import psycopg
from psycopg import sql
import redis.asyncio as redis

SEED_VERSION = "klukai-perf-v1-100-messages-20-archives-two-owners"
OWNER = "jalsarraf"
OTHER = "ricky"


def connection(stack: Any, database: str) -> str:
    return f"postgresql://perf_test:{stack.password}@{stack.backends['postgres']['ip']}:5432/{database}"


async def create_database(stack: Any, database: str) -> None:
    deadline = asyncio.get_running_loop().time() + 30
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(
                connection(stack, "postgres"), autocommit=True
            ) as conn:
                await conn.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
                )
            return
        except psycopg.OperationalError:
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(
                    "Disposable PostgreSQL protocol unavailable"
                ) from None
            await asyncio.sleep(0.1)


async def seed(stack: Any, database: str) -> None:
    async with await psycopg.AsyncConnection.connect(
        connection(stack, database)
    ) as conn:
        for owner in (OWNER, OTHER):
            conversation = uuid.uuid5(uuid.NAMESPACE_URL, f"perf:{owner}")
            await conn.execute(
                "INSERT INTO companion_conversations (id,user_id) VALUES (%s,%s)",
                (conversation, owner),
            )
            for index in range(100):
                await conn.execute(
                    "INSERT INTO companion_messages (id,conversation_id,role,content,user_id,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                    (
                        uuid.uuid5(conversation, str(index)),
                        conversation,
                        "user" if index % 2 else "assistant",
                        f"seed:{owner}:{index}",
                        owner,
                        datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index),
                    ),
                )
            for index in range(20):
                await conn.execute(
                    "INSERT INTO companion_memories (id,filename,annotation,scene_tags,kept,user_id,created_at) VALUES (%s,%s,%s,%s,true,%s,%s)",
                    (
                        uuid.uuid5(conversation, f"archive:{index}"),
                        f"fixture-{owner}-{index}.png",
                        f"seed:{owner}:archive:{index}",
                        ["fixture"],
                        owner,
                        datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index),
                    ),
                )


def redis_client(stack: Any) -> redis.Redis:
    return redis.Redis(
        host=stack.backends["redis"]["ip"], port=6379, db=1, decode_responses=True
    )


async def reset_rate_limits(client: redis.Redis) -> None:
    # Only this newly created stack's Redis is reachable through this interface.
    keys = [key async for key in client.scan_iter(match="ratelimit:*")]
    if keys:
        await client.delete(*keys)
