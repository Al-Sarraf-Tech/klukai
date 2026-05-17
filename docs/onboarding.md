# Onboarding — running klukai from a fresh machine

> Goal: a fresh laptop with git + Docker installed can reach a running `klukai` stack and send a chat message in under 30 minutes. This doc is **tested quarterly**; the most recent test result lives in `docs/onboarding-test-result.json`. Any drift is a bug — open an issue with the `runbook-incident` template.

## Prerequisites

- Fedora 43 (or any modern Linux). macOS host is not supported (global CLAUDE.md absolute).
- Docker Engine + Docker Compose v2.
- Python 3.13 (only required for tooling outside the container — the app itself runs inside Docker).
- `git`, `make`, `curl`, `jq`.
- Tailscale account + amarillo node enrolled (only if you want to reach dominus).

## 1. Clone the repo

```bash
git clone git@github.com:Al-Sarraf-Tech/klukai.git ~/git/klukai
cd ~/git/klukai
```

## 2. Set up the local environment

```bash
cp .env.example .env
# Fill in:
#   POSTGRES_PASSWORD=<dev password>
#   SEED_PASSWORD_JALSARRAF=<dev password>
#   ADMIN_TOKEN=<short random string>
# Optional (for cloud fallback / image gen):
#   ANTHROPIC_API_KEY=...
#   VAPID_PUBLIC_KEY=...
#   VAPID_PRIVATE_KEY=...
chmod 600 .env
```

The `.env` file is **never** committed. Production reads secrets via `systemd-creds` (`/etc/credstore.encrypted/klukai-secrets.cred`); dev uses `.env`.

## 3. Bring up the stack

```bash
docker compose up -d                        # core + gateway
docker compose -f docker-compose.obs.yml up -d   # observability (optional for dev)
```

Wait for healthchecks:

```bash
docker compose ps     # all "Up (healthy)"
curl -s http://localhost:8300/health | jq .
```

You should see:

```json
{
  "status": "ok",
  "service": "companion-core",
  "database": {"status": "ok"},
  "redis": "ok",
  "qdrant": "ok"
}
```

## 4. Verify the auth path

```bash
# Acquire a session token using your seed password
curl -X POST http://localhost:8300/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"user_id":"jalsarraf","password":"'"${SEED_PASSWORD_JALSARRAF}"'"}'
```

Returned `token` is a JWT. Use it for `/api/*` calls:

```bash
TOKEN=<paste token>
curl -H "Authorization: Bearer $TOKEN" http://localhost:8300/api/character/info
```

## 5. Send a chat message

```bash
curl -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -X POST http://localhost:8300/api/chat/turn \
     -d '{"message":"Hello, Klukai."}'
```

If LM Studio (on dominus) isn't reachable, you'll get a 503 with a clear error and the circuit breaker for `lm_studio` will be open. That's expected for an isolated dev box.

## 6. Run the tests

```bash
cd docker/core
pip install -r requirements.txt -r requirements-dev.txt
pytest -q                              # unit (the default)
pytest -m integration                  # integration (needs testcontainers / docker)
pytest tests/golden/                   # golden / character regression
pytest tests/property/                 # hypothesis-driven property tests
pytest tests/perf/ -m perf             # perf gate (needs running stack)
```

## 7. Run the audit harness

```bash
scripts/s-tier-audit.sh                # human report
scripts/s-tier-audit.sh --json | jq .  # CI-friendly
scripts/s-tier-audit.sh --only=testing # focus one dimension
```

A green run is the definition of S+ tier. Anything red is the floor.

## Common gotchas

| Symptom | Fix |
|---|---|
| `companion-core` immediately exits | Check `docker compose logs core` — usually a missing env var |
| `voice` service has no port 8301 | dominus port-binding bug; `docker rm -f klukai-voice && docker compose up -d voice` (ref `feedback_dominus_voice_port.md`) |
| Flutter app login page is blank | SW intercepted; rebuild with `--base-href=/app/` (ref `feedback_flutter_base_href.md`) |
| Image upload returns 401 on web | `Image.network(headers:)` ignored on web — use `http.get` + `Image.memory` (ref `feedback_flutter_web_image_auth.md`) |
| Tests can't import `psycopg` | dev shim in `conftest.py` mocks it — make sure `tests/__init__.py` is loaded |
| Coverage gate fails | `cd docker/core && pytest --cov=app -q` to see per-module gaps |

## Quarterly drill

The point of this doc is **someone other than the author** must be able to run klukai from these instructions. Once per quarter:

1. Spin a fresh VM (or wipe a checkout).
2. Walk through §1-§5 verbatim.
3. Note any drift (commands that fail, missing context, broken links).
4. Update this doc.
5. Append a result row to `docs/onboarding-test-result.json`:

   ```json
   {
     "drill_date": "2026-08-15",
     "operator": "<name>",
     "duration_minutes": 22,
     "drift": [],
     "outcome": "pass"
   }
   ```

If drift is non-empty, the next PR fixes it. S+ certification (`scripts/s-tier-audit.sh`) requires this file to be <90 days old.

## See also

- `docs/architecture.md` — what the boxes-and-arrows actually mean.
- `docs/runbooks/` — what to do when an alert fires.
- `docs/slos.md` — what "working" means quantitatively.
- `docs/superpowers/specs/2026-05-16-s-plus-uplift.md` — the long-term roadmap.
