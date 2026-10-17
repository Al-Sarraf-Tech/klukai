# S-tier uplift: CI on the fleet and deployment (2026-10-16)

Owner request: "finish what codex did" — Codex's goal was "get everything to an S at
least and CI/CD pass it and deploy" (spec `docs/superpowers/specs/2026-10-15-s-tier-completion.md`).
Owner decisions this session: ship with an honest ledger (no fabricated S), owner powered
Dominus on for CI, approved a one-time fleet-CI unlock, and approved a 5 ms perf noise floor.

## What changed after Codex stopped (2d52345)

| Commit | Change |
| --- | --- |
| 7388223 | Codex's uncommitted PERF-1 semantic verifier, reviewed and committed |
| c81a3dc | gitleaks false positive (a zoneinfo timezone-name assertion in a test) recorded |
| 77cd942 | workflow did not parse on GitHub: `runner.temp` in job-level `env` |
| 455fd21 | `KLUKAI_CI_NATIVE_SANDBOX=unavailable` opt-out; real paired perf harness in CI; containerd image IDs; virtualized thermal telemetry |
| 123b8e9 | inspect templates tolerate omitted keys; AppArmor userns profile for `/usr/bin/bwrap` on heavy |
| 85682b8 | reviewed postgres image's tab-separated env value accepted |
| 5f8183a | Docker 29 lowercase absence messages; components use the pinned Chrome |
| e98c221 | container healthcheck cost 370 ms CPU per probe; now 17 ms (`_socket` + `_json`) |
| dcf66a6 | perf regression must exceed 1.20x and 5 ms (owner decision) |
| d6f19a7 | container logs judged by Python warning signatures (log-level WARNING lines are not failures) |
| c6d02f0 | perf verifier accepts newer Docker inspect spellings (CAP_ prefixes, omitted User, stopped IP) |

Every CI job runs on the self-hosted fleet: heavy `[self-hosted, Linux, X64, ci-heavy]`, light
`[fleet-ci-v1, ci-small]`. Codex's harnesses had never run on the actual fleet; the first dispatch
showed the workflow could not even be parsed.

## Findings worth keeping

- **Perf regression found by the gate's first real run.** The distroless image has no shell or
  curl, so its healthcheck started a Python interpreter that compiled urllib/json from source
  (the image ships no stdlib bytecode): about 370 ms of CPU every 15 s inside core's budget.
  Under the harness's 1.4-CPU quota it CFS-throttled concurrent requests: login p99 +26-30%.
  Production has no CPU limit, but the cost was real. Fixed (17 ms).
- **Gate noise.** Nearest-rank p99 of 100 samples is the second-slowest request. An A/A run
  (identical image both sides) produced 5 false failures under the old rule and 1 under the new
  rule on busy amarillo. Re-scoring earlier runs, the new rule still fails the +55 ms login tail.
- **Fleet CI boot deadlock.** After a Nobara boot, `verify-network.py` refuses while
  `fleet-ci-light-pool.timer` is active, and heavy.py needs its receipt, so neither runner starts.
  Reported on bus thread e638a8095d1e for a durable fix in `feat/fleet-ci`.
- **Heavy guest namespaces.** Ubuntu 24.04 restricts unprivileged user namespaces; Codex had
  installed a persistent AppArmor profile for the pinned Chrome. CI now loads a narrow runtime
  profile for `/usr/bin/bwrap` before component tests. Light containers have no bwrap.

## Release

