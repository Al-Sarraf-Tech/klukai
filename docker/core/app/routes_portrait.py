"""Live Portrait + avatar model routes.

GET  /api/portrait                               frames for what she is wearing now (Bearer)
POST /api/portrait/refresh                        re-render the set (Bearer; 1 per 10 min)
GET  /api/portrait/frame/{user}/{outfit}/{f}.webp a frame, by short-lived signature (no Bearer,
                                                  so it works as a plain <img src>)
GET  /api/avatar/model                            the GLB (Bearer; game-ripped, never public)
"""

from __future__ import annotations

import gzip
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from . import context, portrait
from . import error_codes as ec

logger = logging.getLogger(__name__)

DEFAULT_AVATAR_MODEL_PATH = "/avatar/klukai.glb"
FRAME_CACHE = "private, max-age=1800"
# Revalidate every load (a 304 is ~free): a rebuilt model reaches him on his
# next open instead of after a day. The ETag changes with the file.
MODEL_CACHE = "private, no-cache"
_MODEL_GZ: dict[str, bytes] = {}  # etag -> gzipped model (one entry)


async def _get_user_id(request: Request) -> str | None:
    from .auth import get_user_from_token

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return await get_user_from_token(auth[7:])


def _etag(path: Path) -> str:
    st = path.stat()
    return f'"{st.st_size:x}-{st.st_mtime_ns:x}"'


def _etag_matches(header: str, etag: str) -> bool:
    if header.strip() == "*":
        return True
    tags = {t.strip().removeprefix("W/") for t in header.split(",")}
    return etag in tags


def register_portrait_routes(app: FastAPI) -> None:
    async def _context(user_id: str) -> tuple[int, str, bool]:
        level = (await context.affection.get_state(user_id)).level
        outfit = await portrait.current_outfit(user_id, level)
        return level, outfit, await context.router.is_game_active()

    @app.get("/api/portrait")
    async def api_portrait(request: Request):
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        level, outfit, game = await _context(user_id)
        return await portrait.portrait_state(user_id, outfit, level, game_active=game)

    @app.post("/api/portrait/refresh")
    async def api_portrait_refresh(request: Request):
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        level, outfit, game = await _context(user_id)
        if game:
            return ec.err("GPU_BUSY", "A game owns the GPU right now; try again after it.",
                          status_code=409)
        if not portrait.refresh(user_id, outfit, level):
            return ec.err("IN_PROGRESS", "Her portrait is already being drawn.", status_code=409)
        return await portrait.portrait_state(user_id, outfit, level, game_active=False)

    @app.get("/api/portrait/frame/{user_id}/{outfit_id}/{frame}.webp")
    async def api_portrait_frame(user_id: str, outfit_id: str, frame: str, sig: str = ""):
        try:
            path = portrait.frame_file(user_id, outfit_id, frame)
        except ValueError:
            return JSONResponse({"error": "Not found"}, status_code=404)
        if not portrait.verify_frame(user_id, outfit_id, frame, sig):
            return JSONResponse({"error": "Invalid or expired signature"}, status_code=403)
        if not path.is_file():
            return JSONResponse({"error": "Not found"}, status_code=404)
        return FileResponse(path, media_type="image/webp", headers={"Cache-Control": FRAME_CACHE})

    @app.get("/api/avatar/model")
    async def api_avatar_model(request: Request):
        user_id = await _get_user_id(request)
        if not user_id:
            return ec.auth_required()
        path = Path(os.environ.get("AVATAR_MODEL_PATH", DEFAULT_AVATAR_MODEL_PATH))
        if not path.is_file():
            return JSONResponse({"error": "Avatar model not found"}, status_code=404)
        etag = _etag(path)
        headers = {"Cache-Control": MODEL_CACHE, "ETag": etag, "Vary": "Accept-Encoding"}
        if _etag_matches(request.headers.get("if-none-match", ""), etag):
            return Response(status_code=304, headers=headers)
        if "gzip" in request.headers.get("accept-encoding", "").lower():
            # Binary glTF isn't compressed by Cloudflare or StaticFiles; gzip
            # saves ~30% on a phone connection. Compressed once per version.
            body = _MODEL_GZ.get(etag)
            if body is None:
                body = gzip.compress(path.read_bytes(), compresslevel=6)
                _MODEL_GZ.clear()
                _MODEL_GZ[etag] = body
            return Response(
                body, media_type="model/gltf-binary",
                headers={**headers, "Content-Encoding": "gzip"},
            )
        return FileResponse(path, media_type="model/gltf-binary", headers=headers)
