.PHONY: build build-backend build-pwa run stop logs health gateway gateway-stop deploy perf-baseline test-local lint-local type-check security-scan

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
	docker exec companion-core sh -c "pip install -q --target=/tmp/pylibs pytest pytest-asyncio && PYTHONPATH=/tmp/pylibs:/app python3 -m pytest /app/tests/integration -m integration -q"

# ── Core stack (amarillo) — runs companion-core + datastores ─────────────────

run:
	docker compose up -d

stop:
	docker compose down

logs:
	docker compose logs -f

logs-core:
	docker compose logs -f companion-core

logs-voice:
	docker compose logs -f companion-voice

restart:
	docker compose restart

rebuild: stop build run

# ── Health checks ────────────────────────────────────────────────────────────

health:
	@echo "=== companion-core (amarillo) ==="
	@curl -sf http://localhost:8300/health 2>/dev/null | python3 -m json.tool || echo "UNREACHABLE"
	@echo ""
	@echo "=== companion-voice (dominus 192.168.50.2) ==="
	@curl -sf http://192.168.50.2:8301/health 2>/dev/null | python3 -m json.tool || echo "UNREACHABLE"
	@echo ""
	@echo "=== ComfyUI (dominus 192.168.50.2) ==="
	@curl -sf http://192.168.50.2:8388/system_stats 2>/dev/null >/dev/null && echo "ok" || echo "UNREACHABLE"

# ── Full deploy: core on amarillo, GPU services on dominus ───────────────────

deploy: gateway
	@echo "Core runs on amarillo (this host). Deploy steps:"
	@echo "  1. Python change:  docker compose build companion-core && docker compose up -d companion-core"
	@echo "  2. Web change:     rsync web-build/ into the bind-mount (no rebuild)"
	@echo "  3. GPU sidecar:    LM Studio / voice / ComfyUI live on dominus (192.168.50.2)"

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
