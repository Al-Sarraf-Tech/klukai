# Klukai Project Telegram Bot — Design Spec

**Date:** 2026-04-06
**Project:** Companion (Klukai)
**Bot:** @KlukaiProjectBot

## Overview

A Telegram bot for real-time project management of the Companion/Klukai project. Three layers: instant ops commands, a persistent Claude Code agent for dev work, and push notifications for events and health changes. Runs as a single Python process on dominus.

**Core principles:**
- Ops commands are instant (shell, no LLM)
- Dev instructions go through a persistent Claude Code session with full autonomous permissions
- Notifications push to you — you never have to ask
- Single-user lockdown: your Telegram ID is the only authorized sender

## Architecture

```
 Telegram API (long-polling)
        │
        ▼
┌─────────────────────────────────────────────────┐
│  dominus (host)                                  │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │  klukai-bot (systemd user service)       │   │
│  │                                          │   │
│  │  ┌──────────┐  ┌──────────┐  ┌────────┐ │   │
│  │  │ Ops      │  │ Agent    │  │ Notify │ │   │
│  │  │ Handler  │  │ Handler  │  │Handler │ │   │
│  │  └────┬─────┘  └────┬─────┘  └───┬────┘ │   │
│  └───────┼──────────────┼────────────┼──────┘   │
│          │              │            │           │
│          ▼              ▼            ▼           │
│   subprocess       claude CLI   Redis pub/sub   │
│   (docker, curl,   (--resume,   (companion:     │
│    make, ssh)       stream-json)  events)        │
│                         │                        │
│  ┌──────────────┐  ┌───┴──────────────────┐    │
│  │companion-core│  │ tmux pane            │    │
│  │  (Docker)    │  │ claude-companion-agent│    │
│  └──────────────┘  └──────────────────────┘    │
│                                                  │
│  ┌──────────────────────┐                       │
│  │ companion-voice      │                       │
│  │   (Docker + GPU)     │                       │
│  └──────────────────────┘                       │
└─────────────────────────────────────────────────┘
```

**Why dominus:** Everything the bot controls (Docker, Claude Code, tmux) lives on dominus. Running elsewhere adds SSH for every operation.

**Why host-native, not Docker:** The agent layer manages tmux sessions and spawns Claude Code. This is fundamentally a host-level concern.

**Single asyncio event loop** runs Telegram polling, health checks, Redis subscriber, and agent I/O. No threads, no multiprocessing.

## Ops Layer

Direct shell commands. No LLM. Sub-second response.

| Command | Action | Shell |
|---|---|---|
| `/status` | Container states | `docker compose ps` |
| `/health` | Core + voice health | `curl -sf localhost:8300/health && curl -sf localhost:8301/health` |
| `/deploy` | Build + restart | `make build && make run` |
| `/logs [service] [n]` | Tail logs | `docker logs --tail={n} companion-{service}` |
| `/restart [service]` | Restart service | `docker compose restart companion-{service}` |
| `/voice` | Voice status | Health endpoint + model info |
| `/gateway` | Amarillo gateway | `ssh amarillo docker ps \| grep gateway` |
| `/db` | DB quick stats | SQL: message count, episode count, affection level |
| `/session` | Agent session info | Running? Session age, context |

**Output:** Telegram monospace blocks. Truncated at 4000 chars with a note if exceeded.

**Errors:** stderr returned immediately. No silent failures.

## Agent Layer

Persistent Claude Code session via CLI, managed through `--resume`.

### Session lifecycle

1. **First message** (no active session):
   ```
   claude -p "user message" \
     --dangerously-skip-permissions \
     --output-format stream-json
   ```
   Bot captures session ID from JSON output.

2. **Subsequent messages** (active session):
   ```
   claude -p "user message" \
     --resume <session-id> \
     --dangerously-skip-permissions \
     --output-format stream-json
   ```
   Full conversation context preserved.

3. **Idle recycling** (30 min no messages):
   Discard session ID. Next message starts fresh. Project memory (MEMORY.md, CLAUDE.md) means a fresh session still knows the project.

4. **Crash/timeout recovery:**
   Same as idle recycling — transparent restart on next message.

### I/O flow

```
Telegram message arrives
  → Bot replies "Working on it..." (immediate ack)
  → Spawns claude CLI as async subprocess
  → Reads stream-json stdout line by line
  → Filters noise (routine Read/Glob calls)
  → Forwards meaningful progress to Telegram
  → On completion: sends result summary + duration + files changed
```

