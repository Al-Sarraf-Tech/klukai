# Memory Archive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Klukai's character-driven memory archive — image persistence, LLM curation, mood-based recall, and a responsive timeline/scrapbook UI.

**Architecture:** Images saved to Docker volume on dominus, metadata in PostgreSQL on amarillo. Curation runs on qwen2.5-3b (Intel Arc A380 via LM Studio remote device). Flutter PWA gets a new Memory Archive screen with sidebar categories (desktop) and tab categories (mobile). Final task seeds the archive by having Klukai review conversation history.

**Tech Stack:** Python 3.13 / FastAPI (backend), PostgreSQL (metadata), Docker volume (images), Pillow (thumbnails), Flutter/Dart (frontend), qwen2.5-3b (curation LLM)

**Spec:** `docs/superpowers/specs/2026-04-09-memory-archive-design.md`

**Testing:** Full regression + E2E + metadata validation after each backend task. No chat alteration — all tests use separate API endpoints and dedicated test users.

---

## File Map

### New Files
| File | Responsibility |
|------|---------------|
| `docker/core/migrations/060_memory_archive.sql` | DB table + indexes |
| `docker/core/app/memory_archive.py` | Archive CRUD, recall logic, thumbnail gen, save/discard |
| `flutter_app/lib/models/memory.dart` | Memory data model |
| `flutter_app/lib/services/memory_service.dart` | API client for memories |
| `flutter_app/lib/screens/memory_archive_screen.dart` | Full archive UI (responsive) |
| `flutter_app/lib/widgets/memory_timeline_entry.dart` | Single timeline entry widget |

### Modified Files
| File | Changes |
|------|---------|
| `docker-compose.yml` | Add `companion-images` volume + mount |
| `docker/core/requirements.txt` | Add `Pillow` for thumbnail generation |
| `docker/core/app/main.py` | API endpoints, recall detection, image save hook, commander override |
| `docker/core/app/fact_extractor.py` | Add curation fields to extraction prompt |
| `docker/core/app/image_gen.py` | Save PNG to volume after generation |
| `flutter_app/lib/screens/chat_screen.dart` | Archive navigation icon, image bubble download overlay |
| `flutter_app/lib/widgets/message_bubble.dart` | Download/save icon on images |

---

## Task 1: Database Migration + Docker Volume

**Files:**
- Create: `docker/core/migrations/060_memory_archive.sql`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Write the migration**

```sql
-- 060_memory_archive.sql
CREATE TABLE IF NOT EXISTS companion_memories (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filename        TEXT NOT NULL,
    thumb_filename  TEXT,
    prompt          TEXT,
    annotation      TEXT,
    scene_tags      TEXT[] DEFAULT '{}',
    mood            TEXT,
    affection_level INT,
    kept            BOOLEAN DEFAULT true,
    kept_by         TEXT DEFAULT 'klukai',
    category        TEXT DEFAULT 'Mission Records',
    conversation_id TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memories_kept ON companion_memories(kept) WHERE kept = true;
CREATE INDEX IF NOT EXISTS idx_memories_category ON companion_memories(category) WHERE kept = true;
CREATE INDEX IF NOT EXISTS idx_memories_tags ON companion_memories USING gin(scene_tags) WHERE kept = true;
CREATE INDEX IF NOT EXISTS idx_memories_created ON companion_memories(created_at DESC) WHERE kept = true;
```

- [ ] **Step 2: Add volume to docker-compose.yml**

Add `companion-images` volume and mount it in companion-core:

```yaml
# Under companion-core service volumes:
      - companion-images:/images

# Under top-level volumes:
  companion-images:
```

- [ ] **Step 3: Add Pillow to requirements.txt**

Append `Pillow>=10.0` to `docker/core/requirements.txt`

- [ ] **Step 4: Sync, rebuild, verify migration runs**

```bash
rsync -avz --exclude .git --exclude flutter_app/.dart_tool --exclude flutter_app/build --exclude __pycache__ --exclude tgbot/.venv ~/git/companion/ wsl2:~/companion/
ssh wsl2 "cd ~/companion && docker compose build companion-core && docker compose up -d companion-core"
```

