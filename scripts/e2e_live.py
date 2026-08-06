#!/usr/bin/env python3
"""Live end-to-end checks against a running companion-core.

Exercises the real stack — HTTP + WebSocket + Postgres + LM Studio + ComfyUI —
the way the Flutter client does, so a green run means Klukai actually works,
not just that the unit suite passes.

SAFETY: runs as the ``claude`` test user by default. jalsarraf's chat history,
episodes and affection are SACRED — never point this at that account. The
script refuses to run against jalsarraf unless --i-know-what-im-doing is given.

Usage:
    python3 scripts/e2e_live.py                  # all checks
    python3 scripts/e2e_live.py --only chat      # one check
    python3 scripts/e2e_live.py --list

Exit code is non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from typing import Any, Callable, Awaitable

BASE_URL = os.environ.get("KLUKAI_E2E_BASE", "http://localhost:8300")
PG_CONTAINER = os.environ.get("KLUKAI_E2E_PG", "infra-postgres")
PG_USER = os.environ.get("KLUKAI_E2E_PG_USER", "aichat")
PG_DB = os.environ.get("KLUKAI_E2E_PG_DB", "aichat")

# Her POV runs an LLM compose + a full image generation on a remote GPU.
HER_POV_TIMEOUT = int(os.environ.get("KLUKAI_E2E_HER_POV_TIMEOUT", "420"))
CHAT_TIMEOUT = int(os.environ.get("KLUKAI_E2E_CHAT_TIMEOUT", "240"))


# ── tiny result plumbing ────────────────────────────────────────────────────

class CheckFailed(Exception):
    """A check assertion failed (as opposed to the harness breaking)."""


def _log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def _psql(sql: str) -> str:
    """Run a query via docker exec and return raw tuple-only output."""
    out = subprocess.run(
        ["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", PG_DB, "-tAc", sql],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(f"psql failed: {out.stderr.strip()}")
    return out.stdout.strip()


# ── auth ────────────────────────────────────────────────────────────────────

def mint_token(username: str) -> str:
    """Create a short-lived session row for `username` and return the bearer.

    Mirrors app.auth: the DB stores sha256(token); the client holds the
    plaintext. Minting directly avoids needing the seed password.
    """
    user_id = _psql(f"SELECT id FROM companion_users WHERE username = '{username}'")
    if not user_id:
        raise RuntimeError(f"no such user: {username}")
    token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    _psql(
        "INSERT INTO companion_auth_sessions (token, user_id, expires_at) "
        f"VALUES ('{token_hash}', '{user_id}', NOW() + INTERVAL '1 hour')"
    )
    return token


def revoke_token(token: str) -> None:
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    try:
        _psql(f"DELETE FROM companion_auth_sessions WHERE token = '{token_hash}'")
    except Exception:  # cleanup is best-effort
        pass


# ── checks ──────────────────────────────────────────────────────────────────

async def check_health(ctx: dict[str, Any]) -> str:
    """/health reports every dependency as ok."""
    import aiohttp

    async with aiohttp.ClientSession() as s:
        async with s.get(f"{BASE_URL}/health", timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                raise CheckFailed(f"/health returned {r.status}")
            body = await r.json()

    if body.get("status") != "ok":
        raise CheckFailed(f"status={body.get('status')}")
    for dep in ("redis", "qdrant"):
        if body.get(dep) != "ok":
            raise CheckFailed(f"{dep}={body.get(dep)}")
    if (body.get("database") or {}).get("status") != "ok":
        raise CheckFailed(f"database={body.get('database')}")
    return f"version={body.get('version')} deps ok"


async def check_auth_required(ctx: dict[str, Any]) -> str:
    """Protected endpoints reject an anonymous caller."""
    import aiohttp

    protected = [
        ("GET", "/api/memories"),
        ("POST", "/api/memories/her-pov"),
        ("GET", "/api/user/stats"),
    ]
    async with aiohttp.ClientSession() as s:
        for method, path in protected:
            async with s.request(
                method, f"{BASE_URL}{path}", timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                if r.status not in (401, 403):
                    raise CheckFailed(f"{method} {path} allowed anonymous access ({r.status})")
    return f"{len(protected)} endpoints reject anonymous"


async def check_bad_token_rejected(ctx: dict[str, Any]) -> str:
    """A forged bearer token does not authenticate."""
    import aiohttp

    headers = {"Authorization": f"Bearer {secrets.token_urlsafe(48)}"}
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.get(
            f"{BASE_URL}/api/user/stats", timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status not in (401, 403):
                raise CheckFailed(f"forged token accepted ({r.status})")
    return "forged bearer rejected"


async def check_authed_read(ctx: dict[str, Any]) -> str:
    """The minted session token authenticates real reads."""
    import aiohttp

    async with aiohttp.ClientSession(headers=ctx["headers"]) as s:
        async with s.get(
            f"{BASE_URL}/api/user/stats", timeout=aiohttp.ClientTimeout(total=20)
        ) as r:
            if r.status != 200:
                raise CheckFailed(f"/api/user/stats returned {r.status}")
        async with s.get(
            f"{BASE_URL}/api/memories", timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            if r.status != 200:
                raise CheckFailed(f"/api/memories returned {r.status}")
            mems = await r.json()

    items = mems if isinstance(mems, list) else mems.get("memories", mems.get("items", []))
    ctx["memory_count_before"] = len(items)
    return f"stats ok, {len(items)} archive memories visible"


async def check_chat_roundtrip(ctx: dict[str, Any]) -> str:
    """Full chat round-trip over the WebSocket, and it persists to history.

    This is the path that has broken silently before (physical_state tz crash),
    so it asserts a real assistant reply — not just that the socket opened.
    """
    import aiohttp

    # The socket authenticates by query param, not by header (see chat.py).
    ws_url = (
        BASE_URL.replace("http://", "ws://").replace("https://", "wss://")
        + f"/ws?token={ctx['token']}"
    )
    probe = f"E2E probe {int(time.time())} — just say hello briefly."
    reply_parts: list[str] = []
    saw_error: str | None = None
    saw_done = False

    user = ctx["username"]
    before = int(
        _psql(
            f"SELECT COUNT(*) FROM companion_messages WHERE user_id = '{user}'"
        )
        or 0
    )

    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(ws_url, timeout=30, heartbeat=30) as ws:
            await ws.send_json({"type": "message", "content": probe})
            deadline = time.monotonic() + CHAT_TIMEOUT
            while time.monotonic() < deadline:
                try:
                    msg = await ws.receive(timeout=deadline - time.monotonic())
                except asyncio.TimeoutError:
                    break
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
                    continue
                try:
                    frame = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                ftype = frame.get("type")
                if ftype == "error":
                    saw_error = str(frame.get("message") or frame.get("error"))
                    break
                if ftype == "token":
                    reply_parts.append(str(frame.get("text") or ""))
                elif ftype == "done":
                    saw_done = True
                    ctx["chat_model"] = frame.get("model")
                    break

    if saw_error:
        raise CheckFailed(f"server sent error frame: {saw_error}")
    reply = "".join(reply_parts).strip()
    if not reply:
        raise CheckFailed(f"no assistant reply within {CHAT_TIMEOUT}s")
    if not saw_done:
        raise CheckFailed("stream never sent a 'done' frame")

    # It must land in history, not just stream to the socket.
    after = int(
        _psql(
            f"SELECT COUNT(*) FROM companion_messages WHERE user_id = '{user}'"
        )
        or 0
    )
    if after - before < 2:
        raise CheckFailed(
            f"expected user+assistant rows persisted, history grew by {after - before}"
        )

    ctx["chat_reply"] = reply
    return (
        f"replied {len(reply)} chars via {ctx.get('chat_model')}, "
        f"+{after - before} history rows"
    )


async def check_her_pov(ctx: dict[str, Any]) -> str:
    """Her POV end to end: pick a moment -> journal -> draw -> save to archive.

    This is the feature that failed in production on the affection import, so
    it asserts the job reaches `done` with a real image, not merely 202.
    """
    import aiohttp

    async with aiohttp.ClientSession(headers=ctx["headers"]) as s:
        async with s.post(
            f"{BASE_URL}/api/memories/her-pov", timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            if r.status != 202:
                raise CheckFailed(f"start returned {r.status}: {(await r.text())[:200]}")
            job = await r.json()
        job_id = job.get("job_id") or job.get("id")
        if not job_id:
            raise CheckFailed(f"no job_id in start response: {job}")
        ctx["her_pov_job_id"] = job_id

        phases: list[str] = []
        deadline = time.monotonic() + HER_POV_TIMEOUT
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            async with s.get(
                f"{BASE_URL}/api/memories/her-pov/{job_id}",
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                if r.status != 200:
                    raise CheckFailed(f"poll returned {r.status}")
                last = await r.json()
            phase = str(last.get("phase") or last.get("status") or "")
            if phase and (not phases or phases[-1] != phase):
                phases.append(phase)
                _log(f"phase: {phase}")
            if last.get("status") in ("done", "failed"):
                break

        if last.get("status") != "done":
            raise CheckFailed(
                f"job ended status={last.get('status')} error={last.get('error')} "
                f"phases={phases}"
            )
        if not last.get("memory_id"):
            raise CheckFailed(f"done but no memory_id: {last}")
        if not last.get("title"):
            raise CheckFailed("done but she wrote no title")

    ctx["her_pov_memory_id"] = last["memory_id"]
    return f"'{last['title']}' via {'->'.join(phases)} (memory {last['memory_id']})"


async def check_her_pov_idor(ctx: dict[str, Any]) -> str:
    """One user cannot poll another user's Her POV job."""
    import aiohttp

    # Reuse the job the her-pov check already ran — starting another would burn
    # a second GPU image generation for nothing.
    job_id = ctx.get("her_pov_job_id")
    if not job_id:
        raise CheckFailed("no her-pov job to probe (run the her-pov check first)")

    other = ctx["other_username"]
    other_token = mint_token(other)
    try:
        headers = {"Authorization": f"Bearer {other_token}"}
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.get(
                f"{BASE_URL}/api/memories/her-pov/{job_id}",
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                if r.status != 404:
                    raise CheckFailed(
                        f"user '{other}' could read another user's job ({r.status})"
                    )
    finally:
        revoke_token(other_token)
    return f"cross-user job read blocked (404) for '{other}'"


async def check_archive_grew(ctx: dict[str, Any]) -> str:
    """The Her POV image is really in the archive and tagged from her side."""
    if not ctx.get("her_pov_memory_id"):
        raise CheckFailed("no her-pov memory to verify (run the her-pov check first)")

    mem_id = str(ctx["her_pov_memory_id"]).replace("'", "")
    row = _psql(
        "SELECT scene_tags::text || '|' || COALESCE(user_id,'') || '|' || filename "
        f"FROM companion_memories WHERE id::text = '{mem_id}'"
    )
    if not row:
        raise CheckFailed(f"memory {mem_id} not found in companion_memories")
    tags, owner, filename = row.rsplit("|", 2)
    if "her_pov" not in tags:
        raise CheckFailed(f"memory {mem_id} missing her_pov tag: {tags[:120]}")
    if owner != ctx["username"]:
        raise CheckFailed(f"memory saved under wrong user: {owner!r}")

    # The row is only real if the image file actually landed on the volume.
    probe = subprocess.run(
        ["docker", "exec", "companion-core", "sh", "-c",
         f"ls -l /images/{filename} 2>&1"],
        capture_output=True, text=True, timeout=30,
    )
    if probe.returncode != 0 or "No such file" in probe.stdout:
        raise CheckFailed(f"image file missing on volume: {probe.stdout.strip()}")
    size = probe.stdout.split()[4] if len(probe.stdout.split()) > 4 else "?"
    return f"tagged {tags[:60]}, file {filename} ({size} bytes)"


async def check_anniversary_sweep(ctx: dict[str, Any]) -> str:
    """The daily anniversary sweep runs without the date/tzinfo crash.

    companion_firsts.event_date is a SQL DATE, so psycopg returns
    `datetime.date` — which used to blow up on `.tzinfo`.
    """
    code = (
        "import asyncio, sys; sys.path.insert(0,'/app');\n"
        "from app.character_behaviors import select_anniversary_from_firsts;\n"
        "from app.db import get_pool, init_pool;\n"
        "async def main():\n"
        "    await init_pool(min_size=1, max_size=2)\n"
        "    pool = get_pool()\n"
        "    async with pool.connection() as conn:\n"
        "        rows = await (await conn.execute("
        "'SELECT event_type, event_date, metadata FROM companion_firsts')).fetchall()\n"
        "    firsts = [{'event_type': r[0], 'event_date': r[1], 'metadata': r[2]} for r in rows]\n"
        "    pick = select_anniversary_from_firsts(firsts)\n"
        "    print('OK rows=%d pick=%s' % (len(firsts), pick))\n"
        "asyncio.run(main())\n"
    )
    out = subprocess.run(
        ["docker", "exec", "companion-core", "python3", "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if out.returncode != 0 or "OK rows=" not in out.stdout:
        raise CheckFailed(
            f"sweep raised: {(out.stderr or out.stdout).strip().splitlines()[-1:]}"
        )
    return out.stdout.strip().splitlines()[-1]


async def check_deferred_rail(ctx: dict[str, Any]) -> str:
    """A deferred task fires through the RabbitMQ delay rail, on time.

    Proves the whole loop: core persists to Postgres -> publishes `defer.arm`
    to Redis -> the bridge parks it in a fixed-TTL bucket queue -> the TTL
    expires and dead-letters onto the due queue -> the bridge calls core back
    -> core claims the row and dispatches it.

    Asserts the *timer* did the work, not the once-a-minute sweeper: a task
    delivered within the bucket's own delay could not have come from a sweep.
    """
    delay = 10
    code = (
        "import asyncio, sys; sys.path.insert(0,'/app')\n"
        "async def main():\n"
        "    from app.db import init_pool\n"
        "    from app import events, deferred\n"
        "    await init_pool(min_size=1, max_size=2)\n"
        "    await events.init()\n"
        "    tid = await deferred.schedule(\n"
        "        {'kind': 'message', 'text': 'e2e deferred rail probe'},\n"
        f"        user_id='{ctx['username']}', delay_seconds={delay})\n"
        "    print('TASK', tid)\n"
        "    await asyncio.sleep(1)\n"
        "asyncio.run(main())\n"
    )
    out = subprocess.run(
        ["docker", "exec", "companion-core", "python3", "-c", code],
        capture_output=True, text=True, timeout=90,
    )
    task_id = ""
    for line in out.stdout.splitlines():
        if line.startswith("TASK "):
            task_id = line.split(None, 1)[1].strip()
    if not task_id or task_id == "None":
        raise CheckFailed(f"could not schedule: {(out.stderr or out.stdout)[-200:]}")

    deadline = time.monotonic() + delay + 25
    status = "pending"
    while time.monotonic() < deadline:
        await asyncio.sleep(2)
        status = _psql(
            f"SELECT status FROM companion_scheduled WHERE id = '{task_id}'"
        )
        if status and status != "pending":
            break

    if status != "delivered":
        raise CheckFailed(f"task ended status={status!r} (expected delivered)")

    late = _psql(
        "SELECT EXTRACT(EPOCH FROM (delivered_at - due_at))::numeric(8,2) "
        f"FROM companion_scheduled WHERE id = '{task_id}'"
    )
    lateness = float(late or 999)
    # The sweeper runs on a 60s interval, so anything this prompt came from the
    # rail. A generous ceiling still excludes a sweep.
    if lateness > 30:
        raise CheckFailed(
            f"delivered {lateness}s late — the sweeper caught it, not the timer"
        )
    _psql(f"DELETE FROM companion_scheduled WHERE id = '{task_id}'")
    return f"fired {lateness}s after due via the {delay}s bucket"


async def check_cron_bookkeeping(ctx: dict[str, Any]) -> str:
    """Recurring jobs record when they ran, so a restart can replay misses."""
    rows = _psql(
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE last_fired > NOW() - INTERVAL '2 days') "
        "FROM companion_job_runs"
    )
    if not rows:
        raise CheckFailed("companion_job_runs is empty — nothing is recording runs")
    total, recent = (rows.split("|") + ["0"])[:2]
    if int(total or 0) == 0:
        raise CheckFailed("no job runs recorded")
    return f"{total} job(s) tracked, {recent} fired in the last 2 days"


CHECKS: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
    "health": check_health,
    "auth-required": check_auth_required,
    "bad-token": check_bad_token_rejected,
    "authed-read": check_authed_read,
    "anniversary": check_anniversary_sweep,
    "cron-bookkeeping": check_cron_bookkeeping,
    "deferred": check_deferred_rail,
    "chat": check_chat_roundtrip,
    "her-pov": check_her_pov,
    "her-pov-idor": check_her_pov_idor,
    "archive": check_archive_grew,
}


async def run(names: list[str], username: str, other_username: str) -> int:
    token = mint_token(username)
    ctx: dict[str, Any] = {
        "username": username,
        "other_username": other_username,
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
    }
    failures: list[str] = []
    skipped: list[str] = []
    try:
        for name in names:
            print(f"\n▸ {name}", flush=True)
            started = time.monotonic()
            try:
                detail = await CHECKS[name](ctx)
            except CheckFailed as e:
                failures.append(name)
                print(f"  ✗ FAIL ({time.monotonic() - started:.1f}s): {e}", flush=True)
            except Exception as e:  # harness/infra problem, not an assertion
                failures.append(name)
                print(
                    f"  ✗ ERROR ({time.monotonic() - started:.1f}s): "
                    f"{type(e).__name__}: {e}",
                    flush=True,
                )
            else:
                print(f"  ✓ {detail}  ({time.monotonic() - started:.1f}s)", flush=True)
    finally:
        revoke_token(token)

    print("\n" + "─" * 60)
    passed = len(names) - len(failures) - len(skipped)
    print(f"E2E: {passed}/{len(names)} passed as user '{username}'")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--user", default="claude", help="test user (default: claude)")
    ap.add_argument(
        "--other-user", default="ricky", help="second user for cross-user checks"
    )
    ap.add_argument("--only", action="append", help="run only these checks")
    ap.add_argument("--skip", action="append", default=[], help="skip these checks")
    ap.add_argument("--list", action="store_true", help="list check names and exit")
    ap.add_argument(
        "--i-know-what-im-doing",
        action="store_true",
        help="permit running against jalsarraf (SACRED data) — don't",
    )
    args = ap.parse_args()

    if args.list:
        for name in CHECKS:
            print(name)
        return 0

    if args.user == "jalsarraf" and not args.i_know_what_im_doing:
        print(
            "refusing to run against 'jalsarraf' — that account's chat history, "
            "episodes and affection are SACRED. Use --user claude.",
            file=sys.stderr,
        )
        return 2

    names = args.only or list(CHECKS)
    unknown = [n for n in names if n not in CHECKS]
    if unknown:
        print(f"unknown check(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    names = [n for n in names if n not in args.skip]

    return asyncio.run(run(names, args.user, args.other_user))


if __name__ == "__main__":
    sys.exit(main())
