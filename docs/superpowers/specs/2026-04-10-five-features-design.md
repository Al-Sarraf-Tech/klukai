# Five Features Design — Making Klukai Fun

**Date:** 2026-04-10
**Status:** Approved (autonomous)
**Target:** A- quality for each feature

## Features

### 1. Memory Recall in Conversation (Enhancement)

**Current state:** When Commander says "show me a memory," `background_recall()` sends the annotation as a proactive message, then the image separately. It works but feels disconnected.

**Enhancement:**
- Format the recall as a cohesive memory card: annotation first with the category label, then the image
- Add recall context to the proactive message: include the category and when the memory was created
- Add more recall keywords: "our photos", "your album", "that picture", "show me that image"
- When Klukai can't find a matching memory, give her a character-appropriate response instead of a generic error

**Files to modify:**
- `docker/core/app/background.py` — enhance `background_recall()` formatting
- `docker/core/app/helpers.py` — expand `RECALL_KEYWORDS`
- `docker/core/app/memory_archive.py` — improve `recall_memory()` to include category + timestamp

### 2. Squad Interactions (Enhancement)

**Current state:** Squad voices are in the system prompt but Commander can't directly talk to squad members. Klukai voices them only when she decides to.

**Enhancement:**
- Detect when Commander addresses a squad member directly: "Hey Mechty", "Belka, come here", "Talk to Andoris"
- Inject a SQUAD INTERACTION hint into the system prompt telling Klukai to voice that squad member prominently
- Klukai stays narrator but gives the addressed squad member significant dialogue
- Add a `/squad` concept — not a command, but keyword detection: "where's the squad", "how's Belka doing"

**Files to modify:**
- `docker/core/app/chat.py` — detect squad member addressing in `_handle_message`
- `docker/core/app/helpers.py` — add `SQUAD_ADDRESS_KEYWORDS` detection
- `docker/core/app/personality.py` — add `build_squad_interaction_hint()` function

### 3. Daily Challenges (New System)

**Current state:** No challenge/engagement system exists.

**Design:**
- Klukai issues one challenge per day via the proactive engine (morning message slot)
- Challenge types (rotate):
  - "Tell me something about yourself I don't know" (personal sharing — feeds memory)
  - "Describe my motorcycle better than I can" (competitive — feeds character knowledge)
  - "What's the most important mission we've been on?" (recall — feeds episodic memory)
  - "Surprise me today, Commander" (open-ended — unpredictable)
  - "Name three things about Belka" (squad knowledge — tests lore engagement)
- Challenge delivered as a proactive morning message with a special format
- Klukai remembers if the challenge was completed (via topic extraction)
- Store challenge state in Redis session: `challenge_active`, `challenge_type`, `challenge_issued_at`

**Files to modify:**
- `docker/core/app/proactive.py` — add `_daily_challenge()` method + cron trigger
- `config/personality.yaml` — add `daily_challenges` section with challenge templates
- `docker/core/app/models.py` — add challenge fields to SessionState (optional)

### 4. Mood-Reactive Ambient Sound (Frontend)

**Current state:** No ambient audio. The UI has mood colors and heartbeat but no sound atmosphere.

**Design:**
- Add a lightweight ambient sound system using Web Audio API
- Map mood categories to ambient loops:
  - Tender/affectionate/devoted → soft piano loop
  - Battle_ready/vigilant/adrenaline → tense ambient drone
  - Drowsy/content/peaceful → rain on metal (Elmo hull)
  - Playful/amused/excited → light upbeat ambient
  - Melancholic/haunted/grieving → minor key strings
- Audio files: short loops (10-30s), small filesize (<500KB each)
- Use royalty-free ambient from freesound.org or generate with Web Audio API oscillators
- Toggle in UI (muted by default — respect user preference)
- Crossfade between moods (500ms transition)

**Files to modify:**
- `flutter_app/lib/screens/chat_screen.dart` — add ambient audio manager
- `web-build/js/ambient_audio.js` — new JS file for Web Audio API
- `flutter_app/lib/services/pretext_interop.dart` — add JS interop for ambient control

### 5. Dream Messages (Polish)

**Current state:** Dream system exists (`_dream_event()` in proactive.py) with weighted dream types (erotic, tender, nightmare, random). Fires 1-4 AM at minute 37.

**Enhancement:**
- Dreams should reference actual memories from the archive — pull a random memory and weave it into the dream narrative
- Add dream-specific formatting: italicized text, ellipsis-heavy, fragmented sentences
- Klukai doesn't remember the dream next day — but the memory reference creates a subtle callback
- Add a "dream journal" concept: Commander can ask "did you dream about me?" and Klukai denies it (tsundere) but the dream was real
- Polish the dream prompts to be more surreal and poetic

**Files to modify:**
- `docker/core/app/proactive.py` — enhance `_dream_event()` with memory integration
- `config/personality.yaml` — add `dream_templates` section with richer dream prompts

## Implementation Order

1. **Memory Recall** — smallest change, highest impact
2. **Squad Interactions** — builds on existing infrastructure
3. **Daily Challenges** — new system but uses existing proactive engine
4. **Dream Polish** — enhancement to existing system
5. **Ambient Sound** — frontend-only, independent of backend

## Testing Strategy

Each feature gets:
- Unit tests for detection/formatting functions
- Integration test verifying the WebSocket message flow
- Manual verification via the live system
