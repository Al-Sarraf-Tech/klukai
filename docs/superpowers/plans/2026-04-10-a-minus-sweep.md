# A- Sweep: Everything Below A- Gets Fixed

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Raise every component from B/C to A- by fixing the exact gaps identified in the 2026-04-10 session.

**Architecture:** Five independent workstreams — ops, architecture, tests, memory, personality — each producing working, testable improvements. No cross-dependencies between tasks.

**Tech Stack:** Python 3.13, FastAPI, pytest, Docker Compose, Flutter/Dart, PostgreSQL, LM Studio

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `docker-compose.yml` | Add TTS_HOME env var for XTTS model caching |
| Modify | `docker/voice/app/main.py` | Use TTS_HOME for model storage |
| Create | `docker/core/app/routes.py` | All HTTP endpoints extracted from main.py |
| Create | `docker/core/app/background.py` | All _background_* tasks extracted from main.py |
| Create | `docker/core/app/context.py` | Shared globals (ws, memory, affection, proactive, router) |
| Modify | `docker/core/app/main.py` | Slim: app setup, lifespan, WebSocket handler only |
| Modify | `docker/core/tests/conftest.py` | Add personality_config_path fixture |
| Modify | `docker/core/tests/test_kitchen_sink.py` | Use shared fixture, remove inline path logic |
| Modify | `docker/core/tests/test_e2e_websocket.py` | Use shared fixture, add real FastAPI E2E test |
| Modify | `docker/core/app/personality.py` | Config-driven level mapping, hot-reload support |
| Modify | `docker/core/app/proactive.py:598` | Fix brittle min/max level lookup |

---

## Task 1: Fix XTTS Model Re-Download (Ops C → A-)

**Files:**
- Modify: `docker-compose.yml:49-52`
- Modify: `docker/voice/app/main.py:25,49-52`

- [ ] **Step 1: Add TTS_HOME env var to docker-compose.yml**

In `docker-compose.yml`, add to companion-voice environment:

```yaml
  companion-voice:
    build: ./docker/voice
    container_name: companion-voice
    environment:
      TTS_ENGINE: "xtts"
      REFERENCE_WAV: "/app/reference/klukai_reference.wav"
      WHISPER_MODEL: "${WHISPER_MODEL:-base.en}"
      TTS_HOME: "/app/models/tts"
```

- [ ] **Step 2: Verify XTTS respects TTS_HOME**

The `TTS` library uses `TTS_HOME` env var (or `COQUI_TOS_AGREED` + home dir). Setting `TTS_HOME=/app/models/tts` puts the model on the `voice-models` volume at `/app/models/tts/`. Verify by checking the TTS library source.

- [ ] **Step 3: Rebuild voice container and verify model persists**

```bash
ssh wsl2 "cd ~/companion && docker compose build companion-voice && docker compose up -d companion-voice"
# Wait for healthy (~3 min first time)
ssh wsl2 "docker exec companion-voice ls -la /app/models/tts/ 2>/dev/null"
# Recreate and verify NO re-download
ssh wsl2 "cd ~/companion && docker compose up -d --force-recreate companion-voice"
# Should start in <30s instead of 3min
```

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml
git commit -m "fix: cache XTTS model on volume — eliminates 2GB re-download on restart"
```

---

## Task 2: Extract routes.py + background.py + context.py from main.py (Architecture C+ → A-)

**Files:**
- Create: `docker/core/app/context.py`
- Create: `docker/core/app/routes.py`
- Create: `docker/core/app/background.py`
- Modify: `docker/core/app/main.py`

- [ ] **Step 1: Create context.py — shared globals**

```python
"""Shared application context — global service instances.

All modules import from here instead of defining their own globals.
This enables clean module splitting without circular imports.
"""

from __future__ import annotations

from .affection import AffectionManager
from .llm_router import LLMRouter
from .mcp_client import MCPClient
from .memory import MemoryManager
from .proactive import ProactiveEngine
from .ws_manager import WSManager

memory = MemoryManager()
router = LLMRouter()
mcp = MCPClient()
ws = WSManager()
proactive = ProactiveEngine()
affection = AffectionManager()

SESSION_ID = "default"

COMPACT_THRESHOLD = 8
COMPACT_KEEP_RAW = 4

# Tracks the most recently generated memory_id for commander save/discard overrides
last_memory_id: str | None = None
```

- [ ] **Step 2: Create routes.py — all HTTP endpoints**

Move all `@app.get` and `@app.post` decorated functions from main.py (lines 215-560, ~350 lines) into `routes.py`. Import globals from `context.py`. Use a function `register_routes(app)` pattern:

```python
"""HTTP API routes — extracted from main.py."""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
# ... imports from context, memory_archive, etc.