Verify:
```bash
ssh wsl2 "docker exec companion-core python3 -c \"
import asyncio, psycopg, os
async def check():
    async with await psycopg.AsyncConnection.connect(os.environ['DATABASE_URL']) as c:
        await (await c.execute('SELECT count(*) FROM companion_memories')).fetchone()
        print('TABLE OK')
asyncio.run(check())
\""
```

Expected: `TABLE OK`

Verify volume mount:
```bash
ssh wsl2 "docker exec companion-core ls -la /images/"
```

Expected: empty directory listing

- [ ] **Step 5: Commit**

```bash
git add docker/core/migrations/060_memory_archive.sql docker-compose.yml docker/core/requirements.txt
git commit -m "feat(memories): add companion_memories table and image volume"
```

---

## Task 2: Backend — memory_archive.py

**Files:**
- Create: `docker/core/app/memory_archive.py`

- [ ] **Step 1: Write the memory archive module**

```python
"""Memory archive: Klukai's curated image collection."""

from __future__ import annotations

import logging
import os
import random
import uuid
from pathlib import Path

from PIL import Image

from .db import get_conn, get_conn_autocommit

logger = logging.getLogger(__name__)

IMAGES_DIR = Path(os.environ.get("IMAGES_DIR", "/images"))

# Affection-gated categories
CATEGORIES_BY_LEVEL = {
    0: ["Tactical Operations", "Mission Records", "Squad Moments"],
    3: ["The Commander", "Quiet Hours"],
    6: ["Precious Memories"],
}

# Mood-to-category affinity for vague recall
MOOD_CATEGORY_WEIGHTS = {
    "tender": {"Precious Memories": 5, "The Commander": 3, "Quiet Hours": 2},
    "longing": {"Precious Memories": 4, "The Commander": 3, "Quiet Hours": 2},
    "nostalgic": {"Precious Memories": 3, "The Commander": 3, "Squad Moments": 2},
    "affectionate": {"Precious Memories": 5, "The Commander": 4},
    "devoted": {"Precious Memories": 5, "The Commander": 3},
    "melancholic": {"Precious Memories": 3, "Quiet Hours": 3, "Squad Moments": 2},
    "composed": {"Mission Records": 3, "Tactical Operations": 2, "The Commander": 1},
    "battle_ready": {"Tactical Operations": 5, "Mission Records": 3},
    "protective": {"Squad Moments": 4, "The Commander": 3},
    "playful": {"Quiet Hours": 4, "The Commander": 3, "Squad Moments": 2},
}


def available_categories(affection_level: int) -> list[str]:
    """Return categories available at the given affection level."""
    cats = []
    for min_level, level_cats in sorted(CATEGORIES_BY_LEVEL.items()):
        if affection_level >= min_level:
            cats.extend(level_cats)
    return cats


async def save_image(
    image_bytes: bytes,
    prompt: str,
    conversation_id: str,
    mood: str = "composed",
    affection_level: int = 0,
    curation: dict | None = None,
) -> str | None:
    """Save a generated image to the volume and create a metadata row.

    Args:
        image_bytes: Raw PNG bytes.
        prompt: The ComfyUI prompt used.
        conversation_id: FK to conversation.
        mood: Current mood at time of generation.
        affection_level: Current affection level.
        curation: Optional dict from LLM with {keep, annotation, category, image_tags}.

    Returns:
        Memory ID (UUID string) or None on failure.
    """
    try:
        memory_id = str(uuid.uuid4())
        filename = f"{memory_id}.png"
        thumb_filename = f"{memory_id}_thumb.png"

        # Save full image
        img_path = IMAGES_DIR / filename
        img_path.write_bytes(image_bytes)

        # Generate thumbnail (320px wide)
        thumb_path = IMAGES_DIR / thumb_filename
        _generate_thumbnail(img_path, thumb_path, width=320)

        # Extract curation data
        kept = True
        kept_by = "klukai"
        annotation = None
        category = "Mission Records"
        scene_tags: list[str] = []

        if curation:
            kept = curation.get("keep", True)
            annotation = curation.get("annotation")
            category = curation.get("category", "Mission Records")
            scene_tags = curation.get("image_tags", [])
            # Validate category against affection level
            valid = available_categories(affection_level)
            if category not in valid:
                category = valid[-1] if valid else "Mission Records"

        # Store metadata
        async with get_conn_autocommit() as conn:
            await conn.execute(
                "INSERT INTO companion_memories "
                "(id, filename, thumb_filename, prompt, annotation, scene_tags, "
                "mood, affection_level, kept, kept_by, category, conversation_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    memory_id, filename, thumb_filename, prompt, annotation,
                    scene_tags, mood, affection_level, kept, kept_by,
                    category, conversation_id,
                ),
            )

        logger.info(
            "Memory saved: %s (kept=%s, category=%s, tags=%s)",
            memory_id, kept, category, scene_tags,
        )
        return memory_id

    except Exception as e:
        logger.error("Failed to save memory: %s", e)
        return None


def _generate_thumbnail(src: Path, dst: Path, width: int = 320) -> None:
    """Generate a thumbnail with the given width, preserving aspect ratio."""
    try:
        with Image.open(src) as img:
            ratio = width / img.width
            height = int(img.height * ratio)
            thumb = img.resize((width, height), Image.LANCZOS)
            thumb.save(dst, "PNG", optimize=True)
    except Exception as e:
        logger.warning("Thumbnail generation failed: %s", e)


async def get_image_bytes(memory_id: str, thumbnail: bool = False) -> bytes | None:
    """Read image bytes from the volume."""
    try:
        async with get_conn() as conn:
            col = "thumb_filename" if thumbnail else "filename"
            row = await (await conn.execute(
                f"SELECT {col} FROM companion_memories WHERE id = %s", (memory_id,)
            )).fetchone()
            if not row or not row[0]:
                return None
            path = IMAGES_DIR / row[0]
            if path.exists():
                return path.read_bytes()
    except Exception as e:
        logger.error("Failed to read image %s: %s", memory_id, e)
    return None


async def list_memories(
    category: str | None = None,
    limit: int = 20,
    before: str | None = None,
) -> list[dict]:
    """List kept memories, optionally filtered by category."""
    try:
        async with get_conn() as conn:
            conditions = ["kept = true"]
            params: list = []

            if category:
                conditions.append("category = %s")
                params.append(category)
            if before:
                conditions.append("created_at < %s")
                params.append(before)

            where = " AND ".join(conditions)
            params.append(limit)

            rows = await (await conn.execute(
                f"SELECT id, filename, thumb_filename, annotation, scene_tags, "
                f"mood, affection_level, kept_by, category, created_at "
                f"FROM companion_memories WHERE {where} "
                f"ORDER BY created_at DESC LIMIT %s",
                tuple(params),
            )).fetchall()

            return [
                {
                    "id": str(r[0]),
                    "filename": r[1],
                    "thumb_filename": r[2],
                    "annotation": r[3],
                    "scene_tags": r[4] or [],
                    "mood": r[5],
                    "affection_level": r[6],
                    "kept_by": r[7],
                    "category": r[8],
                    "created_at": r[9].isoformat() if r[9] else None,
                }
                for r in rows
            ]
    except Exception as e:
        logger.error("Failed to list memories: %s", e)
        return []


async def get_categories(affection_level: int) -> list[dict]:
    """Return available categories with memory counts."""
    try:
        valid = available_categories(affection_level)
        async with get_conn() as conn:
            rows = await (await conn.execute(
                "SELECT category, count(*) FROM companion_memories "
                "WHERE kept = true GROUP BY category"
            )).fetchall()

            counts = {r[0]: r[1] for r in rows}
            total = sum(counts.get(c, 0) for c in valid)
            result = [{"name": c, "count": counts.get(c, 0)} for c in valid]
            result.append({"name": "All", "count": total})
            return result
    except Exception as e:
        logger.error("Failed to get categories: %s", e)
        return []


async def update_kept(memory_id: str, kept: bool, kept_by: str = "commander") -> bool:
    """Commander saves or discards a memory."""
    try:
        async with get_conn_autocommit() as conn:
            await conn.execute(
                "UPDATE companion_memories SET kept = %s, kept_by = %s WHERE id = %s",
                (kept, kept_by, memory_id),
            )
        return True
    except Exception as e:
        logger.error("Failed to update memory %s: %s", memory_id, e)
        return False


async def recall_memory(
    query: str | None,
    mood: str,
    affection_level: int,
) -> dict | None:
    """Recall a memory from the archive.

    Specific query: search scene_tags + annotation text.
    Vague/none: mood-weighted random from kept memories.
    """
    try:
        async with get_conn() as conn:
            if query:
                # Extract search terms
                terms = [w.lower().strip() for w in query.split() if len(w) > 2]
                # Search tags first
                for term in terms:
                    rows = await (await conn.execute(
                        "SELECT id, filename, annotation, category, scene_tags, created_at "
                        "FROM companion_memories "
                        "WHERE kept = true AND %s = ANY(scene_tags) "
                        "ORDER BY created_at DESC LIMIT 1",
                        (term,),
                    )).fetchall()
                    if rows:
                        r = rows[0]
                        return _row_to_dict(r)

                # Fallback: search annotation text
                for term in terms:
                    rows = await (await conn.execute(
                        "SELECT id, filename, annotation, category, scene_tags, created_at "
                        "FROM companion_memories "
                        "WHERE kept = true AND annotation ILIKE %s "
                        "ORDER BY created_at DESC LIMIT 1",
                        (f"%{term}%",),
                    )).fetchall()
                    if rows:
                        return _row_to_dict(rows[0])

            # Vague query or no results: mood-weighted random
            valid_cats = available_categories(affection_level)
            weights = MOOD_CATEGORY_WEIGHTS.get(mood, {})

            # Build weighted category list
            weighted: list[tuple[str, int]] = []
            for cat in valid_cats:
                w = weights.get(cat, 1)
                weighted.append((cat, w))

            if not weighted:
                return None

            # Weighted random category selection
            chosen_cat = random.choices(
                [c for c, _ in weighted],
                weights=[w for _, w in weighted],
                k=1,
            )[0]

            rows = await (await conn.execute(
                "SELECT id, filename, annotation, category, scene_tags, created_at "
                "FROM companion_memories "
                "WHERE kept = true AND category = %s "
                "ORDER BY random() LIMIT 1",
                (chosen_cat,),
            )).fetchall()

            if rows:
                return _row_to_dict(rows[0])

            # Fallback: any kept memory
            rows = await (await conn.execute(
                "SELECT id, filename, annotation, category, scene_tags, created_at "
                "FROM companion_memories WHERE kept = true "
                "ORDER BY random() LIMIT 1"
            )).fetchall()
            return _row_to_dict(rows[0]) if rows else None

    except Exception as e:
        logger.error("Memory recall failed: %s", e)
        return None


def _row_to_dict(r) -> dict:
    return {
        "id": str(r[0]),
        "filename": r[1],
        "annotation": r[2],
        "category": r[3],
        "scene_tags": r[4] or [],
        "created_at": r[5].isoformat() if r[5] else None,
    }
```

