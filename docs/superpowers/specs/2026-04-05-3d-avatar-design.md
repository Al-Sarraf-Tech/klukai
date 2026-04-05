# 3D Klukai Avatar — Design Spec

**Date:** 2026-04-05
**Status:** Approved
**Rollback tag:** `pre-3d-avatar`

## Summary

Add a 3D model of Klukai to the Companion app as an idle companion panel alongside the existing chat UI. The model is extracted from GFL2 game assets (HK416), rerigged in Blender, and rendered via Three.js in an HTML overlay using the existing JS interop pattern. Full VTuber-level animation with 30+ animations, mood-reactive poses, fidgets, and dorm-mode sleeping.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Layout | Idle companion alongside chat | Preserves existing chat UX, 3D model is additive |
| Model source | GFL2 extracted asset (HK416) | Authentic character, already rigged |
| Tap behavior | Smart routing (recent=replay, stale=proactive) | Natural feel, covers both use cases |
| Animation depth | Full VTuber (~30+ animations) | Maximum personality and immersion |
| Rendering | Three.js via HTML overlay | Matches existing JS bridge pattern, best ecosystem |
| Audio | Separate toggle icon, not tied to tap | Tap is visual-only; voice is opt-in via icon |
| Avatar toggle | Header icon, persisted, default OFF | Opt-in, existing users unaffected |

## Asset Pipeline

```
GFL2 Unity Bundle → AssetStudio → .fbx → Blender → .glb → Three.js
```

1. **Extract**: AssetStudio or UnityPy to pull HK416 mesh, skeleton, textures, animation clips from GFL2 game files
2. **Blender import**: Fix materials (Unity → PBR), verify bone hierarchy, check UV maps
3. **Author animations** (~30+):
   - 8 mood-group idle loops (relaxed, happy, serious, shy, combat, tender, drowsy, melancholy)
   - ~6 fidgets (hair touch, look around, stretch, weapon adjust, cross arms, sigh)
   - Reactions (tap response, affection milestone, surprise)
   - Talking mouth loop (for TTS sync)
   - Independent blink loop
   - Dorm sleeping pose
   - Smooth blend transitions between mood groups
4. **Blend shapes**: Mouth open/close, smile, frown, blink, blush (add if missing from extract)
5. **Export**: Single `klukai.glb` with all animations as named clips, textures packed (~5-15MB)

## Three.js Integration Layer

New file: `web-build/js/klukai_3d.js`

Architecture:
```
Flutter (Dart) ←→ JS Interop ←→ klukai_3d.js ←→ Three.js Scene
```

### Bridge API

| Method | Purpose |
|--------|---------|
| `klukaiBridge.init(canvasId, modelUrl)` | Load .glb, set up scene/camera/lighting |
| `klukaiBridge.setMood(moodName)` | Crossfade to matching mood-group idle |
| `klukaiBridge.playReaction(reactionName)` | One-shot animation, returns to current idle |
| `klukaiBridge.setTalking(bool)` | Start/stop mouth animation (only when voice ON) |
| `klukaiBridge.setBlush(intensity)` | Control blush blend shape (0-1) |
| `klukaiBridge.lookAt(x, y)` | Head/eye tracking toward cursor or touch |
| `klukaiBridge.setDormMode(bool)` | Transition to sleeping pose |
| `klukaiBridge.dispose()` | Cleanup scene and free resources |

### Scene Setup

- Transparent background (composites over Flutter dark theme)
- Soft directional light + ambient light
- Mood-colored rim light synced with existing `_moodGlowColor` system
- Camera: fixed upper-body framing, slight perspective

## Animation State Machine

### Mood Group Mapping

| Group | Moods | Idle Character |
|-------|-------|----------------|
| Relaxed | relaxed, calm, content, neutral | Standing easy, slight sway |
| Happy | happy, playful, teasing, smug, confident | Upbeat posture, occasional bounce |
| Serious | focused, analytical, commanding, determined, vigilant | Rigid stance, arms at side |
| Shy | shy, flustered, embarrassed, bashful | Averted gaze, fidgeting hands |
| Combat | hunting, aggressive, fierce, alert, combat-ready | Wide stance, hand near weapon |
| Tender | tender, devoted, affectionate, warm, vulnerable | Soft posture, gentle head tilt |
| Drowsy | drowsy, sleepy, exhausted, lazy | Swaying, drooping eyes, yawns |
| Melancholy | sad, lonely, distant, nostalgic, worried | Slumped shoulders, looking down |

### Layered Animation System

- **Base layer**: Mood-group idle (looping, 0.5s crossfade on mood change)
- **Additive layer 1**: Blink loop (random interval 2-6s)
- **Additive layer 2**: Talking mouth (when voice ON + audio playing)
- **One-shot layer**: Fidgets and reactions (play once, blend back to base)

### Fidgets

