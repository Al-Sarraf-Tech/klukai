# Klukai detached from the aichat stack (aichat decoupling, step 2): recovery plan

## Data preserved

Configuration only. Core and the events bridge leave the `aichat_default`
network and lose the unused `DATA_URL`; the tailnet gateway stops proxying the
aichat inference service (no client used it) and leaves that network. No
database row, volume or Redis key is touched. Factual memory has been read
from Klukai's own `memory` rows since step 1, and Redis is `companion-redis`.
Keep the `memory` and `companion_*` counts from before the release; they must
not decrease.

## Rollback

The previous release (step 1) needs nothing from the aichat stack except
optionally `DATA_URL`, which its code no longer reads, so the previous image
starts with this compose file. Automatic rollback re-pins the previous image.
If the previous compose file itself is wanted (the aichat containers must
then still exist), check it out in the production checkout and run
`python3 ops/amarillo/scripts/klukai_compose.py up -d --no-build --no-deps companion-core`
and the same for `events-bridge`. For the gateway, restore
`gateway/docker-compose.yml` and `gateway/nginx.conf` from the previous
commit and run `docker compose up -d` in `gateway/`.

## Verification

`docker inspect companion-core companion-events-bridge` lists no
`aichat_default` network and no aichat URL; `/health` and `/ready` are ok;
the bridge subscribes to `companion:events` on companion-redis; the gateway
answers on 100.111.198.19:15432 only. `memory` counts and a known fact recall
match the pre-release values. Run `scripts/e2e_live.py` as `claude`.
`tests/test_aichat_independence.py` guards the files.
