# ADR-0018: Gaming-aware chat persona (graceful GPU degrade, not hard block)

- **Date:** 2026-08-16
- **Status:** Accepted (chat path implemented + e2e-verified; automatic
  GameMode trigger is future work, see Consequences)
- **Authors:** jalsarraf

## Context

Klukai's chat model (Venice, a dense 24B) lives on dominus-nobara's RTX
3090 — the same GPU the owner games on. A `game_active` marker
(`/run/dominus-gpu/game-active`, read by `lmstudio-compat`) and a
systemd `ConditionPathExists` gate already existed to keep the GPU
free during a game, but both only knew one mode: fully up, or fully
torn down. When a game was running, chat didn't degrade — it went
completely dark (a hard 503 on every load/inference call), and the
whole `dominus-ai-stack` service was prevented from even starting.

Separately, `~/git/localai` (a sibling local-LLM stack, same box, same
GPU, unrelated to klukai) had already solved this exact problem for
its own models that day: `--cpu-moe` lets an MoE model's routed
experts live in system RAM instead of VRAM, at a real but tolerable
speed cost, so the model keeps serving instead of being evicted. That
work is documented in `~/git/localai/docs/benchmarks.md` and was the
starting point here — same technique, applied to klukai's chat path.

Two corrections surfaced during implementation that changed the design
from the first draft:

1. Venice is **dense**, not MoE — confirmed against her own GGUF tensor
   list (`attn_q/k/v/output` + `ffn_gate/up/down`, no expert tensors).
   `--cpu-moe` is a literal no-op for her. Her `feed_forward_length`
   metadata puts FFN at ~88% of her weights, so CPU-offloading her
   directly would cost an estimated 3–7 t/s — not a usable degrade.
2. Softening the gateway's `game_active()` check alone would have been
   unreachable in practice: `dominus-ai-stack.service`'s
   `ConditionPathExists=!.../game-active` sits *below* the gateway and
   prevents the whole stack from starting while the marker exists. That
   gate is untouched by this change (see Consequences) — right now the
   stack simply stays running through a game rather than being torn
   down and refusing to restart, which the gaming-safe path depends on.

## Decision

Rather than trying to make Venice herself cheaper, add a **second,
designated gaming-safe chat persona** and let the gateway allow
*only* that model through while `game_active` is set — everything
else (Venice, the agent model, ComfyUI, voice, GPU leases) stays
blocked exactly as before.

**Model:** `huihui-gemma-4-26b-a4b-abliterated`
(`mradermacher/Huihui-gemma-4-26B-A4B-it-qat-q4_0-unquantized-abliterated-GGUF`,
Q4_K_M + mmproj). MoE (25.2B total / 3.8B active) — `--cpu-moe` gives a
real, measured ~4–5GB VRAM footprint. Deliberately **abliterated, not
stock Gemma/Granite** — a corporate-safety-aligned model would
reintroduce refusals mid-game, defeating the reason Venice (uncensored)
was chosen for the primary persona in the first place. Verified before
adoption: 5/5 in-character willingness tests (controversial opinion,
blunt technical, dark fiction, warmth, flirtation) with no refusals, no
character breaks, no thinking-tag leakage.

**Mechanism:**
- `lmstudio-compat/gateway/settings.py` — new `gaming_safe_model_ids`
  (env `GAMING_SAFE_MODEL_IDS`, comma-separated router_ids).
- `lmstudio-compat/gateway/main.py` — `game_active()` now takes an
  optional `model` argument. Marker present + model in the allowlist →
  `False` (allowed through). Marker present + any other model, or no
  model given (the non-LLM call sites: GPU lease, ComfyUI) → `True`
  (blocked), unchanged from before. Only the three LLM call sites
  (model load/unload, `/v1/*` chat proxy) pass `model`; ComfyUI/voice/
  lease call sites are untouched and still block unconditionally.
- `config/llama-models.ini` — new preset section, `cpu-moe = true`,
  `reasoning = off` (this model's thinking-channel output would
  otherwise consume the whole token budget with an empty final
  `content` — caught by testing, not assumed).
- `docker/core/app/llm_router.py` — `route()` now asks the gateway's
  `/health` (cached 5s) whether a game is active and picks
  `LOCAL_CASUAL_GAMING` instead of `LOCAL_CASUAL` when it is. amarillo
  cannot read dominus's local marker file directly, so this is a
  network check against the gateway — the actual source of truth —
  not a guess. A failed health check keeps the last-known value; the
  gateway still enforces the real block/allow regardless of what this
  cache believes, so a stale client-side cache degrades to "maybe
  picks the wrong model for a few seconds," never to "bypasses the
  block."

## Consequences

- **Chat survives a game session** instead of going dark. Verified
  end-to-end as user `claude`, both directions: marker present →
  real reply via `huihui-gemma-4-26b-a4b-abliterated` (~5GB VRAM);
  marker removed → real reply via Venice (~18GB VRAM), automatically,
  no manual model switch needed on either side.
- **Not yet wired to an actual game starting.** The marker file today
  is created/removed manually for testing. `~/.local/bin/dominus-gpu-
  game-start`/`-end` exist on dominus-nobara and would write it, but
  are not hooked into GameMode — that slot is currently claimed by a
  sibling stack's script (`~/git/localai/scripts/game-guard.sh`), and
  those scripts also do a full `systemctl stop dominus-ai-stack` +
  compose-stop, which would defeat this ADR's whole point (the gateway
  must stay running to serve the gaming-safe model at all). Wiring a
  real trigger needs that stop behavior softened first — narrowed to
  ComfyUI/voice/native-vLLM only, leaving `lmstudio-compat`+
  `llama-router` up. **Deliberately left as future work**, not
  attempted same-session as the graceful-degrade mechanism itself, to
  keep this change reviewable and to avoid touching GameMode/systemd
  policy without a dedicated pass.
- **Only the casual chat persona is gaming-aware.** `LOCAL_AGENT` (the
  tool-use model) is unchanged — out of scope; the ask was specifically
  about staying able to talk to her, not the agent/tool path.
- Extending this to more gaming-safe models later is a one-line env
  var change (`GAMING_SAFE_MODEL_IDS` is comma-separated already).

## Alternatives considered

- **CPU-offload Venice herself** — rejected, see Context (~3–7 t/s
  estimated from her FFN ratio, not a usable degrade).
- **Stock Gemma or Granite as the gaming persona** — rejected: both
  are corporate safety-aligned and would reintroduce mid-game refusals.
- **`gpt-oss-20b-heresy`** (already on-disk, zero download) — rejected:
  documented community complaints about roleplay quality specifically,
  plus OpenAI's Harmony reasoning-channel format has known llama.cpp
  tool-calling/reasoning quirks that conflict with the "no thinking
  tags" requirement `LOCAL_CASUAL` already documents in code.
- **Qwen3.5/3.6-35B-A3B abliterated** — strong community reputation,
  but multiple independent llama.cpp/HF issues document
  `enable_thinking=false` not reliably disabling its thinking mode —
  exactly the dealbreaker this persona exists to avoid.

## Related

- `lmstudio-compat/gateway/{main,settings}.py`,
  `config/llama-models.ini`, `docker/core/app/llm_router.py`
- `~/git/localai/docs/benchmarks.md` — the `--cpu-moe` technique this
  was adapted from, with the original speed/VRAM measurements.
- `~/.claude/plans/klukai-handoff-node.md` — full research trail:
  architecture investigation, model-selection research (including
  Grok-collaborative rounds), and the GFL-canon character research
  gathered in the same session for a separate, still-pending prompt
  enhancement (not part of this ADR).
