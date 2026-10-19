# Account-backed Gemini and Grok images

The owner first authorized account-backed images in chat, then explicitly asked:
“And make it the main version of image generation no more gpu usage.” This
supersedes the earlier one-off-generation/local-image-only scope. Images now
use the existing signed-in Google account through pinned Antigravity CLI 1.3.5.
It does **not** authorize new charges: `useG1Credits=false`, no API/ADC provider,
no paid fallback, and paid cloud-image allowances stay at zero.

## Operation

Production selects `KLUKAI_IMAGE_PROVIDER=agy`. All image generation paths
must dispatch before GPU readiness/lease acquisition. Unsupported image edits
fail closed while Agy is selected. Unavailable authentication, exhausted included
quota, busy worker or generation errors yield no image and never invoke ComfyUI.
Voice and text backend selections are unchanged.

The core calls an authenticated HTTP endpoint over a private Unix socket on
Amarillo. `KLUKAI_AGY_ALLOWED_USERS` is enforced in core and independently by the
worker. The owner's Commander reference is available only to `jalsarraf` couple
requests. User IDs must be passed through all image entry points. Background
prompts contain the selected scene, not the complete chat history.

The host worker uses a fresh filesystem profile, filtered Secret Service D-Bus
proxy, fixed hash-checked references, read-only tool policy, and one
`generate_image` call. It stages only selected references, strips image metadata,
validates the native completion stream and the generated file, and returns PNG.
No Google credentials are mounted into Docker. The official CLI and its keyring
access remain trusted; the proxy filters services, not individual keyring items.

Request IDs are claimed durably in SQLite before generation. A completed replay
returns cached bytes; an interrupted, failed or conflicting replay never starts
another generation. Only one request runs at once; concurrent requests get 429.
The client has no retries. CLI timeout is 170 seconds; core deadline is 380 seconds for at most two bounded providers.
Native output dimensions can differ from the requested orientation dimensions.

## Amarillo setup

- Unit: `ops/amarillo/systemd/klukai-agy-images.service`, installed under the
  owner's `~/.config/systemd/user/`.
- Unit environment: `~/.config/klukai/agy-images.env`, containing the user allowlist and `KLUKAI_ACCOUNT_IMAGE_PROVIDERS`
  (`grok` since 2026-10-17; `agy,grok` alternates the preferred provider). The unit uses the owner's desktop keyring session.
- Private host root: `~/.local/share/klukai/agy-bridge/` (0700).
- `runtime/token` (0600) and `runtime/image.sock` (0600), mounted read-only into
  core at `/run/klukai-agy`. The token is unrelated to Google credentials.
- `references.json`: fixed `klukai` and `commander` entries with absolute paths
  and SHA256 digests. Private runtime files and generated images are not committed; the existing repository Commander reference is unchanged.
- `state/`: durable request ledger and completed image cache. Preserve this on
  upgrades; deleting claims removes replay protection.
- Repository `.env`: `KLUKAI_IMAGE_PROVIDER=agy`, `KLUKAI_AGY_ALLOWED_USERS`,
  `KLUKAI_AGY_RUNTIME_DIR`. Other deployments default to their existing local
  provider and need their own account setup before selecting Agy.

Only rebuild/recreate `companion-core` on Amarillo. Do not use the all-stack
Make target, recreate Dominus containers or change GPU gaming/idle guards.
`GET /health` on the socket checks worker configuration/liveness only; it does
not prove a fresh Google login or available quota. A successful real generation
is the end-to-end proof.

## Grok renders, Claude checks (owner decision 2026-10-17)

The owner chose Grok for Klukai's pictures. Since 2026-10-18 the order is fixed
(`KLUKAI_IMAGE_ROTATION=fixed`, `KLUKAI_ACCOUNT_IMAGE_PROVIDERS=grok,codex,agy`):
Grok first, Codex second (`scripts/codex_image_render.py`, the owner's Codex CLI
login and its built-in image generation, 150 s cap, the render taken from
`~/.codex/generated_images/<session>/` and removed), Agy absolutely last. A later
provider runs only when the earlier one fails with a service error; core's 380 s
deadline means Agy is reached only when time remains.
Agy/Gemini had completed none of its six attempts, and with alternation half of
all requests waited on it first.

