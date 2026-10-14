# Agy is the main Klukai image renderer

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
The client has no retries. CLI timeout is 170 seconds; core deadline is 200 seconds.
Native output dimensions can differ from the requested orientation dimensions.

## Amarillo setup

- Unit: `ops/amarillo/systemd/klukai-agy-images.service`, installed under the
  owner's `~/.config/systemd/user/`.
- Unit environment: `~/.config/klukai/agy-images.env`, containing only the user
  allowlist. The unit uses the owner's desktop keyring session.
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

Offline release checks: full core suite 4,947 passed, 57 skipped, one deselected, 96.74% coverage; required threshold 95%. Amarillo suite: 100 passed. Core lint and diff checks passed. Independent worker/runtime/core reviews found no remaining material issues. Native retained generation stream and artifact revalidated successfully. Live deployment evidence follows after rollout.

Manual wardrobe/regeneration scripts without a user identity fail closed under Agy; bulk historical archive regeneration remains separately scoped. The owner also requested Grok alternation/failover if possible. Native Grok image tools exist; activation requires verified included-quota-only enforcement and is pending that check.
