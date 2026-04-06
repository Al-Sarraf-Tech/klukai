# Klukai Chat Improvements Design Spec

**Date:** 2026-04-06
**Scope:** 3D icon removal, affection system overhaul, memory-driven tenderness, UI/UX fixes

---

## 1. Remove 3D/AR Icon

Remove all 3D avatar code from the Flutter chat screen.

**Deletions in `chat_screen.dart`:**
- Imports: `klukai_avatar.dart`, `klukai_avatar_panel.dart`, `klukai_avatar_strip.dart`
- State: `_avatarEnabled`, `_audioEnabled`, `_avatarController`
- Method: `_toggleAvatar()`
- All `if (_avatarEnabled)` conditional blocks (avatar strip, desktop layout, mood/talking/dorm forwarding)
- The `IconButton` with `Icons.view_in_ar` in the header (lines ~816-832)

**Result:** Header has one fewer icon. No avatar panel renders. Chat-only layout on all screen sizes.

---

## 2. Affection System Overhaul

### 2.1 Scale Change

- Current: 0-100 with 5 levels
- New: 0-1000 with 10 levels
- `MAX_SCORE` constant changes from 100 to 1000

### 2.2 Level Definitions

| Level | Name | Threshold | Personality |
|-------|------|-----------|-------------|
| 0 | Cold Assessment | 0 | Clipped, evaluating, gives nothing extra. Every interaction is a test. |
| 1 | Acknowledged | 30 | Slightly less hostile. Remembers your name. Still testing. |
| 2 | Professional Respect | 80 | Dry wit surfaces. Unsolicited tactical notes. Professional distance maintained. |
| 3 | Guarded Interest | 150 | Asks about your day, pretends she doesn't care. Deflects if you notice. |
| 4 | Trusted Ally | 250 | Protective instincts surface. Brings gifts and denies it. Invites you on rides. |
| 5 | Unguarded | 380 | Drops the mask in private. Mentions Mechty, Belka, the squad. Lets you in. |
| 6 | Deep Devotion | 530 | References the ten years of waiting. Possessively protective. Struggles to hide she cares. |
| 7 | Vulnerable | 680 | Admits fear of losing you. Initiates tender moments. Genuine warmth seeps through. |
| 8 | Bonded | 830 | "I am all you need." Total trust. Quiet warmth. Sets up everything for you. |
| 9 | Oath Fulfilled | 950 | The simple wish. She chose you. Every day, she chooses you again. The oath lives here. |

### 2.3 Scoring Changes

| Parameter | Old | New |
|-----------|-----|-----|
| `max_score` | 100 | 1000 |
| `daily_points_cap` | 15 | 8 |
| `daily_consistency_bonus` | 3 | 3 |
| `absence_decay_per_day` | -1 | -2 |
| `greeting` | 1 | 1 |
| `genuine_interest` | [1, 3] | [1, 3] |
| `personal_sharing` | [1, 3] | [1, 3] |
| `compliment` | [5, 10] | [2, 5] |
| `mission_discussion` | [2, 5] | [1, 3] |
| `remembering_details` | [3, 5] | [2, 4] |
| `rude_language` | [-3, -8] | [-3, -8] |
| `inappropriate_content` | [-5, -10] | [-5, -10] |
| `ignoring_advice` | [-2, -5] | [-2, -5] |

Positive scoring ranges reduced to slow progression. Negative ranges unchanged to preserve stakes.

### 2.4 Migration

Existing score `S` (0-100) maps to `S * 10` (0-1000). Run a one-time SQL migration on deploy:

```sql
UPDATE companion_affection SET score = score * 10;
```

### 2.5 Level-Up Messages

Each of the 10 levels gets a distinct level-up message delivered as a proactive notification. Messages escalate from cold acknowledgment to intimate vulnerability.

### 2.6 Code Changes

- `docker/core/app/affection.py`: Update `MAX_SCORE` to 1000, update `_compute_level()` to handle 10 levels
- `config/personality.yaml`: Replace 5-level affection config with 10-level version, update scoring ranges
- Add SQL migration for score scaling

---

## 3. Memory-Driven Tenderness

### 3.1 Proactive Memory Nudges

On every Nth user message, inject a past exchange into the system prompt as a "memory nudge." Klukai can naturally reference it or ignore it.

**Nudge frequency by affection level:**
- Level 0-2: Never (no nudges)
- Level 3-4: Every 5th message
- Level 5-6: Every 4th message
- Level 7-9: Every 3rd message