- [ ] **Step 2: Verify module imports cleanly**

```bash
ssh wsl2 "docker exec companion-core python3 -c 'from app.memory_archive import save_image, recall_memory, list_memories, get_categories, available_categories; print(\"imports OK\"); print(\"Categories at lv8:\", available_categories(8))'"
```

Expected: `imports OK` and all 6 categories listed

- [ ] **Step 3: Commit**

```bash
git add docker/core/app/memory_archive.py
git commit -m "feat(memories): add memory archive module — CRUD, recall, thumbnails"
```

---

## Task 3: Backend — Image Save Hook + Curation Prompt

**Files:**
- Modify: `docker/core/app/image_gen.py`
- Modify: `docker/core/app/fact_extractor.py`
- Modify: `docker/core/app/main.py`

- [ ] **Step 1: Modify image_gen.py to return bytes AND save to volume**

In `_background_image_gen` in `main.py`, after `generate_image()` returns bytes, call `memory_archive.save_image()`. The image_gen module itself stays unchanged — it returns bytes. The save happens in main.py's background task.

- [ ] **Step 2: Add curation fields to fact_extractor.py**

Add an `image_generated` parameter to `extract_facts()`. When true, append the memory curation section to the prompt and return additional `memory_curation` field in the result.

The curation addition to `FACT_EXTRACTION_PROMPT`:

