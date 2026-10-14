# Cloud dream and picture provider preparation

Status: included in v1.1.0 with cloud creative generation disabled by default.
Following the owner's request to use the existing Claude API key, production
now enables Anthropic dream and Her POV writing. Images remain local ComfyUI.
See the [release record](../releases/v1.1.0.md) for release validation.
Interactive Sonnet and local Arc voice remain independent of these settings.

## Claude text activation — 2026-10-14

The owner asked, "Could you use the claude api key", in the creative-provider
discussion. The existing production key was reused without copying it into
source, logs, commands or messages. Only these production environment settings
changed; the image and interactive-chat settings were preserved:

```dotenv
KLUKAI_CREATIVE_TEXT_PROVIDER=anthropic
KLUKAI_CLOUD_TEXT_DAILY_LIMIT=12
KLUKAI_CLOUD_TEXT_MONTHLY_LIMIT=12
```

This initial allowance permits **12 total attempts per UTC month**, shared by
night dreams, morning-return dreams and Her POV scene writing across all users.
Failed attempts count. The daily setting does not multiply the monthly limit.
Once exhausted, creative text requests stop until the allowance resets; limits
are not automatically raised. It is not a hard dollar cap.

Live checks used synthetic content and the deployed adapter with the existing
key, Sonnet 5.5, adaptive thinking and medium effort:

- Dream prose: 256 characters in 2.09 seconds; 89 input and 88 output tokens.
- The actual `compose_pov` path returned valid annotation, scene tags, couple,
  mood and title fields in 2.75 seconds.
- Both calls reserved persistent text attempts; they count toward the allowance.
- 51 focused provider, quota, routing and creative E2E tests passed.
- No nonterminal Her POV jobs were present before restarting the core.
- The core was recreated from the same v1.1.0 image. Its active settings match
  the configuration above; public and local health checks passed afterward.
- The ledger retained both attempts and their usage after recreation, leaving
  ten attempts in October's initial allowance at verification time.

No private conversation history or reference image was used by these checks.
Claude writes scene descriptions; ComfyUI still renders pictures and requires
Dominus to be available. A scene-writing attempt can be consumed even if the
later local render fails. Cloud historical memory seeding remains disabled,
and interrupted in-flight Her POV jobs require review before another attempt.
Ordinary reflections and scheduled template greetings keep their existing paths.

To revert, set the text provider to `local` and both text limits to `0`, then
recreate only `companion-core` using the existing image. Keep the attempt ledger.

## Selected design

See the [research](../research/2026-10-14-dream-image-backends.md),
[specification](../superpowers/specs/2026-10-14-dream-image-providers.md),
and [12-scene comparison](../research/2026-10-14-dream-image-evaluation.json).
Independent evidence favors GPT Image 2.5 Sunburst for quality; Flare is the
faster alternative. Claude writes dreams and memory scenes but does not render
raster pictures. No paid comparison has been run.

Cloud rendering dispatches before Dominus's LM/GPU gates. Images use a canonical
OpenAI Images edit request with a selected character reference pack, a pinned
model, max quality and explicit dimensions. Reference metadata is removed before
upload; returned images are verified and normalized to PNG. Cached and newly
generated live portraits/inpainting retain their local pipeline.

The separate Anthropic creative path covers overnight dream text, the morning
return dream, and Her POV annotation/scene writing. It uses Sonnet 5.5 with medium
adaptive thinking. Ordinary reflections and other background tasks remain local.
Dream entries remain text-only; this change does not add illustrated dream entries.

## Activation prerequisites

The owner has authorized Anthropic interactive chat and the separately capped
creative text deployment above. Paid cloud image generation still requires
further authorization. A key's presence alone does not enable either provider.

Before activation, select and inspect the fictional-character reference files,
record their digests and explicitly approve their cloud use. Do not auto-upload
private conversation archives or the Commander's real photograph. Send the
selected scene and approved references only. Store credentials in the existing
ignored, permission-restricted environment mechanism; never in a reference
manifest, command line, log or RabbitMQ event.

The default Compose/example configuration is:

