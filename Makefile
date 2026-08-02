DOMINUS_SSH_HOST ?= dominus-nobara
DOMINUS_TAILSCALE_IPV4 ?= 100.107.121.5
AMARILLO_TAILSCALE_IPV4 ?= 100.111.198.19
DOMINUS_RAID_MOUNT ?= /mnt/nvmer0
DOMINUS_AI_DIR ?= /mnt/nvmer0/services/ai-stack/source/klukai/ops/dominus-nobara
DOMINUS_STACK_ENV ?= /mnt/nvmer0/services/ai-stack/config/stack.env
LM_STUDIO_URL ?= http://$(DOMINUS_TAILSCALE_IPV4):1234
VOICE_URL ?= http://$(DOMINUS_TAILSCALE_IPV4):8301
CURL_HEALTH = curl -sf --connect-timeout 3 --max-time 10
DOMINUS_SSH = ssh -T -o AddressFamily=inet -o BatchMode=yes -o ClearAllForwardings=yes -o ConnectTimeout=10 -o ControlMaster=no -o ControlPath=none -o ForwardAgent=no -o StrictHostKeyChecking=yes

.PHONY: build build-backend build-pwa run stop logs logs-core logs-voice restart rebuild health dominus-preflight gateway gateway-stop gateway-logs deploy perf-baseline test-local test-integration lint-local type-check security-scan

# ── Local (amarillo) commands ────────────────────────────────────────────────

gateway:
	cd gateway && docker compose up -d

gateway-stop:
	cd gateway && docker compose down

gateway-logs:
	cd gateway && docker compose logs -f

# ── Build (on amarillo, the core host) ─────────────────────────

build: build-pwa build-backend

build-backend:
	docker compose build

