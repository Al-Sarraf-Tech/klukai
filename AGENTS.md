# Klukai

**Cloud AI only (owner decision, 2026-10-26).** Klukai runs no inference on
Dominus-Nobara or Amarillo. Voice in and out is Fish Audio. There is no local
fallback of any kind. Design: `docs/superpowers/specs/2026-10-26-cloud-only-ai-design.md`.

**xAI API for chat and pictures (owner decision, 2026-10-31).** Chat replies
and picture generation use the prepaid key in `KLUKAI_XAI_API_KEY`, loaded from
`~/.config/klukai/xai.env` at runtime. The key is never committed, logged, or
read by application code from that file. Chat is `grok-4.7` at reasoning
effort low, streamed from `https://api.x.ai/v1/chat/completions`. Pictures are
`grok-imagine-image-2.0` edits (`POST /v1/images/edits`) with her approved
reference, aspect `2:3`, resolution `2k`, quality `medium`, and `b64_json`.
Do not use the retired grok-imagine quality or pro image model ids. Every
edit sends `docs/images/klukai-portrait.png` as `images[].url`. The picture
checker is grok-4.7 vision on the API at low effort, with the CLI checker if
that call fails. Background memory (recall, extraction, promises, mood) stays
on the CLI with `disable_api_key_auth=true`. `KLUKAI_CHAT_BACKEND` and
`KLUKAI_IMAGE_BACKEND` are `api` or `cli`. The default is `api` when the key
is present. A missing key, the daily USD cap (`KLUKAI_API_DAILY_USD_CAP`,
default 5; chat uses `usage.cost_in_usd_ticks` / 10^10, each 2k edit is about
$0.09, a vision check about $0.007), or an API failure before any chat token
falls back to the CLI. Fal stays off. Chat sends a stable `x-grok-conv-id`.
Fish voice is unchanged.

**Grok account login (owner decision, 2026-10-30, still the CLI fallback and
the background path).** Background text, the picture checker, and the chat
and picture fallbacks run through the official Grok CLI on the owner's account
login. `disable_api_key_auth = true` on that path. The Claude image checker
and the Sonnet/Haiku chat path stay in the tree and are not selected. See the
section below.
Do not reintroduce LM Studio, Dominus voice, ComfyUI, the GPU lease, local
embeddings or Qdrant into `docker/core`; `tests/test_cloud_only_invariant.py`
fails if any of them comes back. `ops/dominus-nobara` is the retired Dominus
stack, kept only until it is decommissioned separately.

## History (superseded where it mentions Dominus or local models)

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

## Klukai's models: Grok account login (owner decision, 2026-10-30)

Jamal: "Switch klukai to Grok through my account login, no API costs." Then:
"Also for her chat put 4.7 at medium too." Then: "All her stuff must run
through grok." This supersedes the 2026-10-23 choice of fal/Seedream as the
picture provider and the 2026-10-14/2026-10-26 choice of Claude Sonnet for
chat and Claude Haiku for background text.

The pinned CLI is Grok Build 1.0.62. `grok --help` on that binary accepts
`-m/--model` and `--reasoning-effort` (alias `--effort`). The model id is
`grok-4.7`. On 2026-10-31 the owner set chat and background text to `low`
(the fastest effort) and picture renders plus the checker to `xhigh`.
Those are the defaults of `KLUKAI_GROK_CHAT_EFFORT`,
`KLUKAI_GROK_BACKGROUND_EFFORT`, and `KLUKAI_GROK_IMAGE_EFFORT`. The session
must report OAuth
(`apiKeySource=oauth`). `disable_api_key_auth = true` stays in the clean
Grok home. No `XAI_API_KEY` and no call to `api.x.ai` is added here.

Pictures: `ops/amarillo/systemd/klukai-agy-images.service` sets
`KLUKAI_ACCOUNT_IMAGE_PROVIDERS=grok`, `KLUKAI_IMAGE_CHECKER=grok`,
`GROK_AUTH_PATH=%h/.grok/auth.json`, and
`KLUKAI_GROK_DAILY_LIMIT=10` / `KLUKAI_GROK_MONTHLY_LIMIT=150`. A missing or
invalid cap fails closed. The render uses the same locked 2D-anime brief as
fal, sends only Klukai's approved reference, and describes the Commander in
text. A nudity grant is refused before references, the ledger, or the CLI.
The checker uses the same rejection rubric as the Claude checker, including
photorealistic, 3D, and semi-realistic drift. Core still selects the account
worker with `KLUKAI_IMAGE_PROVIDER=agy`; that name is the bridge, not the
renderer. Chat and background text use `POST /complete` on that same worker
socket. A Grok failure does not fall back to Claude, fal, or any other paid
API.