```python
IMAGE_CURATION_ADDENDUM = """

An image was generated during this exchange. Evaluate it for Klukai's memory archive:
- "keep": true/false — would Klukai consider this moment worth preserving?
- "annotation": 1-2 sentence caption as Klukai (first person, in character)
- "category": one of: {categories}
- "image_tags": list of scene/setting keywords for search
"""
```

- [ ] **Step 3: Modify _background_image_gen in main.py**

After image gen succeeds:
1. Save PNG to volume via `memory_archive.save_image()`
2. Pass `image_generated=True` to `_background_extraction` so curation runs
3. After extraction returns curation data, update the memory row

- [ ] **Step 4: Modify _background_extraction in main.py**

Accept `image_generated` flag + `memory_id`. When set, pass to `extract_facts(image_generated=True)` and then update the memory row with curation results via `memory_archive.update_curation()`.

- [ ] **Step 5: Add recall detection + commander override to main.py**

Add recall signal keywords (checked BEFORE image gen):
```python
RECALL_KEYWORDS = [
    "show me a memory", "remember when", "that time we", "do you remember",
    "show me something", "recall", "our memories", "your memories",
]
```

Add commander override keywords (checked after image gen):
```python
SAVE_KEYWORDS = ["save that", "keep this", "keep that", "save this"]
DISCARD_KEYWORDS = ["delete that", "remove this", "discard that", "forget that"]
```

