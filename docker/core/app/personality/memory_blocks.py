"""Memory + relationship + conversation-recall blocks for the system prompt.

These three short blocks inject retrieved context into Klukai's prompt:
- memories: episodic memories (from Qdrant)
- relationship facts: dossier-style facts about the Commander (from PG)
- recalled exchanges: actual past conversation pairs (from Qdrant)
"""

from __future__ import annotations


def build_memory_block(memories: list[str]) -> str:
    """Format retrieved episodic memories as operational records."""
    if not memories:
        return ""
    formatted = "\n".join(f"  - {m}" for m in memories)
    return f"OPERATIONAL RECORDS (relevant past interactions with the Commander):\n{formatted}"


def build_relationship_block(facts: dict) -> str:
    """Format relationship facts as Commander dossier."""
    if not facts:
        return ""
    lines = [f"  - {k}: {v}" for k, v in facts.items()]
    return "COMMANDER DOSSIER (what you know about your Commander):\n" + "\n".join(lines)


def build_conversation_recall_block(exchanges: list[dict]) -> str:
    """Format recalled past conversation exchanges for the prompt."""
    if not exchanges:
        return ""
    lines = ["RECALLED CONVERSATIONS (exact past exchanges with the Commander — reference naturally):"]
    for i, ex in enumerate(exchanges, 1):
        lines.append(f"  [{i}] Commander: {ex['user_content'][:200]}")
        lines.append(f"      Klukai: {ex['assistant_content'][:200]}")
        # Topics kept in payload but not shown to avoid unnatural output
    return "\n".join(lines)