Every render is checked by Claude Sonnet 5.5 at effort `high`
(`scripts/claude_image_check.py`, `--checker claude`,
`KLUKAI_IMAGE_CHECKER=claude`). Claude never generates art; that is an owner
rule.

- **How it runs:** Claude Code print mode under the owner's subscription login,
  so a check never bills the API. It has no tools, no MCP and no session
  persistence, and runs in an empty working directory. The environment carries
  no keys.
- **What it receives:** the Klukai reference, the Commander reference when the
  Commander is in the scene, the candidate (downscaled to 1024 px) and the
  request text.
- **When it rejects:** only when it both says `reject` and names a major
  defect: wrong identity or underage look, broken anatomy, an unexpected person
  or a Commander who is not an adult man, a scene that contradicts the request,
  or text, watermarks or collage.
- **After a reject:** one re-render with the same provider, with the major
  issues appended as things to avoid. It only happens while another 170 s render
  plus a 90 s check still fit the 360 s worker budget, inside core's 380 s.
  A second reject fails the request as `check_rejected`, so core gets no image.
- **Fail-open:** any checker failure (timeout, refusal, quota, bad output) is
  `unchecked`, and the image is delivered.
- **Records:** every verdict lands in `state/requests.sqlite3` table `checks`
  (verdict, bounded issue list, seconds). `GET /health` reports
  `"checker": "claude-sonnet-5-5"`.

Measured live on 2026-10-17: the canon portrait passed in 3.9 s, and a
wrong-person candidate was rejected in 6.5 s with the identity and request named.

## Rollback

Set `KLUKAI_IMAGE_PROVIDER=disabled` and recreate only `companion-core` to stop
image generation while preserving the owner's no-GPU choice. Disable any
chat-only Agy override too. Stop the worker if needed. Restoring ComfyUI requires
an explicit owner decision because it restores GPU image usage. Preserve the
ledger and cache, token and private reference manifest. A prior core image is
retained for code rollback, but its provider must also be disabled to avoid
restoring GPU image generation accidentally.

## Verification

Offline release checks: full core suite 4,947 passed, 57 skipped, one deselected, 96.74% coverage; required threshold 95%. Amarillo suite: 100 passed. Core lint and diff checks passed. Independent worker/runtime/core reviews found no remaining material issues. Native retained generation stream and artifact revalidated successfully. The rotation extension additionally passed 197 ops tests and 4 subtests, 149 targeted core image tests and core lint. The independent follow-up review reproduced and verified the refusal-precedence and string-error fixes; no material findings remain. Live deployment evidence follows below.

Manual wardrobe/regeneration scripts without a user identity fail closed under Agy; bulk historical archive regeneration remains separately scoped. The rotation extension below records the owner’s later Grok authorization.

## Agy/Grok rotation update

The owner requested alternation and mutual failover, then explicitly confirmed
Grok extra usage is off / hard paid-usage cap $0. Native Grok Build 1.0.52 has
account-backed image generation/editing; the native dashboard verified the
signed-in SuperGrok Lite account. Keep that account's paid usage disabled.
No developer API key, API credits, topup, purchase or subscription change is
authorized. Grok credentials are mounted read-only into an isolated fresh
profile for the official client. Refresh requiring a write fails closed; use
the owner's normal Grok login to refresh outside this worker.

2026-10-18: the pin moved to Grok Build 1.0.62. The twice-daily CLI updater keeps
only the newest two builds in `~/.grok/downloads` and had deleted 1.0.52, so every
Grok attempt failed as unavailable. The renderer now prefers its own copy at
`~/.local/share/klukai/agy-bridge/grok/grok-<VERSION>-linux-x86_64`, which the
updater never touches; install it with
`install -m 500 ~/.grok/downloads/grok-1.0.62-linux-x86_64 ~/.local/share/klukai/agy-bridge/grok/`.
`grok models` printing "You are not authenticated" means the owner must run
`grok login` again; the worker never logs in or refreshes by itself.

