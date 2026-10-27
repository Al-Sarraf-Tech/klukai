"""Read Amarillo's bounded, non-sensitive gaming heartbeat without an LLM."""

import json
import time
from pathlib import Path


def game_active(path: str) -> bool:
    try:
        body = json.loads(Path(path).read_text())
        if not isinstance(body, dict):
            return False
        age = time.time() - float(body["updated_at"])
        return 0 <= age <= 45 and body.get("game_active") is True
    except (OSError, ValueError, TypeError, KeyError):
        return False
