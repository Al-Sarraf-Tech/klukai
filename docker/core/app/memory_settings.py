"""Settings shared by memory storage, recall and its manager; no runtime state."""

MSG_RECALL_LIMIT = 5
RECENCY_WEIGHT = 0.15

# Recall (owner decision 2026-10-26: cloud AI only, no embeddings). Postgres
# returns at most this many candidates per search. The chat reply uses that
# order immediately. A Grok pick, when one is still run, is not on the reply
# path: it precomputes the next turn and may take longer than this budget.
RECALL_CANDIDATES = 40
# Callers that still wait for a Grok selection (not the chat reply) abandon
# it after this long and keep the Postgres ranking.
RECALL_SELECTION_TIMEOUT_S = 2.5
# After the reply, one Grok pick may run this long. It cannot delay the turn.
RECALL_PRECOMPUTE_TIMEOUT_S = 12.0
