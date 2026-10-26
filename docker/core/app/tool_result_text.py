"""Bounded model-visible tool text, retaining source links after truncation."""

from __future__ import annotations

import html
import re
from typing import Any

MAX_TOOL_RESULT_CHARS = 8000


def _extract_tool_text(result: dict[str, Any]) -> str:
    """Extract readable text from an MCP tool result."""
    content = result.get("content", [])
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" or "text" in item:
                    text = item.get("text", "")
                    if isinstance(text, str):
                        texts.append(text)
            elif isinstance(item, str):
                texts.append(item)
        if texts:
            combined = "\n".join(texts)
            # Keep link targets before stripping presentation markup. Markdown
            # autolinks are URLs, not HTML tags.
            combined = re.sub(
                r"<a\b[^>]*\bhref=[\"\'](https?://[^\"\']+)[\"\'][^>]*>(.*?)</a>",
                lambda match: f"{match[2]} ({html.unescape(match[1])})",
                combined,
                flags=re.IGNORECASE | re.DOTALL,
            )
            combined = re.sub(r"<(https?://[^\s<>]+)>", r"\1", combined)
            combined = re.sub(r"<[^>]+>", " ", combined)
            combined = html.unescape(combined)
            combined = re.sub(r"[\t\r\f\v ]+", " ", combined)
            combined = re.sub(r" *\n *", "\n", combined)
            combined = re.sub(r"\n{3,}", "\n\n", combined).strip()
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
    urls = list(dict.fromkeys(re.findall(r"https?://[^\s<>\"\']+", text)))
    sources = []
    source_chars = 0
    for url in urls:
        url = url.rstrip(").,;]")
        if len(url) > 1000 or source_chars + len(url) > 2000:
            continue
        sources.append(url)
        source_chars += len(url) + 1
        if len(sources) == 6:
            break
    suffix = "\n[Tool result truncated.]"
    if sources:
        suffix += "\nSource URLs from this result:\n" + "\n".join(sources)
    return text[: MAX_TOOL_RESULT_CHARS - len(suffix)].rstrip() + suffix
