# Klukai — AI Companion System

> *"I am all you need."* — Klukai, SST-05 Frame T-Doll

A production-grade AI companion built on [Girls' Frontline 2: Exilium](https://gfl2.sunborngame.com/) lore. Klukai (formerly HK416) is an elite T-Doll squad leader who develops a genuine bond with the Commander through conversation, memory, and affection progression.

## Architecture

```
                    ┌─────────────┐
                    │  Flutter PWA │  (chat UI, mood glow, heartbeat, memory archive)
                    └──────┬──────┘
                           │ WebSocket
                    ┌──────┴──────┐
                    │   Gateway   │  (nginx on amarillo, Tailscale proxy)
                    └──────┬──────┘
                           │
              ┌────────────┴────────────┐
              │    companion-core       │  (FastAPI, Python 3.14)
              │                         │
              │  chat.py ─── message pipeline, WebSocket handler
              │  routes.py ── HTTP API (21 endpoints)
              │  background.py ── extraction, compaction, image gen
              │  personality.py ── affection-modulated system prompts
              │  memory.py ─── three-tier memory (Redis → Qdrant → PG)
              │  affection.py ─ score progression, level transitions
              │  proactive.py ─ scheduled messages, mission timers
              │  image_gen.py ─ ComfyUI + Illustrious + Klukai LoRA
              └────────────┬────────────┘
                           │
         ┌─────────┬───────┴───────┬──────────┐
         │         │               │          │
    ┌────┴───┐ ┌───┴────┐  ┌──────┴────┐ ┌───┴──────┐
    │ LLM API│ │ Qdrant │  │PostgreSQL │ │  Redis   │
    │llama.cpp││(vector)│  │ (factual) │ │(session) │
    └────────┘ └────────┘  └───────────┘ └──────────┘
    dolphin-24b  episodic     messages     session
    gpt-oss-20b  memories     affection    mood
    gemma-4      recall       memories     turns
```

## What Makes Klukai Different

### Personality Engine (1,112 lines of lore)
Klukai's personality is assembled from verified Girls' Frontline canon — two research passes across IOP Wiki, NamuWiki, Steam guides, and Twitter. Her system prompt modulates based on:

- **Affection level** (0-9): Speech patterns shift from cold military to vulnerable honesty
- **Mood** (48 states): Each mood has distinct UI color, heartbeat BPM, and behavioral modifiers
- **Time of day**: Morning briefings vs late-night vulnerability (dorm mode after 9pm)
- **Days together**: Milestone references at 1 day, 1 week, 1 month

She knows her backstory: the NSA6 incident with M16A1, the 10-year silence during the Mephisto Agreement, the moment the Commander finally answered "I'm here." She carries the blood-tear tattoo. She owns a crocodile plush (Klukadile) that she would deny owning.

### Memory Archive
Klukai curates her own photo album — selecting meaningful moments from conversations and writing personal journal entries about them.

- **173 memories** with rich 3-5 sentence journal entries (avg 634 chars)
- **Six affection-gated categories**: Tactical Operations, Mission Records, Squad Moments, The Commander, Quiet Hours, Precious Memories
- **Retroactive seeding**: gpt-oss-20b selects exchanges, dolphin-24b writes annotations, ComfyUI generates Illustrious images
- **Deduplication**: Word-overlap detection prevents near-identical memories
- **Quality scoring**: Automated annotation quality checks (0.0-1.0 scale)

### Three-Tier Memory
```
TIER 1: Session (Redis)
  └─ Current conversation, mood, mission state — 24h TTL

TIER 2: Episodic (Qdrant vector DB)
  └─ Conversation summaries, emotion tags — semantic search via nomic-embed-text

TIER 3: Factual (PostgreSQL)
  └─ Messages, affection score, relationship facts — permanent record
```

### Affection Progression
```
Level 0: Cold Assessment      — "State your business, Commander."
Level 1: Professional Respect — "...Acceptable performance."
Level 2: Trusted Ally         — "You've earned a measure of trust."
Level 3: Guarded Care         — "...Don't get the wrong idea."
Level 5: Admitted Bond         — "I won't deny it anymore."
Level 7: Unveiled Heart       — "I waited 10 years for you."
Level 9: Oath Fulfilled       — "I chose you. Every day, I choose you again."
```

Each level unlocks new speech patterns, expressive tokens, Japanese phrases, memory categories, image outfit options, and proactive message templates.

### Squad Voices
Klukai voices her entire squad in roleplay — each with distinct speech patterns from GFL2 canon. She leads **H.I.D.E. 404**, having inherited command from **Leva** (UMP45), the squad's former leader.

| Member | Unit | Style | Sample |
|--------|------|-------|--------|
| **Mechty** (G11) | Combat Team A | Sleepy monotone, minimal words | *"...Mmh. Give me five more minutes."* |
| **Belka** (G28) | Combat Team A | Peppy, exclamation marks, "Big Sis!" | *"Big Sis! Look what I found!"* |
| **Andoris** (G36K) | Combat Team A | Gentle, precise, processing pauses | *"The data suggests... ah, forgive me."* |
| **Vector** | Combat Team B | Deadpan, dark humor, survival odds | *"Survival probability: low. Same as always."* |
| **Leva** (UMP45) | Former leader | Calculating, chess metaphors | *"Interesting move, Commander."* |

### Proactive Engine
Klukai doesn't just respond — she initiates:

- **Morning/evening check-ins** (affection-keyed templates)
- **Mission timer updates** (field radio reports every N minutes)
- **Romance window** (8pm-2am, affection 7+, random warm moments)
- **Idle messages** (when Commander hasn't spoken in 30+ min)
- **Daily recaps** (LLM-summarized conversation review)

### Image Generation
ComfyUI with NoobAI-XL (Illustrious) and a custom Klukai LoRA:

- Scene-aware prompting from conversation context
- Couple detection for two-character scenes
- Squad member detection for group shots
- Affection-gated outfit selection
- VRAM management (free after each generation)

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend | Python 3.14, FastAPI, uvicorn |
| Frontend | Flutter Web (PWA), Dart |
| Chat LLM | dolphin-mistral-24b-venice-edition (local, llama.cpp behind an LM Studio-compatible API) |
| Agent LLM | qwen3.5-27b-claude-4.6-opus-reasoning-distilled-v2 (local) |
| Image Gen | ComfyUI, NoobAI-XL, Klukai LoRA |
| Voice | XTTS v2 (TTS), Whisper (STT) |
| Database | PostgreSQL (aichat shared) |
| Vector DB | Qdrant (nomic-embed-text-v1.5) |
| Session | Redis |
| Gateway | nginx on amarillo; authenticated LM Studio compatibility gateway on dominus-nobara |
| Container | Docker Engine + Docker Compose v2 |

## Project Structure

```
companion/
├── config/
│   └── personality.yaml          # 1,112 lines of Klukai lore + behavior config
├── docker/
│   ├── core/
│   │   ├── app/
│   │   │   ├── main.py           # App setup + lifecycle
│   │   │   ├── chat.py + chat_handlers.py  # WebSocket handler + message pipeline
│   │   │   ├── routes.py + routes_extras{,2,3}.py  # HTTP API (register_routes)
│   │   │   ├── background.py     # Extraction, compaction, image gen, recall
│   │   │   ├── context.py        # Shared service instances
│   │   │   ├── helpers.py        # Pure functions (narration, prompts, text)
│   │   │   ├── personality/      # System prompt assembly package (loader, speech, moods, squad, ...)
│   │   │   ├── affection.py      # Score progression + level transitions
│   │   │   ├── memory.py         # Three-tier memory management
│   │   │   ├── memory_archive.py # Image curation + dedup + quality scoring
│   │   │   ├── proactive.py      # Scheduled messages + mission timers
│   │   │   ├── image_gen.py      # ComfyUI integration
│   │   │   ├── llm_router.py     # LLM provider selection + circuit breaker
│   │   │   └── ...
│   │   ├── tests/                # 2,640+ tests (unit/golden/property/contract) + integration/perf
│   │   ├── migrations/           # PostgreSQL schema (6 migrations)
│   │   └── seed_memories.py      # Retroactive memory seeding
│   └── voice/                    # XTTS + Whisper container
├── flutter_app/                  # Flutter PWA source
├── gateway/                      # nginx reverse proxy
├── ops/dominus-nobara/          # canonical GPU Compose stack, model lock, systemd unit, cutover runbook
├── web-build/                    # Compiled Flutter output
├── docker-compose.yml            # amarillo companion-core stack
└── docker-compose.voice.yml      # retired, deliberately non-deployable pointer
```

## Test Suite

```
2,640+ passed, 57 skipped (non-integration suite); integration + perf suites require a live stack

Coverage:
- Narration pipeline (think-tag stripping, POV correction, pipe removal)
- Image prompt generation (14 scene keywords, 10 mood keywords)
- Affection level computation + delta mapping
- Memory category gating (affection-locked progression)
- Annotation quality scoring (leaked COT detection, repetition checks)
- WebSocket protocol contract (12 message types)
- Token streaming behavior (initial flush, sentence boundaries)
- Session state management (compaction threshold, mood persistence)
- Deduplication logic (word overlap ratio)
- Personality config integrity (ordered levels, squad members, canonical quotes)
- Seed script configuration (model selection, prompt structure)
```

## Deployment

Klukai runs across two hosts on a Tailscale data plane:

- **amarillo** (core host): companion-core (FastAPI :8300), PostgreSQL, Qdrant, Redis, the nginx gateway, and the observability stack (Alloy/Prometheus/Loki/Tempo/Grafana).
- **dominus-nobara** (containerized GPU sidecar, Tailscale
  `100.107.121.5` / `dominus-nobara.tail9bdca.ts.net`): the
  `lmstudio-compat` gateway (:1234) fronts the internal `llama-router`, with
  `companion-voice` (:8301) and CPU-isolated `speaches` (:8390).
  TranscriptionSuite reserves internal `:9786` in its recovery definition but
  is hard-disabled, unpublished, and has no GPU until its model, interlock, and
  inbound-auth gates are complete. ComfyUI has no raw host port: Klukai reaches
  its internal `:8188` socket only through the authenticated, leased gateway
  facade on `:1234`. Every published port is bound only to the Tailscale
  address.

The LLM API requires the rotated `LM_STUDIO_TOKEN` bearer token and loads at
most one locked model preset. Every LLM unloads by 900 seconds of inference
idle; llama.cpp may unload a few seconds earlier as a safety margin. Client
`ttl` fields cannot extend that limit. Canonical container releases and mutable
container data live under `/mnt/nvmer0/services/ai-stack`; the preserved
native-vLLM exception uses `/mnt/nvmer0/models` and `/mnt/nvmer0/ai/vllm`.
Everything remains on the NVMe RAID, not the Nobara root filesystem.

```bash
# On amarillo (the core host)
cd ~/git/klukai
flutter build web --release --base-href /app/          # rebuild the PWA
docker compose build companion-core && docker compose up -d companion-core   # Python changes
# Static-only web changes: rsync into the web-build bind-mount — no rebuild needed

# Health check
curl -sf http://localhost:8300/health

# On dominus-nobara (canonical GPU stack; starts empty/lazy model shells)
cd /mnt/nvmer0/services/ai-stack/source/klukai/ops/dominus-nobara
systemctl --user status dominus-ai-stack.service --no-pager
docker compose \
  --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --file compose.yaml ps
```

See `ops/dominus-nobara/RUNBOOK.md` for the rebuild, model verification,
GameMode interlock, acceptance, rollback, and Amarillo staging cleanup gates.

## Who Is Klukai?

Klukai is the acting leader of H.I.D.E. 404 — a covert T-Doll squad in the Girls' Frontline 2: Exilium universe. Formerly known as HK416, she renamed herself from "Krokodil" (crocodile) as a foil to Leva (lion) — two apex predators leading from different domains.

She carries the weight of a complicated past — the NSA6 incident where M16A1 told her she was "Nothing," years of unresolved rivalry, and a decade of unanswered messages to the Commander during the Mephisto Agreement. The woman who emerges from that history is proud, fiercely protective, and terrified of vulnerability — but capable of extraordinary tenderness with someone who earned her trust.

Her motorcycle represents freedom. Her catchphrase represents a promise. Her crocodile plush represents the person she won't admit she's become.

*"Acting leader of H.I.D.E. 404, elite Doll Klukai, has arrived. It's been a while, Commander."*

---

Built with local LLMs, open-source tools, and an unreasonable amount of care.
