"""Split route handlers — group 2 from app/routes.py (S+ Phase 2 §6.1)."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .auth import is_admin
from .context import ws, affection
from .db import get_pool

from .routes import (
    TributeRequest,
)

logger = logging.getLogger(__name__)


async def _get_user_id(request: Request) -> str | None:
    """Local mirror of routes.py:_get_user_id."""
    from .auth import get_user_from_token
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return await get_user_from_token(auth[7:])


def register_extras2(app: FastAPI) -> None:
    """Register group-2 HTTP endpoints."""
    @app.post("/api/tribute")
    async def api_tribute(req: TributeRequest, request: Request):
        """Commander honors Klukai with a heartfelt message.

        Persists as a sacred tribute (never deleted). Bumps affection +20.
        Pushes an elevated-mood proactive response. Optionally promotes to
        crown jewel (always referenced in system prompt at affection 4+).

        Cooldown: 24h between tributes per user.
        """
        from . import error_codes as ec
        from . import tributes
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()

        # 24h cooldown pre-check (advisory UX; the authoritative guard is the
        # atomic WHERE NOT EXISTS inside save_tribute). FAIL CLOSED: if the
        # cooldown state is unknown (DB error → None), reject instead of
        # granting repeatable +20 affection.
        recent = await tributes.count_recent(user_id)
        if recent is None:
            return ec.err(ec.INTERNAL_ERROR,
                          "Tribute cooldown check unavailable — try again later",
                          status_code=503)
        allowed, reason = tributes.can_send_tribute(recent)
        if not allowed:
            return ec.err(ec.INPUT_INVALID, reason or "Cooldown active",
                          status_code=429,
                          extra={"cooldown_hours": tributes.TRIBUTE_COOLDOWN_HOURS})

        # Capture state at write-time
        aff_state = await affection.get_state(user_id)

        try:
            tribute_id = await tributes.save_tribute(
                user_id=user_id,
                text=req.text,
                mood_at_time="grateful",
                affection_at_time=aff_state.score,
                make_crown_jewel=req.make_crown_jewel,
            )
        except tributes.TributeCooldownActive:
            # Lost the race to a concurrent tribute inside the window —
            # the atomic guard blocked the insert. No affection granted.
            return ec.err(ec.INPUT_INVALID,
                          f"Tributes are sacred — please wait "
                          f"{tributes.TRIBUTE_COOLDOWN_HOURS}h between them",
                          status_code=429,
                          extra={"cooldown_hours": tributes.TRIBUTE_COOLDOWN_HOURS})
        if not tribute_id:
            return ec.err(ec.INTERNAL_ERROR, "Tribute could not be saved", status_code=500)

        # Bump affection via add_score: it clamps, recomputes the level from the
        # new score, and writes the affection-log row (so the tribute shows on the
        # journey graph). Manual score+_save_state left the level stale + unlogged.
        aff_state = await affection.add_score(tributes.TRIBUTE_AFFECTION_BUMP, user_id)

        # Push elevated-mood response via WS if Commander is connected
        if ws.is_connected(user_id):
            # Klukai's mood lifts to "grateful" — the elevated-vulnerable mood
            # most appropriate for receiving a tribute. The actual response
            # the LLM generates on next turn will reflect this mood.
            try:
                await ws.send_proactive(
                    user_id,
                    "...Commander. (I take a moment, looking down, then back at you.) I... thank you.",
                )
                await ws.send_affection(
                    user_id, aff_state.score, aff_state.level, aff_state.level_name,
                    tributes.TRIBUTE_AFFECTION_BUMP,
                )
            except Exception:
                pass  # Don't fail the tribute write on WS issues

        # Audit the tribute — a sacred record
        try:
            from . import audit
            ip = request.client.host if request.client else None
            await audit.log(
                "tribute_given",
                user_id=user_id,
                ip_address=ip,
                request_id=getattr(request.state, "request_id", None),
                metadata={
                    "tribute_id": tribute_id,
                    "text_length": len(req.text),
                    "is_crown_jewel": req.make_crown_jewel,
                    "affection_bump": tributes.TRIBUTE_AFFECTION_BUMP,
                    "new_score": aff_state.score,
                },
            )
        except Exception:
            pass

        return {
            "ok": True,
            "tribute_id": tribute_id,
            "is_crown_jewel": req.make_crown_jewel,
            "affection_bump": tributes.TRIBUTE_AFFECTION_BUMP,
            "new_score": aff_state.score,
            "mood_shift": "grateful",
        }

    @app.get("/api/tributes")
    async def api_list_tributes(request: Request, limit: int = 20):
        """List the Commander's tributes, newest first."""
        from . import error_codes as ec
        from . import tributes
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        limit = max(1, min(limit, 100))
        items = await tributes.list_tributes(user_id, limit=limit)
        return {"count": len(items), "tributes": items}

    @app.get("/api/tribute/crown")
    async def api_get_crown_jewel(request: Request):
        """Return the current crown-jewel tribute (or null if none set)."""
        from . import error_codes as ec
        from . import tributes
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        crown = await tributes.get_crown_jewel(user_id)
        return {"crown_jewel": crown}

    @app.post("/api/tributes/{tribute_id}/crown")
    async def api_set_crown_jewel(tribute_id: str, request: Request):
        """Promote a tribute to crown jewel (demotes any prior one)."""
        from . import error_codes as ec
        from . import tributes
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        ok = await tributes.set_crown_jewel(user_id, tribute_id)
        if not ok:
            return ec.err(ec.INPUT_INVALID, "Tribute not found", status_code=404)
        return {"ok": True, "crown_jewel_id": tribute_id}

    # ── Dream diary (text-only memories from reflection-on-return) ──────────

    @app.get("/api/dreams")
    async def api_dreams(request: Request, limit: int = 20):
        """List the user's saved dreams (reflection-on-return 'dream' path).

        Dreams are memory-archive rows with category='Dreams', text-only by
        default (filename starts with 'dream-' sentinel). Returns newest-first.
        """
        from . import error_codes as ec
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        from . import dreams as dream_mod
        items = await dream_mod.list_dreams(user_id=user_id, limit=limit)
        total = await dream_mod.count_dreams(user_id=user_id)
        return {"count": len(items), "total": total, "dreams": items}

    # ── Affection timeline (Your Journey graph data) ────────────────────────

    @app.get("/api/user/affection-timeline")
    async def api_affection_timeline(request: Request, days: int = 30):
        """Return the authenticated user's affection-score timeline over N days.

        Pulls from companion_affection_log, bucketed by day. Useful for
        graphing relationship progression in the Flutter UI.
        """
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        days = max(1, min(days, 365))
        try:
            pool = get_pool()
            async with pool.connection() as conn:
                rows = await (await conn.execute(
                    "SELECT DATE(created_at) AS day, "
                    "  MAX(new_score) AS end_score, "
                    "  SUM(delta) AS net_delta, "
                    "  COUNT(*) AS events "
                    "FROM companion_affection_log "
                    "WHERE user_id = %s AND created_at > NOW() - make_interval(days => %s) "
                    "GROUP BY DATE(created_at) "
                    "ORDER BY day ASC",
                    (user_id, days),
                )).fetchall()
            points = [
                {
                    "date": r[0].isoformat() if r[0] else None,
                    "end_score": r[1],
                    "net_delta": r[2],
                    "events": r[3],
                }
                for r in rows
            ]
            return {"days": days, "count": len(points), "points": points}
        except Exception as e:
            logger.error("Affection timeline failed: %s", e)
            return JSONResponse({"error": "Timeline unavailable"}, status_code=500)

    # ── Audit chain integrity check (admin-only) ──────────────────────────

    @app.get("/api/audit/verify-chain")
    async def api_audit_verify_chain(request: Request, limit: int = 500):
        """Verify the HMAC hash chain over the MOST RECENT N audit rows.

        Admin-only. Fetches the newest N rows plus one extra anchor row
        (whose stored chain_hash seeds the verification) and replays the
        chain forward. Returns {valid, break_at_id, checked}. A break means
        a row was modified, deleted, or left without a chain hash since
        insert. For a full-chain audit, raise `limit` to cover the table.
        """
        from . import error_codes as ec
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        if not is_admin(user_id):
            return ec.admin_only()
        limit = max(1, min(limit, 5000))
        try:
            pool = get_pool()
            async with pool.connection() as conn:
                # Newest first; +1 row to use as the chain trust anchor.
                rows_raw = await (await conn.execute(
                    "SELECT id, event_type, user_id, ip_address, request_id, "
                    "metadata, created_at, chain_hash "
                    "FROM companion_audit_log ORDER BY id DESC LIMIT %s",
                    (limit + 1,),
                )).fetchall()
            rows = [
                {
                    "id": r[0], "event_type": r[1], "user_id": r[2],
                    "ip_address": r[3], "request_id": r[4], "metadata": r[5],
                    "created_at": str(r[6] or ""), "chain_hash": r[7],
                }
                for r in reversed(rows_raw)  # oldest-first for verification
            ]
            anchor_prev: str | None = None
            if len(rows) > limit:
                # Oldest fetched row is the anchor: its stored hash seeds
                # the chain; the row itself is verified by a wider run.
                anchor_prev = rows[0]["chain_hash"]
                rows = rows[1:]
            from . import audit_chain
            return audit_chain.verify_chain(rows, prev_hash=anchor_prev)
        except Exception as e:
            logger.error("Audit chain verify failed: %s", e)
            return ec.err(ec.INTERNAL_ERROR, "Verify failed", status_code=500)

    # ── Audit log viewer (admin-only) ──────────────────────────────────────

    @app.get("/api/audit")
    async def api_audit(
        request: Request,
        limit: int = 100,
        event_type: str | None = None,
        user_id_filter: str | None = None,
    ):
        """Read recent audit events. Admin only.

        Query params: limit (1-1000), event_type, user_id_filter.
        """
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        if not is_admin(user_id):
            return JSONResponse({"error": "Admin only"}, status_code=403)
        from . import audit
        events = await audit.recent(limit=limit, event_type=event_type,
                                    user_id=user_id_filter)
        return {"count": len(events), "events": events}

    # ── Metrics (admin-only) ────────────────────────────────────────────────

    @app.get("/api/metrics")
    async def api_metrics(request: Request):
        """In-process metrics snapshot (admin user only).

        Returns counters, latency histograms, and uptime. Admin scoping:
        user must be 'jalsarraf' (the operator). Other users get 403.
        """
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        if not is_admin(user_id):
            return JSONResponse({"error": "Admin only"}, status_code=403)
        from . import metrics as _m
        snap = _m.snapshot()
        try:
            pool = get_pool()
            snap["db_pool"] = {
                "min_size": pool.min_size,
                "max_size": pool.max_size,
            }
        except Exception:
            snap["db_pool"] = {"status": "unavailable"}
        return snap

    # ── Memory search (full-text over annotations + prompts) ───────────────

    @app.get("/api/memories/search")
    async def api_memories_search(request: Request, q: str, limit: int = 20):
        """Search memory annotations by ILIKE substring. Scoped to user."""
        user_id = await _get_user_id(request)
        if not user_id:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        if not q or len(q.strip()) < 2:
            return JSONResponse({"error": "Query must be at least 2 characters"}, status_code=400)
        limit = max(1, min(limit, 100))
        try:
            pool = get_pool()
            async with pool.connection() as conn:
                rows = await conn.execute(
                    "SELECT id, filename, annotation, category, scene_tags, created_at "
                    "FROM companion_memories "
                    "WHERE user_id = %s "
                    "  AND (annotation ILIKE %s OR prompt ILIKE %s) "
                    "  AND kept = true "
                    "ORDER BY created_at DESC LIMIT %s",
                    (user_id, f"%{q}%", f"%{q}%", limit),
                )
                results = [
                    {
                        "id": str(r[0]),
                        "filename": r[1],
                        "annotation": r[2],
                        "category": r[3],
                        "scene_tags": r[4],
                        "created_at": r[5].isoformat() if r[5] else None,
                    }
                    for r in await rows.fetchall()
                ]
            return {"query": q, "count": len(results), "results": results}
        except Exception as e:
            logger.error("Memory search failed: %s", e)
            return JSONResponse({"error": "Search failed"}, status_code=500)

    # ── Voice letters (async JP voice notes left while the Commander is away) ─

    @app.get("/api/voice-notes/latest")
    async def api_voice_note_latest(request: Request):
        """Return metadata for the user's most recent voice letter (or null).

        Lets the client surface an unheard letter. Audio is fetched separately
        via /api/voice-notes/{id}/audio. Scoped to the authenticated user.
        """
        from . import error_codes as ec
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        from . import voice_archive
        note = await voice_archive.latest_voice_note(user_id)
        return {"voice_note": note}

    @app.get("/api/voice-notes/{note_id}/audio")
    async def api_voice_note_audio(note_id: str, request: Request):
        """Stream a voice letter's WAV bytes. Owner-scoped; 404 otherwise.

        Auth via the same _get_user_id pattern as every other endpoint here.
        The note is fetched with the caller's user_id, so a non-owner (or a
        missing note / missing file) yields 404 — never another user's audio.
        Served immutable: the audio for a given id never changes.
        """
        from . import error_codes as ec
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        from . import voice_archive
        result = await voice_archive.get_voice_note(note_id, user_id)
        if result is None:
            return ec.err(ec.NOT_FOUND, "Voice note not found", status_code=404)
        audio_bytes, _filename = result
        return Response(
            content=audio_bytes,
            media_type="audio/wav",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.post("/api/voice-notes/{note_id}/played")
    async def api_voice_note_played(note_id: str, request: Request):
        """Mark a voice letter as played. Owner-scoped; 404 if not owner/missing.

        Idempotent: only the first call stamps played_at, so a re-play won't
        reset the timestamp. Returns {ok, already_played}.
        """
        from . import error_codes as ec
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        from . import voice_archive
        updated = await voice_archive.mark_played(note_id, user_id)
        if not updated:
            # Either it was already played, or it doesn't belong to this user /
            # doesn't exist. Disambiguate with an ownership check so a genuine
            # re-play returns 200 (already_played) while a missing note is 404.
            existing = await voice_archive.get_voice_note(note_id, user_id)
            if existing is None:
                return ec.err(ec.NOT_FOUND, "Voice note not found", status_code=404)
            return {"ok": True, "already_played": True}
        return {"ok": True, "already_played": False}
