"""Fail-closed allowlist policy for configured MCP endpoints and tool names."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass(frozen=True)
class ServerConfig:
    name: str
    url: str
    allowed_tools: frozenset[str]
    enabled_env: str | None = None
    token_env: str | None = None
    token_file: str | None = None

    def enabled(self) -> bool:
        return self.enabled_env is None or os.getenv(
            self.enabled_env, ""
        ).strip().lower() in {"true", "1", "yes"}


def _tool_names(value: object, used: set[str], reserved: set[str]) -> frozenset[str]:
    if not isinstance(value, list) or not value:
        raise ValueError
    for tool in value:
        if (
            not isinstance(tool, str)
            or not _NAME.fullmatch(tool)
            or tool in used
            or tool in reserved
        ):
            raise ValueError
        used.add(tool)
    return frozenset(value)


def _auth_fields(entry: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    for field in ("enabled_env", "token_env"):
        if field in entry and (
            not isinstance(entry[field], str) or not _ENV_NAME.fullmatch(entry[field])
        ):
            raise ValueError
    if "token_file" in entry and (
        not isinstance(entry["token_file"], str) or not entry["token_file"]
    ):
        raise ValueError
    if "token_file" in entry and "token_env" in entry:
        raise ValueError
    return entry.get("enabled_env"), entry.get("token_env"), entry.get("token_file")


def _parse_server(
    entry: object, names: set[str], tools: set[str], reserved: set[str]
) -> ServerConfig:
    if not isinstance(entry, dict) or set(entry) - {
        "name",
        "url",
        "allowed_tools",
        "enabled_env",
        "token_env",
        "token_file",
    }:
        raise ValueError
    name, url = entry.get("name"), entry.get("url")
    if not isinstance(name, str) or not _NAME.fullmatch(name) or name in names:
        raise ValueError
    if not isinstance(url, str):
        raise ValueError
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError
    _ = parsed.port  # Reject malformed ports before a network client formats them.
    allowed = _tool_names(entry.get("allowed_tools"), tools, reserved)
    enabled, token_env, token_file = _auth_fields(entry)
    names.add(name)
    return ServerConfig(name, url, allowed, enabled, token_env, token_file)


def load_config(path: str) -> list[ServerConfig]:
    """Validate the complete configuration before making any connection."""
    from .tool_schemas import get_builtin_tools

    reserved = {tool["function"]["name"] for tool in get_builtin_tools()}
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError
        data = json.loads(raw)
        if (
            not isinstance(data, dict)
            or set(data) != {"servers"}
            or not isinstance(data["servers"], list)
        ):
            raise ValueError
        if len(data["servers"]) > 16:
            raise ValueError
        names: set[str] = set()
        tools: set[str] = set()
        return [
            _parse_server(entry, names, tools, reserved) for entry in data["servers"]
        ]
    except (OSError, ValueError, TypeError, RecursionError):
        logger.warning("MCP configuration rejected; configured tools disabled")
        return []