def register_routes(app: FastAPI) -> None:
    """Register all HTTP routes on the FastAPI app."""

    @app.get("/health")
    async def health():
        # ... existing code ...

    @app.post("/api/tts")
    async def api_tts(req: dict):
        # ... existing code ...

    # ... all other routes ...
```

- [ ] **Step 3: Create background.py — all background tasks**

Move `_background_extraction`, `_background_compaction`, `_background_image_gen`, `_background_recall`, `_do_memory_keep` from main.py into `background.py`. Import globals from `context.py`.

```python
"""Background tasks — extraction, compaction, image gen, recall."""

from __future__ import annotations
import asyncio
import logging
from .context import ws, memory, affection, proactive, SESSION_ID
# ... other imports ...

async def background_extraction(user_msg, assistant_msg, session, user_id="default", ...):
    # ... existing _background_extraction code ...

async def background_compaction(session):
    # ... existing _background_compaction code ...
```

- [ ] **Step 4: Slim main.py — app setup, lifespan, WebSocket only**

main.py should contain ONLY:
- FastAPI app creation + lifespan
- WebSocket handler (`websocket_endpoint`, `_handle_message`, `_handle_voice`)
- Import and call `register_routes(app)`
- Static file mounting + root redirect

Target: main.py under 700 lines.

- [ ] **Step 5: Update imports in main.py**

Replace all `from .affection import ...` etc with `from .context import ...`. Call `register_routes(app)` after app creation.

- [ ] **Step 6: Run tests**

```bash
cd docker/core && python3 -m pytest tests/ -q --tb=short
```

Expected: 301 passed, same skips. Zero failures.

- [ ] **Step 7: Commit**

```bash
git add docker/core/app/context.py docker/core/app/routes.py docker/core/app/background.py docker/core/app/main.py
git commit -m "refactor: split main.py into routes.py, background.py, context.py — 1266→~650 lines"
```

---

## Task 3: Test Suite to A- (Tests B → A-)

**Files:**
- Modify: `docker/core/tests/conftest.py`
- Modify: `docker/core/tests/test_kitchen_sink.py`
- Modify: `docker/core/tests/test_e2e_websocket.py`

- [ ] **Step 1: Add personality_config_path fixture to conftest.py**

```python
@pytest.fixture
def personality_config_path():
    """Resolve personality.yaml path — works both in container and repo."""
    # Container path
    container = Path("/config/personality.yaml")
    if container.exists():
        return str(container)
    # Repo path
    repo = Path(__file__).resolve().parent.parent.parent / "config" / "personality.yaml"
    if repo.exists():
        return str(repo)
    pytest.skip("personality.yaml not found")


@pytest.fixture
def personality_config(personality_config_path):
    """Load and return the personality config dict."""
    from app.personality import load_personality, reload_personality
    return reload_personality(personality_config_path)
```

- [ ] **Step 2: Update test_kitchen_sink.py to use shared fixture**

Replace all inline `Path(__file__).resolve().parent.parent.parent / "config"` logic with the `personality_config` fixture.

- [ ] **Step 3: Update test_e2e_websocket.py to use shared fixture**

Same replacement. Remove all `_load_p()` helper methods.

- [ ] **Step 4: Add true E2E test using FastAPI TestClient**

```python
from fastapi.testclient import TestClient