The Claude CLI process is spawned as an async subprocess by the bot — stdin/stdout is the I/O channel. Optionally, the bot can launch it inside tmux pane `claude-companion-agent` for crash survivability (if the bot restarts mid-task, it can reattach). But tmux is a safety net, not the I/O mechanism.

### What gets forwarded to Telegram

- Text output from Claude (assistant messages)
- Tool results that indicate progress (test results, build output, errors)
- Commit messages when code is committed
- Final summary on completion

### What gets filtered

- Routine file reads and searches
- Internal reasoning/planning
- Repetitive tool call metadata

## Notification Layer

Three sources, all push.

### Health monitor (bot-internal)

- Polls `/health` on core and voice every 60 seconds
- **State-change alerting only** — one message when something goes down, one when it comes back up
- 3 consecutive failures before alerting (avoids flapping)
- Health alerts always go through, regardless of quiet hours

### Redis pub/sub (from companion-core)

- companion-core publishes to `companion:events` Redis channel
- Events surfaced:
  - Proactive messages fired (morning/evening from Klukai)
  - Affection level changes
  - Error conditions (LLM failures, voice failures, DB issues)
- Requires adding `publish_event()` helper to companion-core (~10 lines in key spots)
- Informational events respect quiet hours (23:00-08:00)

### Agent completion (bot-internal)

- Claude Code task finished: result summary + duration + what changed
- Claude Code task failed/timed out: error details immediately

### Quiet hours

Follows existing proactive engine schedule: `QUIET_HOUR_START = 23`, `QUIET_HOUR_END = 8`. Health alerts bypass quiet hours. Everything else queues until morning.

## Security & Access Control

- **Single-user lockdown:** Only the authorized Telegram user ID can interact. Unknown senders are silently ignored — no error, no acknowledgment.
- **Token storage:** `.env` file on dominus, mode 600, loaded via systemd `EnvironmentFile=`. Never in code, never committed to git.
- **Claude Code permissions:** `--dangerously-skip-permissions` per absolute directive. The Telegram user ID gate is the authorization boundary — if the message came from you, it's authorized.
- **No inbound ports:** Telegram long-polling is outbound HTTPS only. Nothing listening on the network.

## Project Structure

```
companion/
├── telegram/
│   ├── bot.py              # Entry point, dispatcher, Telegram polling loop
│   ├── ops.py              # Command handlers → shell execution
│   ├── agent.py            # Claude Code session lifecycle + message piping
│   ├── notify.py           # Health monitor + Redis subscriber + Telegram push
│   ├── config.py           # Env vars, constants, Telegram user whitelist
│   ├── requirements.txt    # python-telegram-bot, redis
│   └── klukai-bot.service  # systemd user unit file
```

## Service Configuration

- **Runtime:** systemd user service under jalsarraf on dominus
- **Restart policy:** `Restart=always` — auto-recovers from crashes
- **Persistence:** `loginctl linger` (already enabled)
- **Logging:** `/mnt/nvmeINT/logs/klukai-bot.log`
- **Dependencies:** `python-telegram-bot>=21.0`, `redis[asyncio]>=5.0`

### systemd unit (klukai-bot.service)

```ini
[Unit]
Description=Klukai Project Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/jalsarraf/git/companion
ExecStart=/home/jalsarraf/git/companion/telegram/.venv/bin/python -m telegram.bot
EnvironmentFile=/home/jalsarraf/git/companion/telegram/.env
Restart=always
RestartSec=5
StandardOutput=append:/mnt/nvmeINT/logs/klukai-bot.log
StandardError=append:/mnt/nvmeINT/logs/klukai-bot.log

[Install]
WantedBy=default.target
```

## Companion-Core Changes

Minimal additions to enable Redis event publishing:

1. **Add Redis publish helper** — `app/events.py` with `publish_event(event_type, data)` function
2. **Publish from key spots:**
   - `proactive.py` — when a proactive message fires
   - `affection.py` — when affection level changes
   - `main.py` — on LLM/voice/DB errors in the message handling loop
3. **Add `redis[asyncio]` to core requirements** (Redis URL already in env vars)

Estimated: ~30 lines of new code across 4 files.

## Future Extensions (not in scope)

- Klukai in-character responses via Telegram (call companion-core API from bot)
- Multi-user support (other authorized users)
- Telegram inline keyboards for interactive approvals
- File/image sharing (screenshots, generated images from ComfyUI)
- Webhook mode instead of long-polling (if latency matters)