Fire randomly every 30-90s, mood-context-aware:
- Universal: blink hard, look around, weight shift
- Relaxed/Happy: hair touch, stretch, small smile
- Combat: weapon check, scan horizon
- Drowsy: yawn, head nod, rub eyes
- Shy: tuck hair behind ear, look away

### Dorm Mode

Activates after 21:00 at affection 2+ (matching existing system):
- Forces Drowsy animation group regardless of actual mood
- Special sleeping pose if idle > 5 minutes
- Dim rim light, warm color shift

### Head/Eye Tracking

- Eyes and head follow cursor (desktop) or last touch point (mobile)
- Clamped to natural range
- Disabled during sleeping pose

## Flutter Layout Changes

### Desktop (width > 768px)

`ChatScreen` becomes a `Row`:
- **Left panel (35%)**: `KlukaiAvatarPanel` — Three.js canvas, speech bubble overlay, audio toggle, mood glow gradient
- **Right panel (65%)**: Existing chat UI — header, affection gauge, messages, input bar (untouched)

Left panel replaces the 52x52 header portrait on desktop.

### Mobile (width ≤ 768px)

Avatar strip (~120px) inserted between header and messages:
- Compact 3D model on left, speech bubble on right
- Collapsible — swipe up or tap chevron to minimize to thin mood-glow bar

### Avatar Toggle

- Icon button in header (cube/avatar icon)
- ON/OFF state persisted via `SharedPreferences`
- **Default: OFF** — existing users unaffected
- OFF: Full-width chat (current layout), Three.js disposed, portrait PNG returns to header
- ON: Split layout, Three.js initializes

### Audio Toggle

- Speaker icon on avatar panel corner
- ON/OFF state persisted via `SharedPreferences`
- **Audio ON**: TTS auto-plays on speech bubbles, mouth animation syncs
- **Audio OFF**: Text bubbles only, no TTS calls, no mouth movement
- Independent from chat bubble speaker icons (those still work)

### Speech Bubble

- Streams tokens from WebSocket in real-time (mirrors chat)
- Truncated to ~100 chars with "..." for long messages (full text in chat)
- Auto-fades after 5s of no new tokens
- Tap to dismiss immediately
- Desktop: floats above model; Mobile: beside model in strip

## Tap-to-Talk Interaction

### Smart Routing

```
User taps Klukai
  → Last assistant message < 30s ago?
    YES → Replay: show text in bubble, play reaction animation
    NO  → Proactive: send tap_interact via WebSocket, stream new response
```

### Replay Path (no backend call)

1. Tap → JS callback to Dart
2. Check last message timestamp
3. Show text in speech bubble
4. Play tap reaction animation
5. If audio ON: call `/api/tts`, `setTalking(true)`, stop on audio end

### Proactive Path (backend call)

1. Tap → send `{"type": "tap_interact"}` via WebSocket
2. Backend generates proactive line (existing system)
3. Response streams back as `token` → `done`
4. Speech bubble streams tokens, reaction animation plays
5. If audio ON: TTS triggers on completion, mouth syncs

### Anti-spam

- 5 second cooldown between taps
- Ignored during TTS playback
- Proactive path respects existing anti-annoyance guards (intimate mood block, daily caps)

## Backend Changes

### New WebSocket message: `tap_interact`

Frontend sends `{"type": "tap_interact"}`. Backend handles it like existing proactive triggers. Response streams as normal `token` → `done`. Respects anti-annoyance guards.

### Static asset serving

`klukai.glb` served from `web-build/assets/models/` via nginx with cache headers:
```
Cache-Control: public, max-age=604800, immutable
```

### No changes to

- WebSocket streaming protocol
- TTS/STT endpoints
- Mood, affection, memory systems
- Database schema
- Docker configuration

## File Structure

### New Files

```
web-build/js/klukai_3d.js              # Three.js bridge
web-build/js/three.module.min.js       # Three.js library
web-build/assets/models/klukai.glb     # Model + animations

flutter_app/lib/widgets/
  klukai_avatar.dart                   # HtmlElementView wrapping Three.js canvas
  klukai_avatar_panel.dart             # Desktop left panel
  klukai_avatar_strip.dart             # Mobile compact strip
  speech_bubble.dart                   # Floating speech bubble with streaming
```

### Modified Files

```
flutter_app/lib/screens/chat_screen.dart  # Row split, strip insertion, toggle state, tap routing
web-build/index.html                      # Script tags for three.js + klukai_3d.js
gateway/ (nginx config)                   # Cache headers for .glb
```

### Dependencies

**JS (bundled):**
- three.js r168+ (core renderer, GLTFLoader, AnimationMixer)

**Flutter:**
- No new packages (dart:js_interop + web already in pubspec.yaml)

**Dev-side tools (not shipped):**
- AssetStudio or UnityPy — GFL2 extraction
- Blender 4.x + glTF exporter — rigging, animation, export
