# Dream and scene provider preparation

The owner asked to research the best image backend among OpenAI/Codex,
Claude and Grok, and to continue until the goal is handled. Research on
2026-10-14 favors GPT Image 2.5 Sunburst for quality and Flare for speed.
Claude remains a text/vision model and can write dreams and scene descriptions.

The initial change prepared opt-in providers without activating paid production
traffic. The owner subsequently requested use of the existing Claude API key;
production now enables Anthropic creative text with 12 attempts per UTC month.
Fresh deployments remain local by default. Paid cloud images and an image
comparison still require separate authorization. See the
[activation record](../../handoff/2026-10-14-dream-image-preparation.md).

## Behavior

- Scene images select ComfyUI (default) or OpenAI Images API. Cloud dispatch
  happens before any local readiness, LM gate or GPU lease operation.
- OpenAI uses a pinned 2026-09-08 Sunburst or Flare snapshot, explicit quality
  and dimensions, one output per request, and configured local character
  reference images. No user-controlled provider URL or credential forwarding.
- Cloud image requests require a reference pack and opt-in. Model-specific
  diffusion weights are removed from the scene description; identity and
  wardrobe details remain. Returned bytes are verified and normalized to PNG
  for existing archive consumers. No automatic retry or provider hopping.
- Dreams and memory scene composition have a separate local/Anthropic selector.
  Opted-in requests use Sonnet 5.5 with medium adaptive thinking. Other local
  background jobs are unaffected. An interactive API flag alone is insufficient.
- Existing dreams remain text entries; adding illustrated dream entries is a
  separate feature. Live portrait/inpainting remains exclusively local.
- A SQLite attempt ledger on the existing persistent images volume atomically
  enforces daily and monthly caps separately for cloud image and creative text
  requests. Defaults are zero. Reserve before network dispatch; failures and
  uncertain timeouts consume the attempt. Failure to persist denies dispatch.
  These are request caps, not a guarantee of a fixed dollar price for token-
  billed APIs. Record usage where available; do not log prompts or secrets.
- All paid image entry points pass the same dispatcher. Historical memory
  seeding remains local and refuses to run before database, GPU or API work
  whenever either creative provider is remote. Its existing whole-run cursor
  and prompt-based deduplication cannot prevent repeat billing after partial
  failure. Remote archive seeding requires durable source-aware jobs first.

## Verification and rollout

Use mocked HTTP transports and real temporary SQLite files. Verify defaults,
disabled/keyless paths, concurrency and restart persistence, failed requests,
Dominus-offline cloud operation, response validation, references, exact model
and thinking configuration, all migrated call sites, and local portrait guards.
Run the existing core suite and review the complete diff. No paid calls,
production environment edits, service restarts or remote pushes in preparation.

Adoption requires a bounded blind comparison against existing local images:
face, green eyes, tear mark, silver hair, cross ornament, wardrobe, pose,
two-character separation, dream scenes and repeated edits. Broad leaderboards
do not prove Klukai-specific identity fidelity.
