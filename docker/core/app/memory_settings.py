"""Settings shared by exchange storage and its manager; no runtime state."""

import os

EMBED_DIM = 768
MSG_COLLECTION_NAME = "companion_exchanges"
MSG_RECALL_LIMIT = 5
MSG_MIN_SCORE = 0.35
RECENCY_WEIGHT = 0.15
QDRANT_URL = os.environ.get("QDRANT_URL", "http://aichat-vector:6333")
