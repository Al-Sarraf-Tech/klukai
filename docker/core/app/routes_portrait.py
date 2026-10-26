"""Live Portrait + avatar model routes.

GET  /api/portrait                               frames for what she is wearing now (Bearer)
POST /api/portrait/refresh                        re-render the set (Bearer; 1 per 10 min;
                                                  GPU renderer only, else 409)
GET  /api/portrait/frame/{user}/{outfit}/{f}.webp a frame, by short-lived signature (no Bearer,
                                                  so it works as a plain <img src>)
GET  /api/avatar/model                            the GLB (Bearer; game-ripped, never public;
                                                  private ETag, 304 on If-None-Match)
"""

from __future__ import annotations
from typing import Any


import asyncio
import gzip
import hashlib
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from . import avatar_models, context, portrait
from . import error_codes as ec

logger = logging.getLogger(__name__)

DEFAULT_AVATAR_MODEL_PATH = "/avatar/klukai.glb"
FRAME_CACHE = "private, max-age=1800"
# The model is personal-use, game-derived content behind the bearer token.
# `private`: only his browser may keep it, never Cloudflare or any shared
# cache. `no-cache`: the browser revalidates on every load, so each load
# still passes the auth check (a revoked token gets a 401, not his cached
# copy) and a rebuilt model reaches him at once; an unchanged one costs a
# 304 with no body. The ETag is a strong hash of the bytes.
MODEL_CACHE = "private, no-cache"
# The response differs by content coding and is only for this bearer.
MODEL_VARY = "Accept-Encoding, Authorization"
_MODEL_GZ: dict[str, bytes] = {}  # etag -> gzipped model (one entry)
# str(path) -> (size, mtime_ns, sha256 hex): hashed once per file version.
_MODEL_DIGEST: dict[str, tuple[int, int, str]] = {}


async def _get_user_id(request: Request) -> str | None:
    from .auth import get_user_from_token

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return await get_user_from_token(auth[7:])


