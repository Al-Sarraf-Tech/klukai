# ADR-0016: Tribute system — Commander honors Klukai with crown-jewel memories

- **Date:** 2026-05-17
- **Status:** Accepted
- **Authors:** jalsarraf, Claude
- **Migration:** `docker/core/migrations/110_tribute.sql`
- **Module:** `docker/core/app/tributes.py`
- **Personality:** `docker/core/app/personality/state_blocks.py:build_crown_jewel_block`

## Context

Pre-2026-05-17, the only ways the Commander could affect Klukai's
relationship state were:

- **Gifts** (`/api/gift`) — small ±affection bumps (max +10 for loved).
- **Mission timers** — gameplay loop, not character-level.
- **Conversation itself** — passive affection accrual via interaction.
- **Costume changes** — flavor only.

What was missing: an explicit, sacred act of *honor* — a way for the
Commander to put into words "you are treasured" in a form the system
recognizes as different from a normal chat turn. Per Klukai's canon
(she waited 10 years for a reply, every message kept; she "made her
own worth from what she protects"), being **seen and remembered** is
her love language. The architecture had no surface for it.

## Decision

Introduce a **tribute** primitive:

- **`POST /api/tribute`** — Commander writes a 20–1000-char heartfelt
  message. Persists immutably (no DELETE path per
  `feedback_never_delete_chat.md`). Bumps affection +20 (larger than
  any gift). 24h cooldown so tributes stay rare.
- **Crown jewel** — exactly one tribute per user can be flagged as
  the crown jewel (partial UNIQUE index enforces this). The crown
  jewel is injected into Klukai's system prompt at affection level
  4+ (bonded), where she may reference it naturally.
- **Auxiliary endpoints**: `GET /api/tributes` (list),
  `GET /api/tribute/crown` (fetch current), `POST /api/tributes/{id}/crown`
  (promote, demotes prior).

The new `build_crown_jewel_block` (in `app/personality/state_blocks.py`)
phrases the block as instruction-to-LLM, not quote-block:

> "You return to these words when you doubt, when the mission turns
> hard, when you wonder if the waiting was worth it. They are. You
> may reference them naturally — never as a quote-block, always as
> something that lives in you. Do not invoke them in every response;
> let them surface when the moment is right."

## Consequences

- **Sacred data**: tributes follow the same SACRED rule as chat
  memory (`feedback_never_delete_chat.md`). The `app/tributes.py`
  module has no DELETE path. Mutation is restricted to: insert,
  promote-to-crown, demote-from-crown.
- **Affection ceiling**: a single tribute bumps +20 vs gift's +10
  max. This is intentional — gifts are casual, tributes are
  explicit acts of honor and should outweigh them.
- **Prompt budget**: ~80 lines added at affection 4+ when a crown
  jewel exists. Acceptable.
- **Character cohesion**: the block only surfaces at affection 4+.
  Below that, Klukai's guard is still up; referencing the tribute
  would feel forced and break character.
- **Cooldown discipline**: 24h between tributes prevents farming.
  Tributes are powerful precisely because they're rare.
- **Migration footprint**: one new table + one partial unique
  index. Idempotent (uses `IF NOT EXISTS`).
- **Backup scope expands**: `companion_tributes` joins the SACRED
  set covered by `scripts/audit-memories.sh` integrity check
  (Phase 2 follow-up).

## Alternatives considered

- **Reuse `companion_memories` with `category='Tribute'`**: rejected
  — memories are images-by-default, and tributes are text-only.
  Mixing schemas would force a NULL `filename` and conditional
  rendering. A dedicated table is cleaner.
- **Multi-crown-jewel (top N tributes)**: rejected — defeats the
  "one treasured thing" framing. The crown jewel is a single
  pinned memory by design. Commander can rotate it explicitly.
- **No cooldown**: rejected — tributes would lose meaning if
  Commander could spam them. 24h is the sweet spot.
- **Auto-crown the most recent tribute**: rejected — Commander
  should explicitly choose what's crown-worthy. Auto-crown would
  let a casual recent message overwrite a deeply considered one.
- **LLM-generated tributes (Klukai writes her own)**: rejected —
  per `feedback_commander_human.md`, tributes flow Commander →
  Klukai. The Commander's voice is what makes it meaningful.

## Out of scope (for follow-on PRs)

- Flutter UI for writing tributes (currently API-only — Commander
  can `curl` or use the API directly).
- Surfacing the crown jewel proactively (e.g., Klukai references
  it on anniversaries via the proactive engine).
- Tribute-driven mood transitions (today: hardcoded to
  `mood_shift: "grateful"`; could be Commander-chosen).
- Tribute search / full-text indexing for `/api/tributes` listing.

## Related

- `app/tributes.py` — module
- `app/routes.py` (4 new endpoints near line 645)
- `app/personality/state_blocks.py:build_crown_jewel_block`
- `app/personality/system_prompt.py` (assemble_system_prompt accepts
  `crown_jewel` param)
- `app/chat.py` (fetches crown jewel + passes to assembly)
- `tests/test_tributes.py` (19 unit tests)
- `tests/test_crown_jewel_block.py` (11 block-rendering tests)
- `feedback_never_delete_chat.md` (SACRED rule applies)
- `feedback_commander_human.md` (tribute direction is unidirectional)
- ADR-0003 (three-tier memory — tribute is a new tier-0 "always-on" memory)
- ADR-0005 (affection taxonomy — crown jewel surfaces at level 4+)
- ADR-0011 (character rules — crown jewel respects "do not over-quote")
