"""Agent/direct chat reply presentation, isolated from memory writes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
import time
from typing import Any

from .agent_loop import AgentLoop
from .llm_router import FAILURE_SENTINEL, LLMStreamFailed, LLMRouter
from .mcp_client import MCPClient
from .models import SessionState
from .ws_manager import WSManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatReply:
    text: str
    model: str
    latency_ms: int
    failed: bool


@dataclass
class ChatReplyRuntime:
    router: LLMRouter
    mcp: MCPClient
    ws: WSManager
    agent_factory: Callable[[LLMRouter, MCPClient, WSManager], AgentLoop]
    fix_narration: Callable[[str], str]
    chunk_text: Callable[[str, int], list[str]]
    failure_prefix: str

    async def respond(
        self,
        content: str,
        session: SessionState,
        system_prompt: str,
        messages: list[dict[str, Any]],
        user_id: str,
        msg_id: str,
        start: float,
    ) -> ChatReply:
        if await self.router.needs_agent(content, interactive=True):
            return await self.agent(system_prompt, messages, user_id, msg_id, start)
        return await self.direct(
            content, session, system_prompt, messages, user_id, msg_id, start
        )

    async def agent(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        user_id: str,
        msg_id: str,
        start: float,
    ) -> ChatReply:
        # Agentic path: think, use tools, then respond (agent loop sends its own thinking events)
        logger.info("Routing to agent loop (tools needed)")
        agent = self.agent_factory(self.router, self.mcp, self.ws)
        agent_result = await agent.run(
            system_prompt, messages, user_id=user_id, interactive=True
        )

        response_text = self.fix_narration(agent_result.response)
        model_name = agent_result.model

        # Stream the final response token by token for the UI
        for token in self.chunk_text(response_text, 8):
            await self.ws.send_token(user_id, token)
            await asyncio.sleep(0.02)

        latency_ms = int((time.monotonic() - start) * 1000)
        await self.ws.send_done(user_id, msg_id, model_name)

        logger.info(
            "Agent loop: %d iterations, %d tools used (%s)",
            agent_result.iterations,
            len(agent_result.tools_used),
            ", ".join(agent_result.tools_used) or "none",
        )
        return ChatReply(
            response_text,
            model_name,
            latency_ms,
            response_text.startswith(self.failure_prefix),
        )

    async def direct(
        self,
        content: str,
        session: SessionState,
        system_prompt: str,
        messages: list[dict[str, Any]],
        user_id: str,
        msg_id: str,
        start: float,
    ) -> ChatReply:
        stream_failed = False
        # Direct path: stream from LLM
        await self.ws.send_thinking(user_id, "Composing response...")
        try:
            config = await self.router.route(content, session, interactive=True)
        except RuntimeError as e:
            # No local backend: dominus-nobara is off or its gateway port is
            # not bound yet. That is normal, not a crash — degrade in-band
            # exactly like a pre-token stream failure (the sentinel is the
            # whole reply) instead of letting the WS loop log a traceback and
            # tell him "something glitched" on every turn of the outage.
            logger.warning("Local LLM unavailable this turn: %s", e)
            await self.ws.send_token(user_id, FAILURE_SENTINEL)
            await self.ws.send_done(user_id, msg_id, "offline")
            return ChatReply("", "offline", 0, True)
        logger.info("Routing to %s/%s", config.provider, config.model)

        # Fire a delayed "warming up" message if model is slow to respond
        warmup_timer: asyncio.Task[Any] | None = None
        if config.provider == "lmstudio":

            async def _warmup_notify() -> None:
                await asyncio.sleep(8.0)
                await self.ws.send_thinking(
                    user_id, "Loading neural pathways, Commander. Stand by..."
                )

            warmup_timer = asyncio.create_task(_warmup_notify())

        full_response = []
        buffer = ""
        first_flush = True
        try:
            async for token in self.router.stream(system_prompt, messages, config):
                # Cancel warmup message once first token arrives
                if (
                    warmup_timer
                    and not warmup_timer.done()
                    and not warmup_timer.cancelling()
                ):
                    warmup_timer.cancel()
                full_response.append(token)
                buffer += token
                # First flush after 20 chars for fast perceived response, then sentence boundaries
                flush_threshold = 20 if first_flush else 80
                if any(c in buffer for c in ".!?\n)") or len(buffer) > flush_threshold:
                    await self.ws.send_token(user_id, buffer)
                    buffer = ""
                    first_flush = False
        except LLMStreamFailed as e:
            # Mid-stream death: don't propagate (it would kill the WS). The
            # user message is already persisted; surface an explicit error,
            # finalize the truncated reply for the UI, and skip follow-ups.
            stream_failed = True
            logger.error("LLM stream failed mid-response: %s", e)
            await self.ws.send(
                user_id,
                {
                    "type": "error",
                    "message": "...Connection to my core dropped mid-sentence, "
                    "Commander. Say that again?",
                },
            )
            # The unflushed tail is not sent after the error: a token frame
            # following an error opens a stray second bubble in the client.
            buffer = ""
        finally:
            # Retain and join the notifier even after the first token cancels
            # it. Disconnects and stream exceptions must retire status work
            # before a later connection can receive another reply.
            if warmup_timer:
                if not warmup_timer.done() and not warmup_timer.cancelling():
                    warmup_timer.cancel()
                await asyncio.gather(warmup_timer, return_exceptions=True)
        if buffer:
            await self.ws.send_token(user_id, buffer)

        # Apply narration fix only to the complete text (avoids split-pattern bugs)
        raw_text = "".join(full_response)
        response_text = self.fix_narration(raw_text)
        model_name = config.model
        latency_ms = int((time.monotonic() - start) * 1000)

        # Send corrected final text if narration fix changed anything
        final_text = response_text if response_text != raw_text else None
        await self.ws.send_done(user_id, msg_id, model_name, final_text=final_text)

        return ChatReply(
            response_text,
            model_name,
            latency_ms,
            stream_failed or response_text.startswith(self.failure_prefix),
        )
