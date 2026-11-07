"""Private read-only POST routes for original conversation search."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
import logging
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException
from starlette.types import Receive, Scope, Send

from . import message_search
from .message_search import ContextRequest, SearchRequest

logger = logging.getLogger(__name__)


def _response(body: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


class _PrivateRoute(APIRoute):
    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().handle(scope, receive, send)
        except HTTPException as exc:
            # Method errors are raised before FastAPI enters the body handler.
            exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
            raise

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def private_handler(request: Request) -> Response:
            try:
                response = await handler(request)
            except RequestValidationError:
                # Framework validation details include rejected input. Keep
                # phrases and original content out of generic error bodies.
                return _response({"error": "Invalid request"}, 422)
            except HTTPException as exc:
                # Invalid UTF-8 JSON is surfaced by FastAPI as a body-parse
                # HTTP 400 rather than a RequestValidationError.
                status = 422 if exc.status_code == 400 else exc.status_code
                return _response({"error": "Invalid request"}, status)
            except Exception as exc:
                logger.warning("Conversation search %s unavailable: %s", self.name, type(exc).__name__)
                return _response({"error": "Conversation search unavailable"}, 503)
            response.headers["Cache-Control"] = "no-store"
            return response

        return private_handler


def register_message_search(app: FastAPI, resolve_user: Callable[[Request], Awaitable[str | None]]) -> None:
    router = APIRouter(route_class=_PrivateRoute)

    @router.post("/api/messages/search")
    async def api_message_search(req: SearchRequest, request: Request) -> Any:
        user_id = await resolve_user(request)
        if not user_id:
            return _response({"error": "Authentication required"}, 401)
        return _response(await message_search.search_messages(user_id, req))

    @router.post("/api/messages/context")
    async def api_message_context(req: ContextRequest, request: Request) -> Any:
        user_id = await resolve_user(request)
        if not user_id:
            return _response({"error": "Authentication required"}, 401)
        context = await message_search.get_message_context(user_id, req)
        if context is None:
            return _response({"error": "Source not found"}, 404)
        return _response(context)

    app.include_router(router)
