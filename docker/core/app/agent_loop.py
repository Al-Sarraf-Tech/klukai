"""Klukai's existing tools with opt-in Sonnet reasoning and local fallback."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from dataclasses import dataclass, field

from .llm_router import FAILURE_SENTINEL, LLMRouter, LLMConfig, LM_STUDIO_URL, LOCAL_AGENT, LOCAL_CASUAL
from .models import SessionState
from .mcp_client import MCPClient
from .tool_schemas import get_tool_schemas, get_builtin_tools
from .ws_manager import WSManager

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 5
TIMEOUT_SECONDS = 150
MAX_TOOL_RESULT_CHARS = 8000
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
    """Klukai's agentic reasoning loop with MCP and built-in tool access."""

    def __init__(
        self,
        router: LLMRouter,
        mcp: MCPClient,
        ws: WSManager,
    ) -> None:
        self._router = router
        self._mcp = mcp
        self._ws = ws

    async def _builtin_recall_memory(self, args: dict, user_id: str) -> dict:
        """Search Klukai's memory for the Commander."""
        query = args.get("query", "")
        try:
            from .context import memory

            # Search facts — data API uses SQL LIKE patterns (% wildcard)
            pattern = f"rel:%{query.replace(' ', '%')}%"
            facts = await memory.recall_facts_by_pattern(pattern, user_id=user_id)
            fact_text = "\n".join(f"- {f.get('value', '')}" for f in facts[:5]) if facts else "No matching facts found."

            # Search episodic memories via Qdrant
            episodes = await memory.recall_episodes(query, user_id=user_id, limit=3)
            ep_text = "\n".join(f"- {e.get('summary', '')[:150]}" for e in episodes) if episodes else ""

            result = f"Facts:\n{fact_text}"
            if ep_text:
                result += f"\n\nRelated conversations:\n{ep_text}"

            return {"content": [{"type": "text", "text": result}]}
        except Exception as e:
            logger.error("recall_memory tool failed: %s", e)
            return {"content": [{"type": "text", "text": f"Memory search error: {e}"}]}

    async def _builtin_get_time(self) -> dict:
        """Return current date/time."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        return {"content": [{"type": "text", "text": now.strftime("%A, %B %d, %Y at %I:%M %p UTC")}]}

    async def run(
        self,
        system_prompt: str,
        messages: list[dict],
        user_id: str = "default",
        *,
        interactive: bool = False,
    ) -> AgentResult:
        """Use the interactive primary when opted in; otherwise stay local."""
        result = AgentResult(response="")
        start_time = time.monotonic()

        tools = await get_tool_schemas(self._mcp)
        if not tools:
            logger.warning("No MCP tools available — trying recovery")
            try:
                await self._mcp._initialize_session()
                tools = await get_tool_schemas(self._mcp, force_refresh=True)
                if tools:
                    logger.info("MCP session recovered, %d tools available", len(tools))
            except Exception as e:
                logger.warning("MCP session recovery failed: %s", e)

        # Always include builtin tools (memory recall, time)
        tools = (tools or []) + get_builtin_tools()
        declared_names = {
            tool["function"]["name"] for tool in tools
            if isinstance(tool.get("function"), dict)
            and isinstance(tool["function"].get("name"), str)
        }

        # Try agent model first, fall back to chat model if not available
        config = LLMConfig(
            provider="lmstudio",
            model=LOCAL_CASUAL,  # Use R1 model — handles tools + chat
            base_url=LM_STUDIO_URL,
            max_tokens=2048,
            temperature=0.7,
        )
        if interactive:
            try:
                config = await self._router.route(
                    "", SessionState(conversation_id="agent"), needs_tools=True, interactive=True,
                )
            except RuntimeError:
                return AgentResult(response=FAILURE_SENTINEL, model="offline")
        complete = self._router.complete_chat if interactive else self._router.complete_local
        # Record the model that actually answers — the dataclass default is
        # LOCAL_AGENT, which is not what runs here, and the name is persisted
        # with the message and shown in the UI's done frame.
        result.model = config.model

        work_messages = list(messages)
        await self._ws.send_thinking(user_id, "Analyzing request...")

        for iteration in range(MAX_ITERATIONS):
            result.iterations = iteration + 1

            elapsed = time.monotonic() - start_time
            if elapsed > TIMEOUT_SECONDS:
                logger.warning("Agent loop timeout after %.1fs", elapsed)
                break

            try:
                response = await complete(
                    system_prompt, work_messages, config,
                    tools=tools if tools else None,
                )
                result.model = config.model
            except Exception as e:
                logger.error("Agent loop LLM call failed: %s", e)
                # Try streaming fallback
                try:
                    logger.info("Trying streaming fallback for agent response")
                    await self._ws.send_thinking(user_id, "Switching to direct response...")
                    parts = []
                    async for token in self._router.stream(system_prompt, work_messages, config):
                        parts.append(token)
                    result.model = config.model
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
            if message.get("_anthropic_content"):
                assistant_msg["_anthropic_content"] = message["_anthropic_content"]
            work_messages.append(assistant_msg)

            for tc in tool_calls:
                tc_id = tc.get("id", "")
                func = tc.get("function", {})
                requested_name = func.get("name")
                tool_name = requested_name if isinstance(requested_name, str) else "unknown"
                tool_args_str = func.get("arguments", "{}")

                rejection = None
                try:
                    tool_args = json.loads(tool_args_str) if isinstance(tool_args_str, str) else tool_args_str
                except json.JSONDecodeError:
                    tool_args = None
                if not isinstance(requested_name, str) or tool_name not in declared_names:
                    rejection = "Tool is not available in this turn."
                elif not isinstance(tool_args, dict):
                    rejection = "Tool arguments must be a JSON object."

                if rejection is None:
                    result.tools_used.append(tool_name)
                result.steps.append(AgentStep(
                    type="tool_call",
                    content=f"Rejected {tool_name}" if rejection else f"Invoking {tool_name}",
                    tool_name=tool_name,
                    tool_args=tool_args if isinstance(tool_args, dict) else None,
                ))

                # Send descriptive status message
                status_msg = "Tool request rejected." if rejection else TOOL_STATUS.get(tool_name, f"Using {tool_name}...")
                await self._ws.send_tool_use(user_id, tool_name, "calling")
                await self._ws.send_thinking(user_id, status_msg)

                # Handle builtin tools locally, others via MCP
                if rejection is not None:
                    tool_result = {"error": rejection}
                elif tool_name == "recall_memory":
                    tool_result = await self._builtin_recall_memory(tool_args, user_id)
                elif tool_name == "get_current_time":
                    tool_result = await self._builtin_get_time()
                else:
                    try:
                        tool_result = await asyncio.wait_for(
                            self._mcp.invoke_tool(tool_name, tool_args),
                            timeout=TOOL_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        tool_result = {"error": f"Tool {tool_name} timed out after {TOOL_TIMEOUT}s"}
                        logger.warning("Tool %s timed out", tool_name)
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

                await self._ws.send_tool_use(user_id, tool_name, "done")

                work_messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": _bounded_tool_text(result_text),
                })

            logger.info(
                "Agent iteration %d: %d tools called (%s)",
                iteration + 1,
                len(tool_calls),
                ", ".join(str(tc.get("function", {}).get("name", "?")) for tc in tool_calls),
            )

        # Force synthesis if no response after all iterations
        if not result.response:
            try:
                await self._ws.send_thinking(user_id, "Compiling briefing...")
                if interactive:
                    # Claude needs the declared tools for its earlier tool_use
                    # messages, even when this turn must finish with plain text.
                    final = await self._router.complete_chat(
                        system_prompt, work_messages, config, tools=tools, allow_tools=False,
                    )
                else:
                    final = await complete(system_prompt, work_messages, config, tools=None)
                result.model = config.model
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
            # Keep link targets before stripping presentation markup. Markdown
            # autolinks are URLs, not HTML tags.
            combined = re.sub(
                r'<a\b[^>]*\bhref=[\"\'](https?://[^\"\']+)[\"\'][^>]*>(.*?)</a>',
                lambda match: f"{match[2]} ({html.unescape(match[1])})",
                combined, flags=re.IGNORECASE | re.DOTALL,
            )
            combined = re.sub(r'<(https?://[^\s<>]+)>', r'\1', combined)
            combined = re.sub(r'<[^>]+>', ' ', combined)
            combined = html.unescape(combined)
            combined = re.sub(r'[\t\r\f\v ]+', ' ', combined)
            combined = re.sub(r' *\n *', '\n', combined)
            combined = re.sub(r'\n{3,}', '\n\n', combined).strip()
            return combined

    if isinstance(content, str):
        return content
    if "error" in result:
        return f"Error: {result['error']}"

    return str(result)[:2000]


def _bounded_tool_text(text: str) -> str:
    """Bound model context while keeping a few source URLs after truncation."""
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    urls = list(dict.fromkeys(re.findall(r'https?://[^\s<>\"\']+', text)))
    sources = []
    source_chars = 0
    for url in urls:
        url = url.rstrip(').,;]')
        if len(url) > 1000 or source_chars + len(url) > 2000:
            continue
        sources.append(url)
        source_chars += len(url) + 1
        if len(sources) == 6:
            break
    suffix = "\n[Tool result truncated.]"
    if sources:
        suffix += "\nSource URLs from this result:\n" + "\n".join(sources)
    return text[:MAX_TOOL_RESULT_CHARS - len(suffix)].rstrip() + suffix
