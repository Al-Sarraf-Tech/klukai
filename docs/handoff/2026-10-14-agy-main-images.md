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
  (`agy,grok` for alternating preferred providers). The unit uses the owner's desktop keyring session.
- Private host root: `~/.local/share/klukai/agy-bridge/` (0700).
- `runtime/token` (0600) and `runtime/image.sock` (0600), mounted read-only into
  core at `/run/klukai-agy`. The token is unrelated to Google credentials.
- `references.json`: fixed `klukai` and `commander` entries with absolute paths
  and SHA256 digests. Private photos and generated images are not committed.
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
