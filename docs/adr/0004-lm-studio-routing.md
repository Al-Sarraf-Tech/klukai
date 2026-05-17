# ADR-0004: LM Studio model routing (gemma-4 / dolphin / gpt-oss)

- **Date:** 2026-04 (formalized 2026-05-16)
- **Status:** Accepted
- **Authors:** jalsarraf

## Context

klukai needs LLM inference for three distinct task types:

1. **Conversational chat** with Klukai's personality (creative,
   character-driven, sometimes intimate).
2. **JSON extraction** (facts from messages, mood classification,
   gift parsing) — needs strict structured output.
3. **Quick utility** (one-off short responses, classification tags,
   sanity checks) — speed matters more than depth.

Running everything on one model wastes VRAM (chat-tuned models are
slow at JSON; JSON-tuned models are wooden at chat). dominus has
24GB VRAM (RTX 3090) shared with ComfyUI image gen.

## Decision

Route by task type to three models, all served by LM Studio on dominus
at `192.168.50.2:1234`:

| Model | Role | Why |
|---|---|---|
| `gemma-4-e2b-it` (Q4_K_M, 2.9GB) | Quick utility + ambient | Always loaded, fast, small VRAM footprint |
| `dolphin-2.9.4-llama3.1-8b` | Conversational chat + memory annotation | Creative, character-stable, good at long-form |
| `gpt-oss-20b` | JSON extraction + memory selection | Disciplined, follows schemas, strict output |

Routing logic lives in `app/llm_router.py`. Per
`feedback_dolphin_for_annotations.md`: Dolphin for creative text,
gpt-oss for JSON only, gemma-4 for quick fixes.

Per `feedback_llm_load_on_demand.md`: LM Studio JIT TTL (600s)
handles model lifecycle. Models load on demand, unload after idle.
First request after long idle pays the cold-start tax — this is
intentional (see `docs/runbooks/lm-studio-cold.md`).

## Consequences

- **VRAM contention**: chat + image gen on the same GPU. Image gen
  evicts the chat model. Mitigation: 5s VRAM-contention delay built
  into image gen (commit `c8ad96f` April 2026).
- **No Anthropic fallback by default** — `ANTHROPIC_API_KEY` is
  optional. When present, used as last-resort fallback if LM Studio
  is unreachable.
- **First-message-after-idle is slow** (15-60s depending on model).
  This is documented behavior, not a bug.
- **No model fine-tuning** — using off-the-shelf models per
  `feedback_local_llm.md`. The character is in the personality YAML
  + system prompt, not the model weights.

## Alternatives considered

- **One big model** (Dolphin for everything): rejected — JSON
  extraction is unreliable, structured output frequently malformed.
- **OpenAI / Anthropic only**: rejected — privacy (all chat content
  is private to the Commander) + cost + offline capability.
- **Always-resident models**: rejected — eats VRAM that image gen
  needs. JIT TTL is the right trade-off.

## Related

- `app/llm_router.py`
- `feedback_dolphin_for_annotations.md`
- `feedback_model_routing.md`
- `feedback_llm_load_on_demand.md`
- `feedback_local_llm.md`
- `docs/runbooks/lm-studio-cold.md`
- ADR-0007 (voice on dominus — same GPU)
