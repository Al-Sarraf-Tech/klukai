# Klukai

Read `docs/handoff/2026-09-25-dominus-tailscale-publish.md` before touching the Dominus GPU stack, Docker startup, or the chat path to the RTX 3090.

Powering that computer off is normal. On boot, Docker can restore the AI containers before Tailscale has `100.107.121.5` and then never bind `1234`, `8301`, or `8390`. A healthy container and `docker restart` do not mean Amarillo can reach her. The repair is `ops/dominus-nobara/scripts/repair-tailscale-publish.sh`. It must keep refusing to run while `/run/user/1000/dominus-gpu/game-active` exists, and it must not recreate `llama-router` or `comfyui`.

On 2026-10-14 the owner explicitly authorized paid Anthropic API use for
Klukai: Sonnet 5.5 with medium adaptive thinking is the interactive primary,
and Dominus is secondary. This supersedes earlier local-only instructions for
this path only. Dominus gaming/idle guards remain.
See `docs/superpowers/specs/2026-10-14-local-voice-sonnet.md`.

Later that day, the owner requested use of the existing Claude API key for the
dream/scene work. Anthropic creative text is now separately enabled with an
initial limit of 12 attempts per UTC day and 12 per UTC month, shared across
dreams and Her POV writing. This is an attempt limit, not a dollar cap. Images
remain local ComfyUI with cloud image allowances at zero; this instruction does
not authorize paid image APIs. Other background writers remain local. See
`docs/handoff/2026-10-14-dream-image-preparation.md` for activation evidence.

The owner subsequently explicitly overrode the no-spending restriction for
Klukai's existing Brave MCP API usage. Paid Brave search requests are authorized
for this path; `KLUKAI_BRAVE_ENABLED=true` is the production setting. This is
separate from image generation: cloud image generation allowances remain zero.
See `docs/handoff/2026-10-14-web-tools.md` for activation and live-search evidence.

The owner then requested main speech output on Dominus-Nobara's GPU. Production
uses `VOICE_BACKEND=dominus`, `VOICE_URL=http://100.107.121.5:8301`, and
`VOICE_ASYNC_JOBS=true`. XTTS WAV output follows its 24 kHz decoder rate. Keep
the original pinned Dominus reference, exclusive GPU lease and gaming/cleanup
guards. The Arc release remains available as an explicit rollback choice.
The owner explicitly permits running without voice when Nobara is offline:
keep text chat available, with no automatic Arc or paid voice fallback.
See `docs/handoff/2026-10-14-nobara-primary-voice.md` for measurements and rollout.

## Voice source baseline

Read `docker/voice/reference/README.md` for the owner's selected Klukai voice
baseline. The complete source video is
`docker/voice/reference/baseline/3tJ1ZE11J34.mp4`; its extracted mono PCM audio is
`docker/voice/reference/baseline/3tJ1ZE11J34.wav`. Source attribution, media
properties, and digests are in `docker/voice/reference/baseline/source.json`.
Verify the files with
`cd docker/voice/reference/baseline && sha256sum -c SHA256SUMS`.

This is the source recording for listening, comparison, and curating short
conditioning clips. Dominus XTTS retains its separately pinned
`klukai_reference.wav` artifact. The Amarillo Arc release uses its own pinned
excerpt and adoption record under `ops/amarillo/voice_xpu`; do not change the
Dominus lock to deploy that release.

## Gemini artwork review

The owner authorized advisory artwork review through the existing Agy Google
account, plus a one-off generation capability test. Use
`scripts/gemini_art_review.py` and read
`docs/handoff/2026-10-14-agy-artwork.md` for the isolated account runtime and
pilot evidence. Preview is the default; cloud review requires explicit selected
images. Never route this through the retired Gemini CLI MCP. Paid-credit
fallback stays disabled. Production images remain local ComfyUI with zero
cloud allowances; the successful one-off generation test does not enable an
automatic cloud provider. Gemini has durable bus membership, with no unattended
model worker.

## Main image renderer (owner update, 2026-10-14)

The owner explicitly authorized Gemini/Agy in chat and then made it the main
image provider with no GPU image usage. Production selects
`KLUKAI_IMAGE_PROVIDER=agy`, through the private Amarillo account worker. This
supersedes earlier local-image-only and one-off-generation restrictions for
Klukai images. No paid image APIs, credits or fallback are authorized. On Agy
failure, only the later authorized account-provider failover below is allowed; never fall back to ComfyUI. Voice remains unchanged.
Read `docs/handoff/2026-10-14-agy-main-images.md` for deployment, reference
privacy, account/session limits and rollback.

The owner then requested Agy/Grok alternation and mutual failover, confirmed
Grok extra usage is off / hard cap $0, and requested Commander couple samples
from both. The private worker supports `KLUKAI_ACCOUNT_IMAGE_PROVIDERS=agy,grok`
with durable alternating preference and at most one service-error failover.
Never fail over policy refusals or enable API keys, paid credits, or GPU images.
Keep Grok's account paid usage disabled. Its official native client uses a
read-only auth-file mount; account refresh remains outside the image worker.

## Ordered GPU image backups (owner update, 2026-10-15)

The owner subsequently requested Dominus GPU as the third image backend and
Amarillo GPU as the final fallback, and reaffirmed that Agy and Grok remain
mainline. The target order is the existing alternating Agy/Grok pair, then
Dominus ComfyUI, then Amarillo Arc. This supersedes the earlier categorical
prohibition on GPU images for these ordered backups. The owner also authorized
obtaining necessary free models/runtime and requested Klukai samples from both
hosts with two agents assessing Amarillo.

This is authorized work in progress, not evidence of a deployed fallback.
Production still uses the account pair. Preserve policy-refusal precedence,
durable request identity, and observational recovery of uncertain attempts;
do not interpret a generic failure as permission to start another renderer.
Dominus gaming, exclusive lease, bounded residency and cleanup guards remain.
No paid image APIs, credits, new charges or provider upgrades are authorized.
Image backup authorization does not change text or voice provider selection.
