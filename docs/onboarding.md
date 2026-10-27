> **Current architecture (with diagrams):** see [architecture.md](architecture.md) and the root [README](../README.md).

# Onboarding — running klukai from a fresh machine

> Goal: a Linux machine with git + Docker can reach a running `klukai` core and
> complete an authenticated chat turn. Drill results live in
> [`onboarding-test-result.json`](onboarding-test-result.json); any drift is a bug.

## Prerequisites

- Fedora 44 or another modern Linux (macOS hosts are not supported).
- Docker Engine with Compose v2.
- Python 3.14 for tooling outside the container (the app runs in Docker).
- `git`, `make`, `curl`, `jq`.
- **The shared database.** Core does not ship its own PostgreSQL: it joins the
  external Docker networks `aichat_default`, `infra` and `klukai-obs` and
  expects `infra-postgres` on `infra`. Redis is Klukai's own (`companion-redis`,
  started with core). `aichat_default` is still joined for one release so a
  rollback can reach the previous image's services; create it empty if the
  aichat stack is not running.
- All of Klukai's AI is cloud (ADR-0020): chat needs `KLUKAI_CLAUDE_PRIMARY=true`
  and `ANTHROPIC_API_KEY`, voice needs `FISH_API_KEY`. Without them core still
  starts and answers health/auth requests.

## 1. Clone the repo

```bash
git clone git@github.com:Al-Sarraf-Tech/klukai.git ~/git/klukai
cd ~/git/klukai
```

## 2. Set up the local environment

```bash
test -e .env || cp .env.example .env   # never overwrite an existing .env
chmod 600 .env
# Required (compose refuses to render without them): POSTGRES_PASSWORD,
# KLUKAI_REDIS_PASSWORD, RABBITMQ_PASSWORD. Add the SEED_PASSWORD_* values for
# the accounts you want created.
docker compose config --quiet           # validates interpolation, prints nothing
```

`.env` is never committed. Production keeps its exact runtime values in a
private Compose override outside Git (see the README's operator section).

## 3. Bring up core

```bash
docker network ls | grep -E 'aichat_default|infra|klukai-obs'   # must exist
docker compose up -d companion-core     # starts companion-redis first; migrations apply at startup
docker compose -f docker-compose.obs.yml up -d   # observability (optional)
docker compose ps                        # companion-core "Up (healthy)"
curl -s http://127.0.0.1:8300/health | jq .
```

Expected shape:

```json
{"status": "ok", "service": "companion-core",
 "database": {"status": "ok"}, "redis": "ok"}
```

## 4. Verify the auth path

```bash
TOKEN=$(curl -s -X POST http://127.0.0.1:8300/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"jalsarraf","password":"'"${SEED_PASSWORD_JALSARRAF}"'"}' | jq -r .token)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8300/api/session/info | jq .
```

Expected: `{"user_id": "jalsarraf", "created_at": "…", "expires_at": "…"}`.

The token is an opaque random bearer value (stored hashed server-side), not a
JWT. Three failed logins from one IP within an hour trigger a temporary ban.

## 5. Send a chat message

Chat is a WebSocket conversation, not a REST call. The supported check is the
live end-to-end suite, which mints a short-lived session for the dedicated
`claude` test user (it refuses `jalsarraf` unless overridden):

```bash
python3 scripts/e2e_live.py --only chat
```

Without the Anthropic settings (or if Anthropic is down) she answers with an
in-character disruption line; health and auth keep working.

## 6. Run the tests

The CI environment is hash-locked; use the same lock locally:

```bash
python3.14 -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r docker/core/requirements-ci.txt
python -m ruff check docker/core/app --config docker/core/ruff.toml
python -m mypy docker/core/app --config-file docker/core/mypy.ini
pytest tests/quality tests/release -q -o pythonpath=. \
  -o asyncio_default_fixture_loop_scope=function \
  --deselect tests/release/test_image_runtime.py   # that file needs KLUKAI_TEST_IMAGE
```

The suites outside `docker/core` run from the repo root:

```bash
make test-tgbot test-ops test-voice test-bridge test-js test-shell
make test-all                          # everything above plus the core gate
```

`make test-shell` needs shellcheck; `make test-js` needs Node 20+ and, for the
avatar tests, `npm ci --prefix tools/avatar --ignore-scripts`.

The full core suite (5,400+ tests) runs in CI against a disposable PostgreSQL
through `scripts/ci/core.sh`; the paired performance gate is
`python -m tools.ci.perf_stack` (see [tools/ci/README.md](../tools/ci/README.md)).
CI is dispatched on a branch head with
`gh workflow run ci.yml --ref <branch> -f expected_revision=<sha>`.

## 7. Run the audit harness

```bash
scripts/s-tier-audit.sh                  # human report
scripts/s-tier-audit.sh --json | jq .    # machine-readable
scripts/s-tier-audit.sh --only testing   # one dimension
```

The audit only credits measured evidence: missing or stale evidence never earns
a tier, so a local run without CI evidence directories reports most rows as
unverified. The honest per-row status lives in the latest handoff document.

## Common gotchas

| Symptom | Fix |
|---|---|
| `docker compose` refuses to render | A required `.env` value is missing (`POSTGRES_PASSWORD`, `KLUKAI_REDIS_PASSWORD`, `RABBITMQ_PASSWORD`) |
| `network aichat_default not found` | `docker network create aichat_default` (Prerequisites) |
| `companion-core` exits immediately | `docker compose logs companion-core` — usually a missing env var or an unreachable `infra-postgres` |
| `companion-core` restarts with `PermissionError` under `/app/app` | The checkout was made with a private umask (077). The image copies source files with their modes and runs as UID 1000; clone with umask 022 or `chmod -R a+rX` the checkout, then rebuild |
| Voice answers 503 | `KLUKAI_VOICE_ENABLED` is off or `FISH_API_KEY` is empty; speech-to-text also needs Fish credit |
| Flutter app login page is blank | Service worker intercepted; rebuild with `--base-href=/app/` (`scripts/deploy-web.sh` does) |
| Images fail to load on web | `Image.network(headers:)` is ignored on web; use `http.get` + `Image.memory` |
| Coverage gate fails | Read the per-module report from the CI core job; the floor is 95% statements and branches |

## Drill

The nightly `onboarding` job runs §2-§4 from a clean clone of the commit against
throwaway infrastructure (`tools/ci/onboarding_drill.py`); every command it
runs must still appear verbatim above, and its result lists what it did not
exercise. Someone other than the author should still run §1-§5 on a fresh VM, note any drift,
fix this doc in the next PR, and append a dated row to
[`onboarding-test-result.json`](onboarding-test-result.json) with the exact
steps actually executed, their results and what was not exercised.

## See also

- [architecture.md](architecture.md) — what the boxes and arrows mean.
- [runbooks/](runbooks/) — what to do when an alert fires.
- [slos.md](slos.md) — what "working" means quantitatively.
- [handoff/2026-10-16-s-tier-finish.md](handoff/2026-10-16-s-tier-finish.md) — current release, CI evidence and the honest S ledger.
