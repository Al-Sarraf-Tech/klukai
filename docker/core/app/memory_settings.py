"""Settings shared by memory storage, recall and its manager; no runtime state."""

MSG_RECALL_LIMIT = 5
RECENCY_WEIGHT = 0.15

# Recall (owner decision 2026-10-26: cloud AI only, no embeddings). Postgres
# returns at most this many candidates per search; Claude Haiku picks from them.
RECALL_CANDIDATES = 40
# Recall runs before her reply is written. A slow or queued Haiku selection is
# abandoned after this long and the Postgres ranking is used instead.
RECALL_SELECTION_TIMEOUT_S = 10.0