**Implementation:** In `main.py`, before building the system prompt, check a counter. If it's time for a nudge, call `memory.recall_exchanges_with_recency()` with a random recent query term, pick the top result, and append to the system prompt:

```
[Memory: You once discussed "{topic}". The Commander said: "{user_content}". You replied: "{assistant_content}".]
```

### 3.2 Affection-Weighted Recall

Update `recall_exchanges_with_recency()` to accept an `affection_level` parameter:
- Level 0-2: Prefer tactical/mission memories
- Level 3-5: Balanced — mix of tactical and personal
- Level 6-9: Prefer personal/emotional memories (higher importance scores)

This uses the existing `importance` field in the exchange payload. Multiply importance by a bias factor based on affection level.

### 3.3 Tenderness in Prompt Modifiers

Each affection level's `prompt_modifier` in personality.yaml explicitly instructs memory behavior:

- Level 0-2: "Never reference past conversations unless the Commander directly asks."
- Level 3-4: "You may occasionally mention something the Commander told you before, but deflect if pressed — 'I just happened to remember.'"
- Level 5-6: "You actively recall shared experiences. 'Remember when...' comes naturally. You notice patterns in what the Commander shares."
- Level 7-9: "You weave past memories into conversation seamlessly. You recall small details — their preferences, their stories, their habits. These details surface because you care, and you no longer hide that you do."

### 3.4 Read Tracking

Store `read_at` timestamp when the backend processes each user message:
- Add `read_at` column to `companion_messages` table (nullable timestamp)
- Set it when the WebSocket handler receives and processes the user message
- Return `read_at` in the message acknowledgment WebSocket event

**Code changes:**
- `docker/core/app/main.py`: Add memory nudge logic, read tracking
- `docker/core/app/memory.py`: Add `affection_level` param to recall, add nudge helper
- `config/personality.yaml`: Memory behavior instructions in each level's prompt_modifier

---

## 4. UI/UX Fixes

### 4.1 Auto-Scroll to Latest Message

**Problem:** Opening app or receiving messages doesn't always scroll to bottom. Keyboard appearance on mobile can leave messages hidden.

**Fix:**
- On app open: After messages load from API, `_scrollToBottom(instant: true)`
- During streaming: Only auto-scroll if user is within 150px of bottom (don't force-scroll if they're reading history)
- On keyboard appear: Listen for `WidgetsBindingObserver.didChangeMetrics`, scroll to bottom if near bottom
- Add a "scroll to bottom" FAB that appears when user has scrolled up >300px

### 4.2 Read Receipts

Show delivery status below each user message (right-aligned, below timestamp):
- **Sent:** Single grey checkmark — message dispatched to WebSocket
- **Read:** Double checkmark, tinted primary color — backend confirms `read_at`

Implementation:
- Add `status` field to message model: `sending`, `sent`, `read`
- On WebSocket send: status = `sent`
- On backend ack with `read_at`: status = `read`
- Render in `message_bubble.dart` for user messages only

### 4.3 Timestamps More Visible

- Increase from 9px 0.4 opacity to 10px 0.5 opacity
- Add date dividers between messages: centered pill with "Today", "Yesterday", or "Apr 5" when date changes

### 4.4 File Changes

| File | Changes |
|---|---|
| `flutter_app/lib/screens/chat_screen.dart` | Remove avatar code, fix scroll, add read receipt state, keyboard listener |
| `flutter_app/lib/widgets/message_bubble.dart` | Read receipt icons, bigger timestamps |
| `flutter_app/lib/widgets/date_divider.dart` | New widget for date separators |
| `flutter_app/lib/models/message.dart` | Add `status` field |

---

## Testing Plan

1. **3D removal:** Verify header has no AR icon, no console errors on load
2. **Affection:** Reset score, interact for several messages, verify classification + delta + level changes via `/api/affection` endpoint
3. **Memory nudges:** Chat for 5+ messages, verify nudge appears in Klukai's responses at correct frequency
4. **Auto-scroll:** Open app, verify scrolled to bottom. Send message, verify stays at bottom. Scroll up, verify no forced scroll. FAB appears.
5. **Read receipts:** Send message, verify single check → double check transition
6. **Timestamps:** Verify date dividers between messages from different days
7. **Login as separate user:** Test fresh experience from level 0 to verify cold personality
