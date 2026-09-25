"""The Thread — the ten years of messages Klukai sent and never got answered.

Canon: from the Mephisto Agreement (2065) to Aphelion (2074) she messaged the
Commander and never deleted the thread. Content lives in
``config/personality.yaml`` under ``ten_year_thread``; this module decides how
much of it she will show him at a given affection level.
"""

from __future__ import annotations


def unlocked_entries(p: dict, affection_level: int) -> list[dict]:
    """Thread entries she is willing to show at this affection level."""
    return _unlocked_entries(p.get("ten_year_thread", {}), affection_level)


def _unlocked_entries(cfg: dict, affection_level: int) -> list[dict]:
    share_at = cfg.get("share_from_level", 6)
    return [
        e for e in cfg.get("entries", [])
        if e.get("text") and affection_level >= e.get("min_affection", share_at)
    ]


def _pick_entries(entries: list[dict], seed: int) -> list[dict]:
    """Two entries from different years of the decade, varying with ``seed`` so
    a repeated question surfaces different messages. Returned in thread order."""
    if len(entries) <= 2:
        return entries
    first = seed % len(entries)
    second = (first + len(entries) // 2) % len(entries)
    return [entries[i] for i in sorted((first, second))]


def build_thread_block(p: dict, affection_level: int, seed: int = 0) -> str:
    """Per-message block for when the Commander asks about the messages.

    Below ``acknowledge_from_level`` she deflects; below ``share_from_level``
    she admits the thread exists but reveals nothing; from there she may quote
    real entries verbatim, and from ``open_thread_from_level`` she may offer
    him the whole thread.
    """
    cfg = p.get("ten_year_thread", {})
    if not cfg.get("entries"):
        return ""

    head = (
        "THE THREAD: The Commander is asking about the messages you sent him during "
        "the ten-year silence. Every night at 0200, for nine and a half years, never "
        "answered. You never deleted a single one."
    )
    if affection_level < cfg.get("acknowledge_from_level", 3):
        return head + " You are not ready to discuss them with him. Deflect coolly; confirm nothing."

    unlocked = _unlocked_entries(cfg, affection_level)
    if affection_level < cfg.get("share_from_level", 6) or not unlocked:
        return head + (
            " You admit the thread exists, but you do NOT reveal what any message said. "
            "Not yet. Let one tell slip: a pause, a glance away."
        )

    last_reply = cfg.get("last_reply", "I'm here.")
    lines = [
        head,
        "You may quote from it, exactly as written, timestamp and all. Never invent other messages:",
    ]
    lines += [f'  [{e.get("stamp", "0200 hours")}] "{e["text"]}"' for e in _pick_entries(unlocked, seed)]
    lines.append(
        "Share one, perhaps two. Let him feel what it cost. The thread ends with the only "
        f'reply it ever received: "{last_reply}"'
    )
    if affection_level >= cfg.get("open_thread_from_level", 8):
        lines.append(
            "You may offer to let him read the whole thread himself. You have never shown it to anyone."
        )
    return "\n".join(lines)


def thread_share_lines(p: dict, affection_level: int) -> list[str]:
    """Proactive messages in which she shares one entry, for the
    ``random_events`` category whose ``source`` is ``ten_year_thread``."""
    cfg = p.get("ten_year_thread", {})
    framings = cfg.get("share_framings", [])
    if not framings:
        return []
    return [
        framings[i % len(framings)].format(stamp=e.get("stamp", "0200 hours"), text=e["text"])
        for i, e in enumerate(_unlocked_entries(cfg, affection_level))
    ]