- Source and GitHub `main`: `c6d02f0` (PR #7, fast-forwarded to the exact tested commit).
- CI: [run 38995269209](https://github.com/Al-Sarraf-Tech/klukai/actions/runs/38995269209), all 11
  required jobs and the aggregate gate green on self-hosted runners.
- Core image `klukai-core:s-uplift-c6d02f0`, `sha256:b05978283780ef9999840757d3dbd146142b4fbcba4094d8528a0938705a69fe`,
  revision label `c6d02f0`, deployed 2026-10-17 02:46 UTC on amarillo with the preserved runtime
  override (50 production values unchanged; image-provided `PATH`/`PYTHON_*` keys dropped). Non-root
  1000:1000, read-only root, all capabilities dropped, no-new-privileges. events-bridge rebuilt.
- PWA `main.dart.e34e8061.js`, `flutter_bootstrap.e1d6942f.js`.
- Live checks: public readiness/app, anonymous and forged-token rejection, authenticated session,
  all subsystems ok (incl. LM Studio, voice, images on Dominus), public WebSocket and revocation
  (4001), token redaction in access logs, zero server errors. `e2e_live.py`: health, auth-required,
  bad-token, authed-read, anniversary, cron-bookkeeping, deferred and chat passed; her-pov,
  her-pov-idor and archive were not run (they spend a real image render).
- Backup `aichat.rollout.dump`; a pre-deploy dump restored into disposable PostgreSQL 17 with
  identical counts (32 users, 1,148 messages, 50 tables). Rollback: image
  `klukai-core:rollback-before-s-uplift-20261016` (a56979d) with the 2026-10-15 override and the
  saved df5cc96 Compose file (the new Compose healthcheck needs `/app/healthcheck.py`), plus the saved
  `web-build`. Private evidence: `~/.local/state/klukai/deploy-20261016-s-uplift/`.

## S-tier ledger (honest)

Not an overall S. Each row is met only with direct evidence for `c6d02f0`.

| ID | Status | Evidence / gap |
| --- | --- | --- |
| CODE-1 | Met | strict mypy, Ruff lint/format blocking in CI; audit CODE-1 pass |
| CODE-2 | Met | audit module-size + executable architecture contract pass |
| CODE-3 | Met | audit: 1,640 callables, max CC 20, mean 4.43 |
| TEST-1 | Met | CI core: 5,443 passed, 57 skipped, warnings as errors; statements 97.96%, branches 95.17% |
| TEST-2 | Met | unit/contract/property/golden (core), 9 real-backend fault cases, Chrome browser journeys, paired perf; 993 local quality/release tests |
| TEST-3 | Open | no fresh mutation run on affection/audit-chain/speech |
| SEC-1 | Partial | distroless non-root, no shell/pip, hash locks; Trivy 0 HIGH/CRITICAL on the deployed image. License inventory shows LGPL/GPL from Debian base (glibc etc.) and psycopg: needs an owner license policy |
| SEC-2 | Partial | per-build CycloneDX SBOM (93 components), gitleaks clean, actions SHA-pinned; no signed provenance manifest; image-scan job still fetches Trivy's install script by tag |
| SEC-3 | Open | production secrets still in `.env`/override; systemd-creds only proven with a dummy credential |
| REL-1 | Partial | breakers and degradation covered by tests; no live outage drill |
| REL-2 | Partial | CI real-backend fault/recovery cases; shutdown-ordering tests |
| REL-3 | Partial | restore-verified backup and a prepared manual rollback; no automatic rollback deployer |
| OBS-1 | Met | bounded RED/breaker/LLM metrics at token-protected `/internal/metrics` (401 without the token); Prometheus scrapes `job=klukai-core`, target up, live series queried (2026-10-16 follow-up) |
| OBS-2 | Partial | 60 recording + 23 alert rules loaded and healthy, retention 15d; no Alertmanager, so alerts are not routed |
| PERF-1 | Met | CI paired disposable gate vs previous `main`: login 1.00, readiness 0.21, max pooled 1.32 (`POST /api/tts`, +2.9 ms, below the 5 ms floor: a small real regression worth a look) |
| PERF-2 | Open | nightly perf retention not implemented |
| DOC-1 | Partial | onboarding re-run from a fresh clone fixed real drift (env vars, API, prerequisites); a fresh-VM drill of the stack steps by someone else is still due |
| PROC-1 | Partial | PR + blocking self-hosted CI; `main` protected with required `Required CI gates`, no force push/deletion (admins may bypass, fleet standard); no independent reviewer |
| PROC-2 | Open | deployed image rebuilt on amarillo from the exact commit, not the CI-built artifact |
| PROC-3 | Met | exact CI-green commit deployed, image label bound, live state read back |

## Owner decisions still needed

1. Done 2026-10-16: `main` protection on with the required `Required CI gates` check.
2. License policy for LGPL/GPL components (glibc base, psycopg) in a private, non-distributed service.
3. Done 2026-10-16: metrics token, scrape job and rules. Still open: an alert destination (Alertmanager route).
4. Schedule the remaining open rows: mutation run, encrypted secret store, signed provenance,
   nightly perf retention, CI-artifact transfer, automatic rollback.
5. Durable fix for the fleet-CI boot deadlock (bus thread e638a8095d1e).
