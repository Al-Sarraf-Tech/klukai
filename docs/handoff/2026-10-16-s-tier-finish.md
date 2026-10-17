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

## Follow-up 2026-10-17: alerts, CI image retention, nightly perf, mutation

Owner decisions: alerts go to the owner on the bus; the CI-built image leaves the isolated heavy
guest through Dominus's guest agent; Codex's quota was exhausted until 2026-10-22, so the owner had
Claude review the PRs (Claude wrote them, so this is an author review, said so on each PR); activation
approved.

| PR / commit | Change | Live evidence |
| --- | --- | --- |
| #9 5323ab9 | Alertmanager -> host bridge -> `bus send owner@amarillo`; Dominus-backed breaker alerts silenced | Prometheus discovers 1 Alertmanager, 0 notification errors/drops; live test alert reached the owner as bus note `1f64201f21d4`, then resolved |
| #10 9dc3e7f, 755d3ae | CI saves the scanned image + manifest; amarillo relay; nightly perf retained and shipped to Loki; nightly window guard; review fixes (guest disk pruning, Loki timestamps, once-a-day warning, relay hardening) | CI 39050626578 green; first nightly summary `2026-10-17T15-51Z-9dc3e7f` relayed, 11 rows in Loki at its `finished_at` |
| #11 d26cf90, 05fa740, 71dea42 | nightly mutation runs `tools.ci.mutation` (virtualized-guest thermal proof); jobs ordered perf -> trivy -> mutation from 19:37 CDT; guest-agent reads 4 MiB; Prometheus retention 35d / 20 GB | CI 39052139032 green; mutation run 39052144718 at 71dea42: 1072/1300 killed (82.46%), 0 errors/timeouts/no-test mutants; retention flag reads back `5w`, samples kept from 2026-10-02 |

Incident: the first live image pull read 16 MiB guest-agent chunks; libvirt caps agent replies at
10 MiB and disabled the heavy guest's agent channel ("No complete agent response found in 10485760
bytes"). Runner and VM stayed healthy; fixed in 05fa740; the channel returns on the VM's next start.

First nightly perf result: the night compared two builds of identical `docker/core` and still reported
`passed: false` because `POST /api/tts` exceeded the rule in one of three rounds (pooled 1.12x). The
per-round rule can fail with no regression behind it; owner to choose pooled-only or 2-of-3 rounds.

## S-tier ledger (honest)

Not an overall S. Rows marked met have direct evidence for `c6d02f0` (2026-10-16) or `71dea42`
(2026-10-17). 11 met, 8 partial, 1 open. By dimension: Code, Testing, Observability and Performance
are at S; Process, Reliability, Security and Docs are not.

| ID | Status | Evidence / gap |
| --- | --- | --- |
| CODE-1 | Met | strict mypy, Ruff lint/format blocking in CI; audit CODE-1 pass |
| CODE-2 | Met | audit module-size + executable architecture contract pass |
| CODE-3 | Met | audit: 1,640 callables, max CC 20, mean 4.43 |
| TEST-1 | Met | CI core: 5,443 passed, 57 skipped, warnings as errors; statements 97.96%, branches 95.17% |
| TEST-2 | Met | unit/contract/property/golden (core), 9 real-backend fault cases, Chrome browser journeys, paired perf; 993 local quality/release tests |
| TEST-3 | Met | `tools.ci.mutation` on the heavy guest, run 39052144718 at 71dea42 (first run 39051043616 at d26cf90, same result): 1,072/1,300 killed (82.46%), 0 errors, timeouts or no-test mutants; every mutant bound to raw pytest observations, source/test hashes and mutmut 3.6.0; strict validator passed. Survivors: affection 161, speech 47, classifier 17, audit chain 3 |
| SEC-1 | Partial | distroless non-root, no shell/pip, hash locks; Trivy 0 HIGH/CRITICAL on the deployed image. License inventory shows LGPL/GPL from Debian base (glibc etc.) and psycopg: needs an owner license policy |
| SEC-2 | Partial | per-build CycloneDX SBOM (93 components), gitleaks clean, actions SHA-pinned; no signed provenance manifest; image-scan job still fetches Trivy's install script by tag |
| SEC-3 | Open | production secrets still in `.env`/override; systemd-creds only proven with a dummy credential |
| REL-1 | Partial | breakers and degradation covered by tests; no live outage drill |
| REL-2 | Partial | CI real-backend fault/recovery cases; shutdown-ordering tests |
| REL-3 | Partial | restore-verified backup and a prepared manual rollback; no automatic rollback deployer |
| OBS-1 | Met | bounded RED/breaker/LLM metrics at token-protected `/internal/metrics` (401 without the token); Prometheus scrapes `job=klukai-core`, target up, live series queried (2026-10-16 follow-up) |
| OBS-2 | Met | 60 recording + 23 alert rules healthy; Alertmanager routes to the owner on the bus (live test note `1f64201f21d4`, resolved; Prometheus 0 notification errors); Grafana loads both provisioned dashboards; retention 35d (20 GB cap) covers the 30-day window, first complete window 2026-11-01 |
| PERF-1 | Met | CI paired disposable gate vs previous `main`: login 1.00, readiness 0.21, max pooled 1.32 (`POST /api/tts`, +2.9 ms, below the 5 ms floor). The per-round rule can fail an A/A comparison (2026-10-17 nightly), so it errs toward false failures; owner decision pending |
| PERF-2 | Met | nightly paired run per night, revision-bound summary kept on the guest (raw bundles 14 days), relayed hourly into Loki stamped at `finished_at` and queryable as `{job="klukai-perf-nightly"}`; the window guard, `passed: false` summaries and a 3-day owner warning make missing collection explicit |
| DOC-1 | Partial | onboarding re-run from a fresh clone fixed real drift (env vars, API, prerequisites); a fresh-VM drill of the stack steps by someone else is still due |
| PROC-1 | Partial | PRs + blocking self-hosted CI; `main` protected with required `Required CI gates`, no force push/deletion (admins may bypass, fleet standard). PRs #9-#11 reviewed line by line by Claude at the owner's direction (7 defects fixed), but Claude also wrote them; independent Codex review queued until 2026-10-22 |
| PROC-2 | Partial | CI builds, scans and retains the exact image with a manifest (tar sha256, layer diff IDs, revision label); the relay requires a green `Required CI gates` run and verifies the manifest before tagging `klukai-core:ci-<sha>`. Not yet proven live (first pull hit the libvirt cap, fixed); production still runs the amarillo rebuild of `c6d02f0`; no second reproducibility build |
| PROC-3 | Met | exact CI-green commit deployed, image label bound, live state read back |

## Owner decisions still needed

1. Done 2026-10-16: `main` protection on with the required `Required CI gates` check.
2. License policy for LGPL/GPL components (glibc base, psycopg) in a private, non-distributed service.
3. Done 2026-10-16/17: metrics token, scrape job, rules, and alert routing to the owner on the bus.
4. Schedule the remaining rows: encrypted secret store, signed provenance, automatic rollback,
   a live outage drill, and deploying a relayed CI image (after the next Dominus boot).
5. Durable fix for the fleet-CI boot deadlock (bus thread e638a8095d1e).
6. Perf gate rule: judge on pooled p99, or require a regression in at least 2 of 3 rounds.
7. Independent review: Codex after its quota resets (2026-10-22), or another reviewer.
8. Retention for the other per-run CI evidence on the heavy guest (about 800 MB per run).