def _digest_file(path: Path) -> str:
    """sha256 of the model, read in chunks. Runs in a worker thread."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def _model_digest(path: Path) -> str:
    """The model's content hash, computed once per file version (size and
    mtime) and off the event loop; after that a request costs one stat. A
    file that changes while it is hashed is hashed again next time."""
    st = path.stat()
    version = (st.st_size, st.st_mtime_ns)
    cached = _MODEL_DIGEST.get(str(path))
    if cached is not None and cached[:2] == version:
        return cached[2]
    digest = await asyncio.to_thread(_digest_file, path)
    after = path.stat()
    if (after.st_size, after.st_mtime_ns) == version:
        _MODEL_DIGEST[str(path)] = (*version, digest)
    return digest


def _etag_matches(header: str, etag: str) -> bool:
    if header.strip() == "*":
        return True
    tags = {t.strip().removeprefix("W/") for t in header.split(",")}
    return etag in tags


def _gzip_file(path: Path) -> bytes:
    """Read + deflate the model. Runs in a worker thread: tens of MB would
    otherwise stall the event loop (chat WebSocket included) for seconds."""
    return gzip.compress(path.read_bytes(), compresslevel=6)


def register_portrait_routes(app: FastAPI) -> None:
    async def _context(user_id: str) -> tuple[int, str, bool]:
        level = (await context.affection.get_state(user_id)).level
        outfit = await portrait.current_outfit(user_id, level)
        # The game guard belongs to Dominus's GPU; off it, never ask Dominus.
        game = await context.router.is_game_active() if portrait.uses_gpu() else False
        return level, outfit, game

    @app.get("/api/portrait")
    async def api_portrait(request: Request) -> Any:
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        level, outfit, game = await _context(user_id)
        return await portrait.portrait_state(user_id, outfit, level, game_active=game)

    @app.post("/api/portrait/animate")
    async def api_portrait_animate(request: Request) -> Any:
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        from . import portrait_account

        if not portrait_account.enabled_for(user_id):
            return ec.err(
                "UNAVAILABLE",
                "Portrait animation is not configured for this account.",
                status_code=403,
            )
        level, outfit, _ = await _context(user_id)
        if not portrait.animate(user_id, outfit, level):
            return ec.err(
                "IN_PROGRESS",
                "Portrait animation is busy or cooling down.",
                status_code=409,
            )
        return await portrait.portrait_state(user_id, outfit, level, game_active=False)

    @app.post("/api/portrait/refresh")
    async def api_portrait_refresh(request: Request) -> Any:
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        if not portrait.can_redraw():
            return ec.err(
                "REDRAW_UNAVAILABLE",
                "Her portrait is kept as drawn; redrawing it needs the local renderer.",
                status_code=409,
            )
        level, outfit, game = await _context(user_id)
        if game:
            return ec.err(
                "GPU_BUSY",
                "A game owns the GPU right now; try again after it.",
                status_code=409,
            )
        if not portrait.refresh(user_id, outfit, level):
            return ec.err(
                "IN_PROGRESS", "Her portrait is already being drawn.", status_code=409
            )
        return await portrait.portrait_state(user_id, outfit, level, game_active=False)

    @app.get("/api/portrait/frame/{user_id}/{outfit_id}/{frame}.webp")
    async def api_portrait_frame(
        user_id: str, outfit_id: str, frame: str, sig: str = ""
    ) -> Any:
        try:
            path = portrait.frame_file(user_id, outfit_id, frame)
        except ValueError:
            return JSONResponse({"error": "Not found"}, status_code=404)
        if not portrait.verify_frame(user_id, outfit_id, frame, sig):
            return JSONResponse(
                {"error": "Invalid or expired signature"}, status_code=403
            )
        if not path.is_file():
            return JSONResponse({"error": "Not found"}, status_code=404)
        return FileResponse(
            path, media_type="image/webp", headers={"Cache-Control": FRAME_CACHE}
        )

    @app.get("/api/avatar/models")
    async def api_avatar_models(request: Request) -> Any:
        if not await _get_user_id(request):
            return ec.auth_required()
        default = Path(os.environ.get("AVATAR_MODEL_PATH", DEFAULT_AVATAR_MODEL_PATH))
        return JSONResponse(
            avatar_models.catalog(default), headers={"Cache-Control": MODEL_CACHE}
        )

    @app.get("/api/avatar/model")
    async def api_avatar_model(request: Request, model: str = "default") -> Any:
        # Auth first: without a valid bearer there is no 304 and no bytes.
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        default = Path(os.environ.get("AVATAR_MODEL_PATH", DEFAULT_AVATAR_MODEL_PATH))
        path = avatar_models.model_path(model, default)
        not_found = JSONResponse({"error": "Avatar model not found"}, status_code=404)
        if path is None or not path.is_file():
            return not_found
        try:
            digest = await _model_digest(path)
        except OSError:  # removed or replaced mid-request
            return not_found
        use_gzip = "gzip" in request.headers.get("accept-encoding", "").lower()
        # Strong tag per representation: the gzip body is a different one.
        etag = f'"{model}-{digest[:32]}{"-gz" if use_gzip else ""}"'
        headers = {"Cache-Control": MODEL_CACHE, "ETag": etag, "Vary": MODEL_VARY}
        if _etag_matches(request.headers.get("if-none-match", ""), etag):
            return Response(status_code=304, headers=headers)
        if use_gzip:
            # Binary glTF isn't compressed by Cloudflare or StaticFiles; gzip
            # saves ~30% on a phone connection. Cache the most recently used version.
            body = _MODEL_GZ.get(etag)
            if body is None:
                body = await asyncio.to_thread(_gzip_file, path)
                _MODEL_GZ.clear()
                _MODEL_GZ[etag] = body
            return Response(
                body,
                media_type="model/gltf-binary",
                headers={**headers, "Content-Encoding": "gzip"},
            )
        return FileResponse(path, media_type="model/gltf-binary", headers=headers)
