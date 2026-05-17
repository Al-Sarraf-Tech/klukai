# ADR-0009: Ingress chain — Cloudflare → nginx gateway → companion-core

- **Date:** Origin (formalized 2026-05-16)
- **Status:** Accepted
- **Authors:** jalsarraf

## Context

klukai is served publicly at `klukai.appnest.cc/app/`. The TLS
termination, DDoS protection, edge caching, and bot-mitigation
features of Cloudflare are useful. amarillo runs on a residential
internet connection; exposing companion-core directly is unwise.

## Decision

Ingress chain (three hops):

1. **Cloudflare** (`klukai.appnest.cc`) — public TLS, DDoS, caching.
   Per `feedback_cloudflare_cache.md`: caches JS via Cloudflare;
   nginx gateway sets `max-age=60` for `/app/` so stale JS clears in
   a minute.
2. **nginx gateway** on amarillo (`gateway/docker-compose.yml`) —
   reverse proxy, internal TLS termination, request ID injection,
   serves Flutter PWA static assets from `/app/`.
3. **companion-core** on amarillo (`docker-compose.yml`,
   `companion-core` container) — FastAPI app bound to
   `127.0.0.1:8300` (per Phase 2 hardening commit `8679409`).

Phase 2 also added autoheal labels (`autoheal: "true"`) so a healthcheck-
failed container restarts automatically if/when an autoheal daemon is
deployed.

## Consequences

- **Three-hop latency**: ~30-80ms p99 over Cloudflare. Within
  `docs/slos.md` budgets.
- **Cloudflare cache invalidation**: long-cached JS would prevent
  hotfixes from reaching users. The 60s `/app/` max-age is the
  trade-off (some staleness, fast cycle on rollback).
- **Loopback bind on core**: companion-core is NOT reachable from
  outside amarillo's loopback. nginx is the only public ingress
  per Phase 2 hardening.
- **WebSocket support**: nginx upgrade_proto config handles
  `/ws` traffic for chat real-time updates.
- **Flutter --base-href=/app/** is MANDATORY per
  `feedback_flutter_base_href.md` — otherwise the service worker
  intercepts the login page and breaks auth.

## Alternatives considered

- **Direct Cloudflare → companion-core**: rejected — no caching
  layer for static PWA assets, no easy WebSocket upgrade handling.
- **Tunnel only (Cloudflare Tunnel + no public IP)**: simpler ingress
  but loses edge cache benefits. Worth reconsidering Phase 4.
- **Self-hosted Caddy instead of nginx**: equivalent functionality;
  nginx was the existing choice and is well-understood.

## Related

- `gateway/docker-compose.yml`
- `docker-compose.yml` (companion-core loopback bind)
- `feedback_cloudflare_cache.md` (global CLAUDE.md)
- `feedback_flutter_base_href.md`
- `feedback_login_html_fragile.md` (rsync --delete wipes login.html)
- Phase 2 hardening commit `8679409`
- ADR-0010 (Flutter PWA base-href)
