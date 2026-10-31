# xAI chat and picture environment: recovery plan

## Data preserved

Configuration only. Core gains an optional env file for `KLUKAI_XAI_API_KEY`,
two settings (`KLUKAI_CHAT_BACKEND`, `KLUKAI_API_DAILY_USD_CAP`), and a bind
mount of `~/.local/share/klukai/xai-ledger` at `/var/klukai/xai`. No database
migration runs. Chat messages, episodes, memories, affection, and users are
untouched. The new ledger is a spend log outside Postgres. Keep the `memory`
and `companion_*` counts from before the release; they must not decrease.

## Rollback

Check out the previous `docker-compose.yml` in the production checkout and
recreate core without rebuilding:

`python3 ops/amarillo/scripts/klukai_compose.py up -d --no-build --no-deps companion-core`

The previous image starts with this compose file (it ignores the new
variables). Leave `~/.local/share/klukai/xai-ledger` in place; it holds no
chat text. Restart `klukai-agy-images.service` from the previous unit if the
unit file was updated in the same release.

## Verification

`docker inspect companion-core` shows `KLUKAI_CHAT_BACKEND` and the ledger
mount, and does not show the key value in the image history. `/health` and
`/ready` are ok. A chat turn still answers when the key file is absent
(CLI fallback) and when it is present (API). `memory` counts and a known
fact recall match the pre-release values. Run `scripts/e2e_live.py` as
`claude`.