```dotenv
KLUKAI_IMAGE_PROVIDER=comfyui
KLUKAI_CREATIVE_TEXT_PROVIDER=local
KLUKAI_IMAGE_REFERENCE_FILES=[]
KLUKAI_CLOUD_IMAGE_DAILY_LIMIT=0
KLUKAI_CLOUD_IMAGE_MONTHLY_LIMIT=0
KLUKAI_CLOUD_TEXT_DAILY_LIMIT=0
KLUKAI_CLOUD_TEXT_MONTHLY_LIMIT=0
```

An authorized pilot would select `KLUKAI_IMAGE_PROVIDER=openai`, provide
`OPENAI_API_KEY`, and put a JSON list of absolute reference paths in
`KLUKAI_IMAGE_REFERENCE_FILES`. Files under the existing read-only `/config`
mount can be provisioned without a new volume. Supported models are
`gpt-image-2.5-sunburst-2026-09-08` and `gpt-image-2.5-flare-2026-09-08`; only
`max` quality is prepared. Keep model/settings fixed during a comparison.

`KLUKAI_CREATIVE_TEXT_PROVIDER=anthropic` independently enables the new text
path with `ANTHROPIC_API_KEY`. Both daily and monthly limits for each enabled
kind must be positive. A key, the interactive-chat flag, or personal mode alone
cannot authorize a creative request. Limits apply to all users together.

## Attempt accounting and recovery

Compose pins the ledger to `/images/.creative/attempts.sqlite3` on the existing
persistent images volume, shared by the web worker and container-exec jobs.
SQLite transactions reserve before dispatch and count against UTC day/month
limits across processes and restarts. Errors, cancellations and uncertain
timeouts consume their attempt. Do not delete the ledger to retry a job.
Only numeric usage telemetry is stored, not prompts, reference paths or keys.
Storage failures deny requests. Personal-mode quotas do not bypass this ledger.

These are **attempt limits, not a hard dollar billing cap**. Image2.5 prices are
token based; model, dimensions and reference inputs change cost. AA's current
max-quality reference is about $0.21/image before this application's extra
inputs. Measure actual usage during the first explicitly authorized pilot.
No automatic provider retry or fallback runs after an API failure/refusal.

Cloud Her POV execution requires durable queue ownership. Database or queue
failures must not fall back to an unclaimed inline paid call. An interrupted
in-flight cloud job needs inspection before any explicit new request; do not
assume a timeout means the provider did no work or charged nothing.

Historical memory seeding exits before reading history or running inference
whenever either creative provider is cloud. Its existing local retry/watermark
logic cannot safely deduplicate paid partial batches. Source-aware durable
backfill jobs are a separate prerequisite; enabling cloud scene generation
does not silently purchase a historical backlog.

## Initial preparation evidence

Preparation verification on 2026-10-14:

- Full core suite: 4,742 passed, 57 skipped, 1 deselected; coverage 96.74%,
  above the required 95%. Output: `/tmp/klukai-dream-image-core-tests.log`.
- After fixing a test-helper SQLite connection leak, all 91 new budget,
  provider, creative text and path tests passed with ResourceWarning as error.
- Independent review: 274 focused tests passed; both paid queue ownership
  findings were fixed and reviewed. Remaining task-creation warnings trace
  to existing tests that deliberately break asyncio task creation.
- Scoped Ruff, mypy for all three new modules, compilation, JSON manifest,
  Compose validation with dummy credentials, and `git diff --check` passed.
- This initial preparation made no paid requests or production changes.
  The later v1.1.0 release record supersedes its build and publication status.

A candidate reference was copied locally from the owner's existing fictional
range-kit portrait and visually inspected in a private local working directory,
without uploading or committing it.
It shows the established silver hair, green eyes, cross ornaments and tear
mark. It is not yet an approved reference pack or an identity benchmark result.

For a future cloud image pilot, run the offline core suite and targeted
provider/ledger/queue tests. Verify
Compose without printing resolved secrets. After separate paid authorization
and reference adoption, run the fixed blind comparison and record returned
usage, latency, refusals and identity failures. Then test the selected route
with Dominus off. No offline HTTP mock establishes live image quality or
account entitlement. Keep the existing local configuration available for
rollback; reverting providers does not require touching the GPU stack.
