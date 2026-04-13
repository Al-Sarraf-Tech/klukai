"""Shared application context — global service instances.

Every module that needs these services imports from here instead of
creating its own instances.  This avoids circular imports and ensures
a single set of singletons across the process.
"""

from __future__ import annotations

from .affection import AffectionManager
from .llm_router import LLMRouter
from .mcp_client import MCPClient
from .memory import MemoryManager
from .physical_state import PhysicalStateTracker
from .proactive import ProactiveEngine
from .ws_manager import WSManager

memory = MemoryManager()
router = LLMRouter()
mcp = MCPClient()
ws = WSManager()
proactive = ProactiveEngine()
affection = AffectionManager()
physical = PhysicalStateTracker()

SESSION_ID = "default"  # Legacy constant — prefer session_id(user_id)


def session_id(user_id: str) -> str:
    """Return a per-user session key for Redis."""
    return f"session:{user_id}"

# Tracks per-user most recently generated memory_id for save/discard overrides
_last_memory_ids: dict[str, str] = {}


def get_last_memory_id(user_id: str) -> str | None:
    return _last_memory_ids.get(user_id)


def set_last_memory_id(user_id: str, memory_id: str) -> None:
    _last_memory_ids[user_id] = memory_id

# Compaction threshold — compact oldest turns when session exceeds this
COMPACT_THRESHOLD = 8
COMPACT_KEEP_RAW = 4  # Keep this many recent turns verbatim after compaction
