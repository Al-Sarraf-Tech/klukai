"""Agentic loop: Klukai's internal reasoning and tool-use pipeline via local LLM."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from .llm_router import LLMRouter, LLMConfig, LM_STUDIO_URL, LOCAL_AGENT, LOCAL_CASUAL
from .mcp_client import MCPClient
from .tool_schemas import get_tool_schemas
from .ws_manager import WSManager

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 5
TIMEOUT_SECONDS = 150
MAX_TOOL_RESULT_CHARS = 3000
TOOL_TIMEOUT = 45


@dataclass
class AgentStep:
    type: str  # "thinking" | "tool_call" | "tool_result" | "response"
    content: str
    tool_name: str | None = None
    tool_args: dict | None = None
    tool_result: dict | None = None


@dataclass
class AgentResult:
    response: str
    steps: list[AgentStep] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    iterations: int = 0
    model: str = LOCAL_AGENT


# Status messages for tool operations
TOOL_STATUS = {
    "web_search": "Searching the network...",
    "fetch_url": "Reading intelligence report...",
    "get_weather": "Checking atmospheric conditions...",
    "search_web": "Scanning communications...",
}


class AgentLoop:
    """Klukai's agentic reasoning loop with MCP tool access via local LLM."""

    def __init__(
        self,
        router: LLMRouter,
        mcp: MCPClient,
        ws: WSManager,
    ) -> None:
        self._router = router
        self._mcp = mcp
        self._ws = ws

    async def run(
        self,
        system_prompt: str,
        messages: list[dict],
    ) -> AgentResult:
        """Execute the agentic loop using local LLM."""
        result = AgentResult(response="")
        start_time = time.monotonic()

        tools = await get_tool_schemas(self._mcp)
        if not tools:
            logger.warning("No tools available for agent loop")
            # Try reinitializing MCP session
            try:
                await self._mcp._initialize_session()
                tools = await get_tool_schemas(self._mcp)
                if tools:
                    logger.info("MCP session recovered, %d tools available", len(tools))
            except Exception as e:
                logger.warning("MCP session recovery failed: %s", e)

        # Try agent model first, fall back to chat model if not available
        config = LLMConfig(
            provider="lmstudio",
            model=LOCAL_CASUAL,  # Use R1 model — handles tools + chat
            base_url=LM_STUDIO_URL,
            max_tokens=2048,
            temperature=0.7,
        )

        work_messages = list(messages)
        await self._ws.send_thinking("default", "Analyzing request...")

        for iteration in range(MAX_ITERATIONS):
            result.iterations = iteration + 1

            elapsed = time.monotonic() - start_time
            if elapsed > TIMEOUT_SECONDS:
                logger.warning("Agent loop timeout after %.1fs", elapsed)
                break

            try:
                response = await self._router.complete_local(
                    system_prompt, work_messages, config,
                    tools=tools if tools else None,
                )
            except Exception as e:
                logger.error("Agent loop LLM call failed: %s", e)
                # Try streaming fallback
                try:
                    logger.info("Trying streaming fallback for agent response")
                    await self._ws.send_thinking("default", "Switching to direct response...")
                    parts = []
                    async for token in self._router.stream(system_prompt, work_messages, config):
                        parts.append(token)
                    if parts:
                        result.response = "".join(parts)
                        return result
                except Exception as e2:
                    logger.error("Streaming fallback also failed: %s", e2)

                result.response = (
                    "Communications disrupted, Commander. "
                    "I'll respond with what I have."
                )
                break

            choice = response.get("choices", [{}])[0]
            message = choice.get("message", {})
            content = message.get("content", "") or ""
            tool_calls = message.get("tool_calls", []) or []

            if not tool_calls:
                result.response = content
                result.steps.append(AgentStep(type="response", content=content))
                break

            assistant_msg: dict = {"role": "assistant"}
            if content:
                assistant_msg["content"] = content
            assistant_msg["tool_calls"] = tool_calls
            work_messages.append(assistant_msg)

            for tc in tool_calls:
                tc_id = tc.get("id", "")
                func = tc.get("function", {})
                tool_name = func.get("name", "unknown")
                tool_args_str = func.get("arguments", "{}")

                try:
                    tool_args = json.loads(tool_args_str) if isinstance(tool_args_str, str) else tool_args_str
                except json.JSONDecodeError:
                    tool_args = {}

                result.tools_used.append(tool_name)
                result.steps.append(AgentStep(
                    type="tool_call",
                    content=f"Invoking {tool_name}",
                    tool_name=tool_name,
                    tool_args=tool_args,
                ))

                # Send descriptive status message
                status_msg = TOOL_STATUS.get(tool_name, f"Using {tool_name}...")
                await self._ws.send_tool_use("default", tool_name, "calling")
                await self._ws.send_thinking("default", status_msg)

                try:
                    tool_result = await asyncio.wait_for(
                        self._mcp.invoke_tool(tool_name, tool_args),
                        timeout=TOOL_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    tool_result = {"error": f"Tool {tool_name} timed out after {TOOL_TIMEOUT}s"}
                    logger.warning("Tool %s timed out", tool_name)
                    # Reinitialize MCP session in case it's stale
                    try:
                        await self._mcp._initialize_session()
                    except Exception:
                        pass
                except Exception as e:
                    tool_result = {"error": str(e)}
                    logger.error("Tool %s failed: %s", tool_name, e)

                result_text = _extract_tool_text(tool_result)

                result.steps.append(AgentStep(
                    type="tool_result",
                    content=result_text[:500],
                    tool_name=tool_name,
                    tool_result=tool_result,
                ))

                await self._ws.send_tool_use("default", tool_name, "done")

                work_messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": result_text[:MAX_TOOL_RESULT_CHARS],
                })

            logger.info(
                "Agent iteration %d: %d tools called (%s)",
                iteration + 1,
                len(tool_calls),
                ", ".join(tc.get("function", {}).get("name", "?") for tc in tool_calls),
            )

        # Force synthesis if no response after all iterations
        if not result.response:
            try:
                await self._ws.send_thinking("default", "Compiling briefing...")
                final = await self._router.complete_local(
                    system_prompt, work_messages, config, tools=None,
                )
                final_content = (
                    final.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    or ""
                )
                if final_content.strip():
                    result.response = final_content
            except Exception as e:
                logger.warning("Forced synthesis failed: %s", e)

        if not result.response:
            result.response = (
                "Intelligence gathered, Commander, but synthesis failed. "
                "I'll brief you with what I have on the next pass."
            )

        return result


def _extract_tool_text(result: dict) -> str:
    """Extract readable text from an MCP tool result."""
    content = result.get("content", [])
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    texts.append(item.get("text", ""))
                elif "text" in item:
                    texts.append(item["text"])
            elif isinstance(item, str):
                texts.append(item)
        if texts:
            combined = "\n".join(texts)
            # Clean up HTML artifacts
            import re
            combined = re.sub(r'<[^>]+>', ' ', combined)
            combined = re.sub(r'\s+', ' ', combined).strip()
            return combined

    if isinstance(content, str):
        return content
    if "error" in result:
        return f"Error: {result['error']}"

    return str(result)[:2000]