- [ ] **Step 6: Add memory API endpoints to main.py**

```python
@app.get("/api/memories")
@app.get("/api/memories/{memory_id}")
@app.get("/api/memories/{memory_id}/image")
@app.get("/api/memories/{memory_id}/thumbnail")
@app.post("/api/memories/{memory_id}/keep")
@app.post("/api/memories/{memory_id}/discard")
@app.get("/api/memories/categories")
```

- [ ] **Step 7: Sync, rebuild, verify endpoints**

```bash
rsync + rebuild + restart
curl -sf http://100.111.198.19:8300/api/memories | python3 -m json.tool
curl -sf http://100.111.198.19:8300/api/memories/categories | python3 -m json.tool
```

- [ ] **Step 8: Commit**

```bash
git add docker/core/app/memory_archive.py docker/core/app/main.py docker/core/app/fact_extractor.py docker/core/app/image_gen.py
git commit -m "feat(memories): image persistence, curation pipeline, recall, API endpoints"
```

---

## Task 4: Backend — Regression Test + E2E Validation

**No code changes — validation only. Does not touch chat.**

- [ ] **Step 1: Run full infrastructure regression**

Verify all existing systems still work after the memory archive changes:
- Service health (core, LM Studio, ComfyUI, MCP)
- Model metadata (all models present, --lowvram removed)
- Affection state (score unchanged from user's real state)
- LLM inference (qwen2.5-3b, dolphin-24b)
- Voice (TTS internal)
- WebSocket chat (two test users — NOT "default")
- Image generation + VRAM recovery

- [ ] **Step 2: E2E memory archive test**

Test the full memory pipeline end-to-end:
1. Generate an image via `/api/generate-image`
2. Verify image saved to volume (`/images/*.png` exists)
3. Verify thumbnail generated (`/images/*_thumb.png` exists)
4. Verify metadata row in `companion_memories`
5. Test `/api/memories` returns the new memory
6. Test `/api/memories/{id}/image` serves the PNG
7. Test `/api/memories/{id}/thumbnail` serves the thumbnail
8. Test `/api/memories/categories` returns categories with counts
9. Test commander keep/discard endpoints
10. Test recall via `recall_memory()` function

- [ ] **Step 3: Metadata validation**

Verify:
- `companion_memories` table exists with correct schema
- Indexes exist (kept, category, tags, created_at)
- Volume mount at `/images` is writable
- Pillow is importable
- `memory_archive` module imports cleanly with all functions

- [ ] **Step 4: Verify no chat alteration**

Check the user's real affection state and message count haven't changed:
```bash
# Affection should still be score=838, level=8, Bonded
curl -sf http://100.111.198.19:8300/api/affection
```

---

## Task 5: Flutter — Memory Model + Service

**Files:**
- Create: `flutter_app/lib/models/memory.dart`
- Create: `flutter_app/lib/services/memory_service.dart`

- [ ] **Step 1: Write Memory model**

```dart
class Memory {
  final String id;
  final String? annotation;
  final List<String> sceneTags;
  final String? mood;
  final int? affectionLevel;
  final String keptBy;
  final String category;
  final DateTime createdAt;
  final String? thumbUrl;
  final String? imageUrl;

  // constructor, fromJson, copyWith
}
```

- [ ] **Step 2: Write MemoryService**

API client with methods:
- `fetchMemories({category, limit, before})` → `List<Memory>`
- `fetchCategories()` → `List<{name, count}>`
- `keepMemory(id)` / `discardMemory(id)`
- `imageUrl(id)` / `thumbnailUrl(id)` — URL builders

- [ ] **Step 3: Commit**

---

## Task 6: Flutter — Memory Archive Screen (Responsive)

**Files:**
- Create: `flutter_app/lib/screens/memory_archive_screen.dart`
- Create: `flutter_app/lib/widgets/memory_timeline_entry.dart`

- [ ] **Step 1: Write MemoryTimelineEntry widget**

Displays one memory in the timeline: thumbnail, annotation, scene tags, saved-by badge, timestamp. Color-coded dot (pink=Klukai, cyan=Commander).

- [ ] **Step 2: Write MemoryArchiveScreen**

Responsive layout using `LayoutBuilder`:
- **>600px (desktop):** Row with sidebar (categories + stats) and timeline ListView
- **≤600px (mobile):** Column with horizontal scrollable category tabs and timeline ListView

Both layouts use the same `MemoryTimelineEntry` widget with slightly different thumbnail sizes.

Desktop sidebar shows: category list with counts, archive stats (total kept/discarded), affection level badge.

Mobile tabs: horizontal scroll, active tab highlighted with pink accent.

Tap on thumbnail opens full-screen image overlay with download button.

GFL2 color scheme: `#12151E` background, `#1A1F2E` surface, `#4FC3F7` primary, `#E88CA5` affinity, `#E8923E` accent.

- [ ] **Step 3: Commit**

---

## Task 7: Flutter — Chat Integration

**Files:**
- Modify: `flutter_app/lib/screens/chat_screen.dart`
- Modify: `flutter_app/lib/widgets/message_bubble.dart`

- [ ] **Step 1: Add archive navigation to chat header**

Add a small icon button (gallery/photo icon) in the header row that opens the Memory Archive screen.

- [ ] **Step 2: Add download overlay on image bubbles**

In `message_bubble.dart`, add a small download icon (bottom-right corner) on image messages. Tapping triggers a browser download of the base64 image data.

- [ ] **Step 3: Build PWA + sync**

```bash
cd flutter_app && flutter build web --release --base-href /app/
cp -r build/web/* ../web-build/
rsync web-build to dominus
```

- [ ] **Step 4: Commit**

---

## Task 8: Retroactive Memory Seeding — Klukai's Choice

**This is the final task. Klukai reviews conversation history and picks her memories.**

- [ ] **Step 1: Build seeding script**

A Python script that runs inside companion-core:
1. Reads all conversation exchanges from `companion_messages`
2. Groups into exchanges (user + assistant pairs)
3. Sends each meaningful exchange to qwen2.5-3b with Klukai's curation prompt
4. For exchanges she marks as `keep=true`, generates an image via ComfyUI
5. Saves image + metadata to the archive
6. Rate-limits to avoid overwhelming LM Studio and ComfyUI

- [ ] **Step 2: Run the seeding**

Execute inside the container. Monitor progress. Expect ~5-10 memories from 200+ interactions.

- [ ] **Step 3: Verify seeded memories**

```bash
curl -sf http://100.111.198.19:8300/api/memories | python3 -m json.tool
curl -sf http://100.111.198.19:8300/api/memories/categories | python3 -m json.tool
```

- [ ] **Step 4: Commit**

---

## Task 9: Final Regression + Full Validation

**No code changes — validation only.**

- [ ] **Step 1: Full infrastructure regression**

Same as Task 4 Step 1 — verify nothing broke.

- [ ] **Step 2: Memory archive E2E**

- Generate a new image → verify it appears in archive
- Test recall via WebSocket ("show me a memory" to test user, not default)
- Test category filtering
- Test commander keep/discard
- Test image and thumbnail serving
- Test mobile layout in Playwright (resize to 390x844)
- Test desktop layout in Playwright (resize to 1440x900)

- [ ] **Step 3: Metadata check**

- DB table schema matches spec
- Indexes present
- Volume has images + thumbnails
- Seeded memories have annotations and categories
- Affection state unchanged (score=838, Bonded)
- No test messages in default user's chat history

- [ ] **Step 4: Performance check**

- `/api/memories` response time < 100ms
- `/api/memories/{id}/thumbnail` response time < 50ms
- Image gen still completes in < 30s
- VRAM freed after gen (torch < 1024MB)
