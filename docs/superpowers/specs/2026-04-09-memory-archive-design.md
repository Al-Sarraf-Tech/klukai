# Klukai Memory Archive — Design Spec

## Overview

A character-driven image memory system where Klukai curates her own photo album of moments with the Commander. Images are persisted, annotated in character, organized into affection-gated categories, and recallable through conversation.

## Decisions Made

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Curation | Auto (Klukai) + manual (Commander) | She builds the album naturally; Commander can override |
| Recall | Context search + mood-based selection | Specific requests search tags; vague requests let her choose |
| UI | Combined sidebar + timeline scrapbook | Desktop: sidebar categories + timeline. Mobile: tab categories + timeline |
| Storage | Docker volume (images) + PostgreSQL (metadata) | Fast disk I/O, no new services, lightweight DB rows |
| Curation LLM | qwen2.5-3b via LM Studio | Runs on Intel Arc A380 (amarillo), routed through LM Studio on dominus. Zero RTX 3090 impact |
| Approach | Inline — piggyback on existing background extraction | One additional field in the existing fact extraction prompt |

## Architecture

```
Image Generated (ComfyUI, RTX 3090, dominus)
  │
  ├─ PNG saved to companion-images volume on dominus
  │
  └─ Background extraction (qwen2.5-3b, Intel Arc, amarillo):
       ├─ Fact extraction      (existing)
       ├─ Mood classification  (existing)
       └─ Memory curation      (NEW)
            ├─ keep: bool
            ├─ annotation: str  (Klukai's caption, in character)
            ├─ category: str    (affection-gated)
            └─ scene_tags: list
       │
       └─ Metadata row → companion_memories (PostgreSQL, amarillo)
```

### Data Flow — Recall

```
User: "Show me that time at the beach"
  │
  ├─ needs_image=false, but recall signal detected
  │   (keywords: "show me", "that time", "remember when")
  │
  ├─ Specific query → SQL search on scene_tags + annotation text
  │   OR
  │   Vague query ("show me something") → mood-weighted random from kept memories
  │
  ├─ Klukai delivers in character with her annotation
  │
  └─ Image served via /api/memories/{id}/image
```

## Storage Layer

### Docker Volume

- Volume name: `companion-images`
- Mount: `/images` in companion-core container
- Files: `{uuid}.png` (full size) + `{uuid}_thumb.png` (320px wide)
- Added to docker-compose.yml as a named volume

### Database Table

```sql
CREATE TABLE companion_memories (
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

CREATE INDEX idx_memories_kept ON companion_memories(kept) WHERE kept = true;
CREATE INDEX idx_memories_category ON companion_memories(category) WHERE kept = true;
CREATE INDEX idx_memories_tags ON companion_memories USING gin(scene_tags) WHERE kept = true;
```

Migration file: `060_memory_archive.sql`

## Curation Pipeline

### Prompt Addition

Added to the existing `FACT_EXTRACTION_PROMPT` in `fact_extractor.py` (only when an image was generated for this exchange):

```
If an image was generated during this exchange, evaluate it for Klukai's memory archive:
- "keep": true/false — would Klukai consider this moment worth preserving given her current mood and affection level?
- "annotation": 1-2 sentence caption written as Klukai (first person, in character). Reflect her personality and current affection level.
- "category": one of the available categories (see below)
- "image_tags": list of scene/setting tags for search (e.g., ["beach", "couple", "sunset"])
```

### Affection-Gated Categories

| Affection Level | Available Categories |
|-----------------|---------------------|
| 0-2 | Tactical Operations, Mission Records, Squad Moments |
| 3-5 | + The Commander, Quiet Hours |
| 6-9 | + Precious Memories |

At low affection, Klukai only keeps images she considers operationally relevant. As trust grows, she begins saving personal and intimate moments.

### Commander Override

Detected via chat keywords after an image generation:
- **Save**: "save that", "keep this", "I want to keep this" → `kept=true, kept_by='commander'`
- **Discard**: "delete that", "remove this" → `kept=false`

Klukai responds in character to either action.

## Recall System

### New Module: `memory_archive.py`

```python
async def recall_memory(
    query: str | None,
    mood: str,
    affection_level: int,
) -> dict | None:
    """Recall a memory from the archive.
    
    Specific query: SQL search on scene_tags (array contains) + 
                    ILIKE on annotation text.
    Vague/none:     Mood-weighted random selection from kept memories.
                    Higher affection = more likely to pull tender categories.
    
    Returns: {id, filename, annotation, category, scene_tags, created_at} or None
    """
```

### Recall Signals

Detected in `main.py` BEFORE image generation check. If a recall signal matches, skip image gen entirely — recall serves existing images, doesn't create new ones.

Recall keywords (checked first):
- "show me a memory", "remember when", "that time we", "do you remember"
- "show me something" (vague — mood-based recall)