# Fails when Flutter is missing so `make build` can't silently ship a stale
# web bundle. Opt out explicitly with SKIP_PWA=1 on backend-only hosts.
build-pwa:
	@if [ "$(SKIP_PWA)" = "1" ]; then \
		echo "SKIP_PWA=1 — skipping PWA build (web-build/ may be stale)"; \
	elif command -v flutter >/dev/null 2>&1 && [ -d flutter_app ]; then \
		cd flutter_app && flutter build web --release --base-href=/app/; \
		rm -rf ../web-build/*; \
		cp -r build/web/* ../web-build/; \
		echo "PWA built and copied to web-build/"; \
	else \
		echo "ERROR: flutter not available (or flutter_app/ missing) — refusing to silently skip."; \
		echo "       Use SKIP_PWA=1 to build backend-only on purpose."; \
		exit 1; \
	fi

# Runs the live-stack integration suite against the RUNNING companion-core
# container (requires `make run`). Uses `docker exec` rather than
# `docker compose run` on purpose: a run-container inherits the service's
# /health healthcheck + autoheal label, so autoheal reaps it mid-test (it
# serves pytest, not /health). exec'ing into the already-healthy container
# avoids that. Tests are copied in and pytest installed to a writable target
# (the image venv rejects --user).
# NOTE: a subset currently errors on a pytest-asyncio strict-mode
# async-fixture incompatibility in the older integration tests — tracked as a
# separate harness cleanup; the live read/write smoke covers the new features.
test-integration:
	docker cp docker/core/tests companion-core:/app/tests
	docker exec companion-core sh -c "pip install -q --target=/tmp/pylibs pytest pytest-asyncio && KLUKAI_TEST_ALLOW_LIVE_BACKENDS=1 PYTHONPATH=/tmp/pylibs:/app python3 -m pytest /app/tests/integration -m integration -q"

# ── Core stack (amarillo) — runs companion-core + datastores ─────────────────

run:
	docker compose up -d

stop:
	docker compose down

logs:
	docker compose logs -f

logs-core:
	docker compose logs -f companion-core

dominus-preflight:
	@command -v tailscale >/dev/null
	@tailscale ip -4 | grep -Fxq '$(AMARILLO_TAILSCALE_IPV4)' || { echo 'ERROR: this is not the expected amarillo Tailnet node ($(AMARILLO_TAILSCALE_IPV4))' >&2; exit 1; }
	@resolved="$$(ssh -G -o AddressFamily=inet '$(DOMINUS_SSH_HOST)' 2>/dev/null | awk '$$1 == "hostname" {print $$2; exit}')"; \
		[ "$$resolved" = '$(DOMINUS_TAILSCALE_IPV4)' ] || { echo "ERROR: $(DOMINUS_SSH_HOST) resolves to $${resolved:-nothing}, not $(DOMINUS_TAILSCALE_IPV4)" >&2; exit 1; }
	@printf '%s\n' \
		'set -Eeuo pipefail' \
		'read -r client_ip _ server_ip _ <<<"$${SSH_CONNECTION:-}"' \
		'[[ "$$client_ip" == "$$1" && "$$server_ip" == "$$2" ]]' \
		'mountpoint --quiet -- "$$3"' \
		'[[ "$$(findmnt -n -o TARGET --target "$$3")" == "$$3" ]]' \
		| $(DOMINUS_SSH) '$(DOMINUS_SSH_HOST)' bash -s -- '$(AMARILLO_TAILSCALE_IPV4)' '$(DOMINUS_TAILSCALE_IPV4)' '$(DOMINUS_RAID_MOUNT)'

logs-voice: dominus-preflight
	@printf '%s\n' \
		'set -Eeuo pipefail' \
		'read -r client_ip _ server_ip _ <<<"$${SSH_CONNECTION:-}"' \
		'[[ "$$client_ip" == "$$1" && "$$server_ip" == "$$2" ]]' \
		'mountpoint --quiet -- "$$3"' \
		'[[ "$$(findmnt -n -o TARGET --target "$$3")" == "$$3" ]]' \
		'[[ "$$(realpath -m -- "$$4")" == "$$4" ]]' \
		'[[ "$$(realpath -e -- "$$5")" == "$$5" ]]' \
		'[[ "$$4" == "$$3/"* && "$$5" == "$$3/"* ]]' \
		'[[ -r "$$4/compose.yaml" && -r "$$5" ]]' \
		'cd -- "$$4"' \
		'exec docker compose --env-file "$$5" --file compose.yaml --profile "*" logs -f companion-voice' \
		| $(DOMINUS_SSH) '$(DOMINUS_SSH_HOST)' bash -s -- \
			'$(AMARILLO_TAILSCALE_IPV4)' '$(DOMINUS_TAILSCALE_IPV4)' \
			'$(DOMINUS_RAID_MOUNT)' '$(DOMINUS_AI_DIR)' '$(DOMINUS_STACK_ENV)'

restart:
	docker compose restart

rebuild: stop build run

# ── Health checks ────────────────────────────────────────────────────────────

health:
	@echo "=== companion-core (amarillo) ==="
	@$(CURL_HEALTH) http://localhost:8300/health 2>/dev/null | python3 -m json.tool || echo "UNREACHABLE"
	@echo ""
	@echo "=== LLM compatibility gateway ($(LM_STUDIO_URL), Tailscale only) ==="
	@$(CURL_HEALTH) '$(LM_STUDIO_URL)/health' 2>/dev/null | python3 -m json.tool || echo "UNREACHABLE"
	@echo ""
	@echo "=== companion-voice ($(VOICE_URL), Tailscale) ==="
	@$(CURL_HEALTH) '$(VOICE_URL)/health' 2>/dev/null | python3 -m json.tool || echo "UNREACHABLE"
	@echo ""
	@echo "=== ComfyUI (authenticated gateway facade; no raw host port) ==="
	@$(CURL_HEALTH) '$(LM_STUDIO_URL)/health' 2>/dev/null | jq -e '.comfyui_status == "ok"' >/dev/null && echo "ok" || echo "UNREACHABLE"

# ── Full deploy: core on amarillo, GPU services on dominus-nobara ───────────────────

deploy: gateway
	@echo "Core runs on amarillo (this host). Deploy steps:"
	@echo "  1. Python change:  docker compose build companion-core && docker compose up -d companion-core"
	@echo "  2. Web change:     rsync web-build/ into the bind-mount (no rebuild)"
	@echo "  3. GPU sidecar:    follow the guarded runbook; installation does not enable/start units without explicit GPU clearance"
	@echo "     Canonical file: $(DOMINUS_AI_DIR)/compose.yaml"
	@echo "     Published APIs: $(DOMINUS_TAILSCALE_IPV4):1234, :8301, :8390 (Tailnet only; ComfyUI uses :1234 facade; transcription disabled)"

# ── Quality gates (mirror CI) ────────────────────────────────────────────────

test-local:
	cd docker/core && python3 -m pytest tests/ -q --tb=short --cov=app --cov-report=term-missing --cov-fail-under=95

lint-local:
	cd docker/core && ruff check app/ --config ruff.toml

type-check:
	cd docker/core && mypy app/ --config-file mypy.ini

security-scan:
	cd docker/core && bandit -r app/ -ll --skip B101
	cd docker/core && pip-audit --requirement requirements.txt

# ── Performance baseline ─────────────────────────────────────────────────────

perf-baseline:
	python3 tools/load-test/probe.py \
		--base http://localhost:8300 \
		--requests 200 --concurrency 10 \
		--out docs/perf-baseline.json
	@echo ""
	@echo "Baseline written to docs/perf-baseline.json"
	@echo "See docs/perf-baseline.md for SLO targets and methodology."
