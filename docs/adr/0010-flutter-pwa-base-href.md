# ADR-0010: Flutter PWA at `/app/` base-href (service worker scope)

- **Date:** 2026-04 (formalized 2026-05-16)
- **Status:** Accepted
- **Authors:** jalsarraf

## Context

The klukai PWA is a Flutter web app served under `/app/` to keep the
root URL (`/`) available for the login page and other non-PWA assets.
Flutter web apps generate a service worker that, by default, claims
the entire origin under its scope. If built without `--base-href=/app/`,
the service worker intercepts `/` and `/login`, breaking the auth flow.

This bit us empirically — caught in `feedback_flutter_base_href.md`.

## Decision

**ABSOLUTE rule**: every Flutter build runs with
`flutter build web --release --base-href=/app/`.

The Makefile `build-pwa` target enforces this. The
`feedback_flutter_base_href.md` memory carries the rule with priority
ABSOLUTE so it can't drift.

Additional constraints captured by neighboring memories:
- `feedback_flutter_web_image_auth.md`: `Image.network(headers:)` is
  ignored on web. Use `http.get` + `Image.memory` for auth-gated images.
- `feedback_login_html_fragile.md`: `rsync --delete` to `/web-build/`
  wipes login.html; deploy scripts use `--exclude=login.html` or
  preserve it explicitly.
- Self-destructing SW at root if base-href is wrong — captured in the
  same memory.

## Consequences

- **Build script is sensitive**: forgetting `--base-href=/app/` once
  ships a broken PWA. Phase 3 pre-commit hook will lint Makefile +
  CI to catch this.
- **Auth flow**: `/` (login) is served as static HTML; PWA at `/app/`
  is the post-auth target.
- **Service worker scope**: limited to `/app/`. Login page reloads
  normally on every visit.
- **Image auth on web**: cannot use `Image.network(headers:)` —
  must use the `http.get + Image.memory` pattern. Already implemented
  in the PWA per `feedback_flutter_web_image_auth.md`.

## Alternatives considered

- **Root-served PWA**: rejected — login page would be intercepted by
  the service worker. Multiple failure modes.
- **Disable service worker**: rejected — loses PWA capability (offline
  fallback, install prompt).
- **Different framework (React/Svelte)**: out of scope; Flutter chosen
  for cross-platform consistency with future mobile native build.

## Related

- `Makefile` `build-pwa` target
- `feedback_flutter_base_href.md` (global CLAUDE.md, ABSOLUTE)
- `feedback_flutter_web_image_auth.md`
- `feedback_login_html_fragile.md`
- `feedback_cloudflare_cache.md` (cache TTL interaction)
- `scripts/deploy-web.sh`
- ADR-0009 (ingress chain)
