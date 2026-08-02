# ADR-0004: Local model routing through the compatibility gateway

- **Date:** 2026-04 (formalized 2026-05-16)
- **Updated:** 2026-08-01 (Nobara rebuild)
- **Status:** Accepted
- **Authors:** jalsarraf

## Context

Klukai needs private local inference for conversational character work,
structured extraction, agent/tool use, embeddings, and operator-selected
specialized jobs. The RTX 3090 on `dominus-nobara` is also shared by voice and
ComfyUI, so an always-resident model would interfere with those workloads and
with games.

The original implementation used LM Studio and a smaller three-model policy.
That Windows/WSL2 installation is gone. The recovered fleet is larger, and its
exact bytes, aliases, quantizations, paths, and enabled state are now recorded
in `ops/dominus-nobara/models.lock.json`.

## Decision

`companion-core` keeps the existing OpenAI/LM-Studio-compatible client
contract, but sends it over Tailscale to the authenticated, CPU-only
`lmstudio-compat` gateway on `100.107.121.5:1234`. The gateway admits only
aliases in the immutable model lock and forwards inference to the internal
pinned llama.cpp router. The router can hold at most one preset.

The application has two primary routing aliases:

| Alias | Role |
| --- | --- |
| `cognitivecomputations_dolphin-mistral-24b-venice-edition` | Default conversation, creative text, and current extraction/annotation paths |
| `qwen3.5-27b-claude-4.6-opus-reasoning-distilled-v2` | Agent and tool-use paths |

The lock also preserves the recovered optional chat, VLM, reasoning,
embedding, and utility catalog. Those entries are selectable only by their
locked aliases; their presence does not make them resident. This ADR does not
replace or abbreviate that catalog: the lock is the source of truth.

LLM residency has a hard 15-minute ceiling. llama.cpp uses a fixed
`--sleep-idle-seconds 898`, allowing for its approximately one-second polling
interval, and the gateway strips client-provided TTL values. Native vLLM uses
a separate fixed 895-second process-stop watchdog. Health, catalog, and idle
keepalive traffic may not extend either deadline.

ComfyUI and companion voice acquire the bounded GPU lease before loading
weights. Lease acquisition blocks new LLM work, drains and unloads llama.cpp,
stops native vLLM, and verifies quiescence. An expired or failed-cleanup lease
remains fail-closed until positive cleanup removes all leased workload residue.

## Consequences

- First use after idle pays a cold-load cost; this is expected behavior.
- At most one locked llama.cpp preset is resident, and it unloads by the
  900-second ceiling.
- The public compatibility API and legacy `LM_STUDIO_*` configuration names
  remain stable even though LM Studio itself is not deployed.
- Local inference remains private and offline. An explicitly configured cloud
  fallback remains optional rather than a residency mechanism.
- The character remains defined by Klukai's personality and memory system,
  not by fine-tuning or an always-loaded model.

## Alternatives considered

- **Restore LM Studio on another desktop host:** rejected because the lost
  Windows/WSL2 host is not a deployment or rollback target.
- **One model for every task:** rejected because conversational and agentic
  requirements differ, while the recovered locked fleet already preserves
  specialized choices.
- **Always-resident models or keepalives:** rejected because they violate the
  shared-GPU and 15-minute residency requirements.
- **Cloud-only inference:** rejected for privacy, cost, and offline operation.

## Related

- `ops/dominus-nobara/models.lock.json`
- `ops/dominus-nobara/RUNBOOK.md`
- `docker/core/app/llm_router.py`
- `docs/runbooks/lm-studio-cold.md`
- ADR-0006 (image generation on the shared GPU)
- ADR-0007 (voice on the shared GPU)
