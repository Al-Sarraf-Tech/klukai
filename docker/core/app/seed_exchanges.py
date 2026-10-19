"""How the memory seeder and the reannotator read chat rows as exchanges.

Rows are ``(id, role, content, created_at)`` in ``(created_at, id)`` order.
Messages that share a timestamp have no recorded order (ids are random
UUIDs), so rows are handled one timestamp group at a time: a reply answers
the newest unanswered user message from an *earlier* group, never one at its
own timestamp. The pairing therefore cannot depend on how a tie happens to
sort.

Each exchange carries a ``fingerprint`` (``seed:<user message id>``, unique
even when timestamps tie) and the ``mark`` a run may stop after: every row
up to the reply's timestamp is settled except, possibly, the newest user
message at that timestamp, which may still be answered later and is
``carry``-ed into the next run.
"""

from __future__ import annotations

from datetime import datetime
from itertools import groupby
from typing import Any

SEED_FINGERPRINT_PREFIX = "seed:"

Row = tuple[Any, str, str, datetime]


def fingerprint(user_message_id: Any) -> str:
    """The seed memory's conversation_id for the exchange this message opens."""
    return f"{SEED_FINGERPRINT_PREFIX}{user_message_id}"


def pair_exchanges(rows: list[Row]) -> list[dict[str, Any]]:
    """Pair rows into user -> assistant exchanges, one timestamp group at a time.

    An unanswered user message is answered by the first reply (lowest id) in
    the next group that has one. Further replies with no question waiting
    are passed over; a user message followed by another before any reply is
    left unanswered, as before. Each exchange's ``mark`` is
    ``(reply timestamp, carry)``, where ``carry`` is the id of the newest user
    message at that timestamp (still waiting for its reply) or None.
    """
    exchanges: list[dict[str, Any]] = []
    pending: Row | None = None
    for ts, group_iter in groupby(rows, key=lambda r: r[3]):
        group = list(group_iter)
        replies = [r for r in group if r[1] == "assistant"]
        questions = [r for r in group if r[1] == "user"]
        newest_question = questions[-1] if questions else None
        if pending is not None and replies:
            reply = replies[0]
            exchanges.append(
                {
                    "user": (pending[2] or "")[:500],
                    "assistant": (reply[2] or "")[:500],
                    "created_at": pending[3].isoformat() if pending[3] else "",
                    "fingerprint": fingerprint(pending[0]),
                    "mark": (
                        ts,
                        str(newest_question[0])
                        if newest_question is not None
                        else None,
                    ),
                }
            )
            pending = None
        if newest_question is not None:
            pending = newest_question
    return exchanges
