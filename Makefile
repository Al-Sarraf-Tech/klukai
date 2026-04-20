.PHONY: build build-backend build-pwa run stop logs health gateway gateway-stop deploy

# ── Local (amarillo) commands ────────────────────────────────────────────────

gateway:
	cd gateway && docker compose up -d

gateway-stop:
	cd gateway && docker compose down

gateway-logs:
	cd gateway && docker compose logs -f

# ── Build (run on dominus or build locally and push) ─────────────────────────

build: build-pwa build-backend

build-backend:
	docker compose build

build-pwa:
	@if command -v flutter >/dev/null 2>&1 && [ -d flutter_app ]; then \
		cd flutter_app && flutter build web --release; \
		rm -rf ../web-build/*; \
		cp -r build/web/* ../web-build/; \
		echo "PWA built and copied to web-build/"; \
	else \
		echo "Flutter not available or flutter_app/ not found, skipping PWA build"; \
	fi

# ── Remote (dominus) commands ────────────────────────────────────────────────

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
	@echo "=== companion-gateway (amarillo) ==="
	@curl -sf http://localhost:8300/health 2>/dev/null | python3 -m json.tool 2>/dev/null || \
		curl -sf http://100.111.198.19:8300/health 2>/dev/null | python3 -m json.tool || echo "UNREACHABLE"
	@echo ""
	@echo "=== companion-core (dominus) ==="
	@curl -sf http://100.78.39.76:8300/health 2>/dev/null | python3 -m json.tool || echo "UNREACHABLE"
	@echo ""
	@echo "=== companion-voice (dominus) ==="
	@curl -sf http://100.78.39.76:8301/health 2>/dev/null | python3 -m json.tool || echo "UNREACHABLE"

# ── Full deploy: gateway on amarillo, services on dominus ────────────────────

deploy: gateway
	@echo "Gateway started on amarillo. Now deploy to dominus:"
	@echo "  1. Copy repo to dominus:  rsync -avz --exclude .git . dominus:~/git/klukai/"
	@echo "  2. SSH to dominus:        ssh dominus"
	@echo "  3. Build and run:         cd ~/git/klukai && make build && make run"