fal/Seedream stays in the tree as an opt-in sole provider. To turn it back
on, set `KLUKAI_ACCOUNT_IMAGE_PROVIDERS=fal` alone (the worker rejects fal
combined with another provider, so it cannot fail over), set
`KLUKAI_FAL_DAILY_LIMIT` and `KLUKAI_FAL_MONTHLY_LIMIT` to positive integers,
and restart `klukai-agy-images.service`. That spends fal credit.

After deploy, as the user who owns the Grok login:

```
GROK_AUTH_PATH=$HOME/.grok/auth.json \
KLUKAI_GROK_DAILY_LIMIT=10 \
KLUKAI_GROK_MONTHLY_LIMIT=150 \
python3 ops/amarillo/scripts/klukai-grok-smoke.py
```

That spends one image attempt. It renders once, checks the picture, confirms
the ledger grew by one, and runs one chat turn. The chat line must show
`model=grok-4.7` and `effort=medium` with `source=stream` or `source=summary`.
The summary is `SMOKE PASS` or `SMOKE FAIL`. It does not print credentials.

The login at `GROK_AUTH_PATH` has to still be valid (`grok login` on the host
if `grok models` says it is not). The unit environment above has to be the
one the service process actually has; a shell without those variables fails
closed. The PR reviewer (`ops/amarillo/scripts/klukai-pr-reviewer.py`) is
still Claude Opus 5.5 at xhigh effort on the Anthropic API. That is an
operator tool, not her chat, pictures, or background text.

## Klukai's pictures: Seedream 4.5 on fal (owner decisions, 2026-10-23)

Provider choice in this section is superseded by the 2026-10-30 Grok decision
above. The standing rules below still apply. Supersedes the Grok-only and
"no paid image APIs" rules further down for the period they were in force.
The owner funded fal.ai and made Seedream 4.5 (`fal-ai/bytedance/seedream/v4.5/edit`)
Klukai's **only** picture provider (`KLUKAI_ACCOUNT_IMAGE_PROVIDERS=fal`), checked
by Claude as before. `scripts/fal_image_render.py` sends her approved reference
with a locked 2D-anime style prompt, reserves every paid call in a persistent ledger against
`KLUKAI_FAL_DAILY_LIMIT=10` / `KLUKAI_FAL_MONTHLY_LIMIT=150` (missing caps fail
closed), reads `FAL_KEY` from Klukai's encrypted compose env into memory only,
and gets results inline so fal keeps no copy of the image (its request history
does keep each request's prompt and status). The checker rejects renders that
drift to photorealistic, 3D or semi-realistic. For Commander scenes, use his
detailed text description; the active renderer and checker never read or send
his portrait image.

Standing rules (owner, 2026-10-23):
- Nudity need not be tasteful; explicit nudity is allowed when Klukai's own
  intimacy gate grants it. It stays adult (never childlike), Klukai alone,
  never with the Commander reference (a real person's likeness), no sex acts.
  Character intimacy controls are unchanged. The current renderer does not
  support the nudity grant: it refuses that flag before any private or paid
  work and always keeps provider safety checks enabled.
- **On fal, intimacy and nudity are implied only** (owner, 2026-10-24: "make it
  implied nudity ... make sure it doesn't trip the flag"). fal refuses anything
  its classifier reads as sexual (`content_policy_violation`, category `sexual`),
  even fully clothed scenes carrying the bond-level body tags. The worker
  rewrites fal's scene (`scripts/implied_scene.py`): body and arousal tags go,
  undress becomes a quilt, towel or blanket that covers, and a grant becomes
  `NUDITY_IMPLIED` instead of the flag. A refusal is redrawn once, fully clothed
  (`implied_scene.plain`). These renders are checked by Claude as usual.
- **Pictures are made only on demand.** The fal API is paid per image: no
  automatic, proactive, dream, prewarm or seeding pictures.

Final owner clarification (2026-10-23): this is a hard rule, including dreams;
it supersedes any older nightly-picture allowance. Dreams remain text. Existing
environment caps cannot opt automatic pictures back in. The owner reports the
existing fal key was added to Klukai's encrypted store and backed up; that is
owner-reported state, not permission to inspect credentials. Each companion's
key belongs in its own encrypted store. The owner subsequently authorized
deployment of both apps' updates; the coordinating agent owns the deployment.

## Klukai's pictures: Grok renders, Claude only checks

Owner decision 2026-10-17. Production pictures come from Grok through the
account image worker (`KLUKAI_ACCOUNT_IMAGE_PROVIDERS=grok`). Claude Sonnet 5.5
at effort `high` checks every render (`scripts/claude_image_check.py`) through
the owner's subscription login. **Never generate art with Claude**, in any form:
no SVG or code-drawn images, and no Claude image provider or fallback. The owner
judged it atrocious. Details are in `docs/handoff/2026-10-14-agy-main-images.md`.

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