`KLUKAI_ACCOUNT_IMAGE_PROVIDERS=agy,grok` in the private service environment
enables deterministic alternating preferred providers. The durable counter
advances only for an accepted new request. At most one attempt per provider
is permitted. Only recognized service/quota/auth failures permit the other
provider; policy refusals, invalid input, unverified output and ambiguous
process timeout stop the request. There is no retry loop or GPU fallback.
Both provider attempts are recorded without prompts in the private ledger;
completed request replays return the same cached bytes.

The core selector retains `KLUKAI_IMAGE_PROVIDER=agy` for the compatible host
bridge protocol; the worker health document lists the actual enabled providers.
The host worker, rather than the core container, owns provider rotation.

## Commander appearance

The owner requested a slightly lighter skin tone as part of the Commander character. The canonical personality description and image tags now use light olive skin; both native provider prompts apply that description while preserving his recognizable facial features. The private reference photo remains the identity source. The owner further requested an anime Commander matching Klukai. His canonical description, image tags and the shared provider prompt now specify matching anime facial proportions, line art and cel shading; the photo supplies identity rather than a photographic rendering style.


## Production verification, 2026-10-14

The account worker is enabled on Amarillo with both `agy,grok` providers and
persistent alternating preference. The core is healthy and selects the account
bridge; its chat-only override is empty. An authenticated test-user request to
`/api/generate-image` returned a validated PNG and an archive memory ID in
33.51 seconds. An unauthenticated request was rejected with 401. The private
ledger records the preferred Agy attempt failing as `unavailable`, followed by
one successful Grok attempt, proving real service failover without a GPU call.
The temporary test login was revoked. The test-user image allowance is removed
before handoff; production is restricted to `jalsarraf`.

Google's native `generate_image` returned HTTP 500 INTERNAL on the fresh couple
and smoke attempts. Earlier Gemini couple generation succeeded and remains a
valid provider sample; this does not establish current Gemini availability.
Grok's native OAuth reference edit completed successfully; its validated sample
and event evidence remain private. One intermediate Grok session validation
failure was rejected; a retained diagnostic run subsequently passed without
weakening validation. Neither provider is assumed always available.

The original pre-change core image and environment snapshot are retained
privately for rollback. Voice remains `dominus`; no Dominus containers, GPU
image services or gaming guards were changed. Paid image allowances remain
zero, Agy credits remain disabled, and Grok uses the owner-confirmed disabled
extra-usage / $0-cap account.


Final appearance deployment: source `7121621`, core image
`sha256:d335ea82c1ca13b69f68ee8feb65a0939069e95708010d34fadb632bb488cd21`.
The running container is healthy, loads the anime/light-olive Commander
configuration, keeps `VOICE_BACKEND=dominus`, and allows only `jalsarraf`.
Worker health lists both providers; the removed test user receives 403 without
advancing rotation. The next accepted request prefers Grok, following the
successful Agy-to-Grok failover. Rotation persists across service restarts.

Final native Grok anime couple sample: 1248x832 PNG, 946715 bytes,
SHA256 `b2ae6bd2a0c545e879f0f357690ff5ec23c331317c3096929690175997741999`.
It passed native OAuth/session/gate/artifact validation and visual inspection:
both characters use anime line art and shading; the Commander retains short
dark hair, strong build and the requested lighter light-olive complexion.
A separate attempt to update the earlier Gemini winter couple image with the
same appearance, using a 4:3 scene reference, again returned HTTP 500 INTERNAL.
That leaves the earlier successful Gemini image as a clearly labeled historical
sample, not proof of the newest appearance or present provider availability.
The failure evidence shows no explicit quota or policy refusal; cause is unknown.
No additional automatic retry was made.

Final prompt-only refinement checks: 132 worker/runtime/Grok tests, 96 core
image tests and core lint passed. The complete earlier core suite, final 197
ops tests plus four subtests, independent review, and live failover evidence
above establish the release checks. Generated samples and detailed native
streams remain in private host storage.