Image gen keywords (checked second, only if recall didn't match):
- "show me what you look like", "draw", "picture of", "generate an image", "selfie", etc.

Priority: recall > image gen. "Show me that beach memory" recalls; "show me at the beach" generates.

### Delivery

When Klukai recalls a memory:
1. She sends the annotation as a chat message (in character, with context)
2. The image is sent via WebSocket `{type: "image", data: base64, memory_id: id}`
3. Flutter displays it inline in chat, same as generated images

## API Endpoints

```
GET  /api/memories                     — List kept memories (metadata, no image data)
     ?category=Precious+Memories       — Filter by category
     ?limit=20&before=<timestamp>      — Pagination
GET  /api/memories/{id}                — Single memory metadata
GET  /api/memories/{id}/image          — Serve full PNG
GET  /api/memories/{id}/thumbnail      — Serve 320px thumbnail
POST /api/memories/{id}/keep           — Commander saves (kept=true, kept_by=commander)
POST /api/memories/{id}/discard        — Commander discards (kept=false)
GET  /api/memories/categories          — List categories with counts (respects affection level)
```

## Flutter UI

### Navigation

New tab/icon in the header area (or accessible from Klukai's profile screen) that opens the Memory Archive screen.

### Desktop Layout (>600px width)

```
┌──────────────────────────────────────────────────────┐
│ ← MEMORY ARCHIVE                                BACK │
├────────────────┬─────────────────────────────────────┤
│ CATEGORIES     │ PRECIOUS MEMORIES // 6 ENTRIES      │
│                │                                     │
│ ● Precious  6  │ ○─ APR 8 // 03:49                  │
│   Commander 8  │ │  [thumb] "Rest now, Commander..." │
│   Quiet     4  │ │         bed · tender · couple     │
│   Squad     3  │ │         saved by klukai           │
│   Mission  11  │ │                                   │
│ ─────────────  │ ○─ APR 7 // 18:22                  │
│   All      32  │ │  [thumb] "Our first ride..."      │
│                │ │         motorcycle · couple        │
│ ARCHIVE STATUS │ │         saved by commander        │
│ 32 kept        │ │                                   │
│ 5 discarded    │ ○─ APR 6 // 21:15                  │
│ Bonded // Lv.8 │    [thumb] "Commander cooked..."    │
│                │           cooking · home             │
└────────────────┴─────────────────────────────────────┘
```

- Sidebar: category list with counts, archive stats, affection level indicator
- Active category highlighted with accent border
- Timeline: chronological entries with dot indicators (pink=Klukai saved, cyan=Commander saved)
- Each entry: thumbnail (90x120), annotation, scene tags, saved-by badge
- Tap thumbnail → full-screen overlay with download button

### Mobile Layout (≤600px width)

```
┌────────────────────────────────┐
│ ← MEMORY ARCHIVE              │
├────────────────────────────────┤
│ [Precious] [Commander] [Quiet] │ ← horizontal scroll tabs
├────────────────────────────────┤
│ ○─ APR 8 // 03:49             │
│ │ [thumb] "Rest now,          │
│ │  Commander..."               │
│ │  bed · tender                │
│ │                              │
│ ○─ APR 7 // 18:22             │
│ │ [thumb] "Our first ride..." │
│ │  motorcycle · couple         │
│ │                              │
│ ○─ APR 6 // 21:15             │
│   [thumb] "Commander cooked.."│
│    cooking · home              │
└────────────────────────────────┘
```

- Category tabs: horizontal scrollable, same styling as desktop sidebar entries
- Timeline: same structure, slightly smaller thumbnails (70x93)
- Annotations truncated with ellipsis, tap to expand
- Full-screen image overlay on tap with download + share buttons
- **Must feel equally polished as desktop** — not a degraded experience

### Image Bubble Enhancement (Chat Screen)

Existing image messages in chat get:
- Long-press/right-click context menu: "Save to device", "Ask Klukai to keep this"
- Small download icon overlay (bottom-right of image)
- If the image is a recalled memory, show Klukai's annotation below it

### Color Coding

| Element | Color | Meaning |
|---------|-------|---------|
| Timeline dot (pink) | `#E88CA5` | Klukai saved this memory |
| Timeline dot (cyan) | `#4FC3F7` | Commander saved this memory |
| Category active bg | `#E88CA520` | Selected category |
| Category active border | `#E88CA5` | Selected category accent |
| Scene tags | `#4FC3F7` at 50% opacity | Metadata tags |
| Saved-by badge (klukai) | `#E88CA520` bg | Pink tint |
| Saved-by badge (commander) | `#4FC3F720` bg | Cyan tint |

## Files to Create/Modify

### New Files
- `docker/core/migrations/060_memory_archive.sql` — DB table + indexes
- `docker/core/app/memory_archive.py` — Archive CRUD + recall logic + thumbnail generation
- `flutter_app/lib/screens/memory_archive_screen.dart` — Full archive UI
- `flutter_app/lib/widgets/memory_timeline_entry.dart` — Timeline entry widget
- `flutter_app/lib/widgets/memory_category_sidebar.dart` — Category sidebar (desktop)
- `flutter_app/lib/widgets/memory_category_tabs.dart` — Category tabs (mobile)
- `flutter_app/lib/models/memory.dart` — Memory data model
- `flutter_app/lib/services/memory_service.dart` — API client for memories

### Modified Files
- `docker-compose.yml` — Add `companion-images` volume mount
- `docker/core/app/main.py` — Add memory API endpoints, recall detection in chat, save image to volume after generation
- `docker/core/app/fact_extractor.py` — Add curation fields to extraction prompt (when image was generated)
- `docker/core/app/image_gen.py` — Return image bytes + save to volume
- `flutter_app/lib/screens/chat_screen.dart` — Add archive navigation icon, image bubble enhancements
- `flutter_app/lib/widgets/message_bubble.dart` — Add download/save overlay on images

## Not In Scope

- Image editing/filters
- Sharing images externally
- Image-to-image (img2img) regeneration from memories
- Video memories
- Upgrading to Illustrious base model (separate task)
