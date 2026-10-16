"""Endpoints shared by memory adapters, without importing manager state."""

import os

DATA_URL = os.environ.get("DATA_URL", "http://aichat-data:8091")