class TestFastAPIEndpoints:
    """Test actual HTTP endpoints with mocked services."""

    @pytest.fixture
    def client(self, mock_services):
        from app.main import app
        return TestClient(app)

    def test_health_endpoint(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_affection_endpoint(self, client):
        r = client.get("/api/affection")
        assert r.status_code == 200
        data = r.json()
        assert "score" in data
        assert "level" in data
```

- [ ] **Step 5: Run tests, verify zero skips from personality.yaml path**

```bash
cd docker/core && python3 -m pytest tests/ -v --tb=short 2>&1 | grep -c "SKIPPED"
```

Target: skips drop from 27 to ~10 (only psycopg-dependent ones remain).

- [ ] **Step 6: Commit**

```bash
git add docker/core/tests/
git commit -m "test: shared personality fixture, E2E FastAPI tests — skips 27→~10"
```

---

## Task 4: Memory Archive to A- (Memory B → A-)

**Files:**
- Create: `docker/core/reannotate_existing.py` (one-shot script, not permanent)
- Modify: `docker/core/app/memory_archive.py`

- [ ] **Step 1: Write a one-shot script to re-annotate tag-based memories with conversation text**

The existing 170 memories were annotated from scene tags, not conversation text. The seeder now uses conversation text for NEW memories but the old ones are still tag-based.

Script approach:
1. For each memory with `conversation_id = 'seed'`, find the original exchange in `companion_messages` by matching `created_at` timestamp (within a window)
2. Re-annotate with dolphin-24b using the actual exchange text
3. Only update if the new annotation is higher quality (not repetitive, not leaked COT)

- [ ] **Step 2: Add annotation quality scoring to memory_archive.py**

```python
def annotation_quality_score(text: str) -> float:
    """Score 0-1 based on specificity, variety, and character voice."""
    score = 1.0
    if text.lower().startswith("whisper"):
        score -= 0.3
    if "intertwined" in text.lower() or "sanctuary" in text.lower():
        score -= 0.2
    if len(text) < 30:
        score -= 0.3
    if len(text) > 300:
        score -= 0.1
    if text.startswith("We need") or text.startswith("The user"):
        score = 0.0  # Leaked COT
    return max(0, score)
```

- [ ] **Step 3: Run the re-annotation script**

```bash
# Run during off-peak (3-6 AM) when dolphin isn't serving chat
docker exec companion-core python3 /app/reannotate_existing.py
```

- [ ] **Step 4: Verify quality improvement**

```sql
SELECT ROUND(AVG(LENGTH(annotation))) AS avg_len,
  COUNT(*) FILTER (WHERE annotation ILIKE 'whisper%') AS repetitive
FROM companion_memories;
```

Target: 0 repetitive, avg_len 80-200.

- [ ] **Step 5: Commit**

```bash
git add docker/core/app/memory_archive.py
git commit -m "feat: annotation quality scoring for memory archive"
```

---

## Task 5: Personality System to A- (Personality B+ → A-)

**Files:**
- Modify: `docker/core/app/personality.py`
- Modify: `docker/core/app/proactive.py:598`
- Modify: `config/personality.yaml`

- [ ] **Step 1: Fix proactive.py brittle level lookup**

Line 598 in proactive.py:
```python
# BEFORE (brittle):
level = min(self._affection_level, max(messages.keys()))

# AFTER (safe):
available = sorted(messages.keys())
level = max(k for k in available if k <= self._affection_level) if available else 0
```

- [ ] **Step 2: Add hot-reload capability to personality.py**

```python
import time

_PERSONALITY: dict | None = None
_PERSONALITY_MTIME: float = 0
_PERSONALITY_PATH: str = ""

def load_personality(path: str | None = None) -> dict:
    """Load personality config, auto-reload if file changed."""
    global _PERSONALITY, _PERSONALITY_MTIME, _PERSONALITY_PATH
    path = path or os.environ.get("PERSONALITY_PATH", "/config/personality.yaml")

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0

    if _PERSONALITY is not None and path == _PERSONALITY_PATH and mtime == _PERSONALITY_MTIME:
        return _PERSONALITY

    with open(path) as f:
        _PERSONALITY = yaml.safe_load(f)
    _PERSONALITY_MTIME = mtime
    _PERSONALITY_PATH = path
    return _PERSONALITY
```

This checks file mtime on every call (negligible cost) and reloads if the file changed. No more container restarts for YAML edits.

- [ ] **Step 3: Add stronger few-shot examples to level_4_bonded speech pattern**

In personality.yaml, add more explicit guidance for dolphin at high affection:

```yaml
  level_4_bonded:
    name: "Bonded"
    anti_patterns:
      - "NEVER respond to 'I love you' with doubt, deflection, or 'prove it'"
      - "NEVER use 'Hmph' or tsundere deflection at this level"
      - "NEVER question the Commander's sincerity"
    examples:
      # Add 3-4 more examples showing direct warmth
      - "...Say it again. (I pull you closer) I need to hear it."
      - "(I press my forehead to yours) ...Idiot. You already know."
      - "You waited for me. Ten years. ...I'm not letting go either."
```

- [ ] **Step 4: Run tests**

```bash
cd docker/core && python3 -m pytest tests/ -q --tb=short
```

- [ ] **Step 5: Rebuild and verify personality loads correctly**

```bash
rsync -avz config/personality.yaml wsl2:~/companion/config/personality.yaml
ssh wsl2 "cd ~/companion && docker compose build companion-core && docker compose up -d companion-core"
# Verify hot-reload: edit YAML, check if change takes effect without restart
```

- [ ] **Step 6: Commit**

```bash
git add docker/core/app/personality.py docker/core/app/proactive.py config/personality.yaml
git commit -m "feat: personality hot-reload, anti-deflection patterns, safe level lookups"
```

---

## Execution Order

Tasks are independent. Recommended order for maximum impact:

1. **Task 1** (Ops) — 5 min, eliminates 3-min restart penalty for all subsequent work
2. **Task 5** (Personality) — 15 min, hot-reload means faster iteration on all remaining tasks
3. **Task 2** (Architecture) — 30 min, largest refactor, needs careful testing
4. **Task 3** (Tests) — 15 min, validates everything else
5. **Task 4** (Memory) — 20 min, needs dolphin availability (run overnight)

Total estimate: ~90 min of implementation + testing.
