# Klukai S-tier completion

Owner objective: “get everything to an S at least and CI/CD pass it and deploy”.
Base: `f802f47`, including the deployed hardening source `a56979d`.
Worktree: `/home/jalsarraf/.codex/worktrees/cba2/klukai`.
Branch: `codex/s-tier-uplift-20261015`.

## Scope and standard

Evaluate all eight dimensions of the installed Software Engineering Tier Rubric,
with S as the minimum in each dimension. The May uplift spec and ADR-0001 are
historical context, not evidence of implementation. Existing checks that merely
find a file, a comment, a tool name or a recent filesystem timestamp cannot
certify runtime behavior or a successful gate. The rating is the minimum proved
dimension, not a count of passed checks. No unexamined dimension earns a grade.

Keep Klukai's character, public protocols, archives, image ownership and retry
semantics, existing provider selection, spending controls, private account
credentials, voice reference, GPU leases and gaming guards intact. Preserve
single-worker session ownership rather than adding unsafe horizontal scaling.
Do not access restricted repositories. Do not rewrite migration history or
delete existing memories, images, conversations, passwords or sessions.

The owner has authorized implementation, CI publication/review and deployment.
Use protected-branch PR integration and the existing self-hosted heavy/light
runner policy. No GitHub-hosted runners, new paid services, account upgrades,
billable artifact storage, API fallbacks or SLSA attestation actions. Supply-chain
provenance must use a verifiable build manifest and permitted signing tools;
do not represent it as certification of an unverified SLSA level.

## Concrete acceptance ledger

| ID | Required evidence for S |
| --- | --- |
| CODE-1 | Strict Python type checks on all core modules, without the current optional/null and per-module error suppressions; annotated dynamic external JSON boundaries do not leak unchecked values into domain state. Python/Dart formatting and lint are blocking. |
| CODE-2 | Cohesive application modules below 500 physical lines, excluding generated assets/data with a specific documented reason; split image orchestration, scheduler registration, archive and chat presentation responsibilities. Enforce dependency direction with executable architecture checks. |
| CODE-3 | A real numeric cyclomatic gate, including an upper bound for individual functions; no success based on radon's exit code alone. Every type-ignore requires a local explanation. No new dead public facades or exception-swallowing paths introduced by extraction. |
| TEST-1 | At least 95% measured core branch/statement coverage with the full served surface retained, existing skip ceiling unchanged, and no unexpected runtime/resource/coroutine warnings. |
| TEST-2 | Passing unit, public-contract, property, golden, disposable real-backend integration, browser and performance layers. Preserve independent expected behavior for refactors, controlled seeds, cancellation, deactivation, authorization and failure cases. Browser assertions exercise visible functionality, not just HTML download. |
| TEST-3 | Actual fresh mutation results on affection, audit-chain and speech-pattern critical paths, bound to source revision and tool version, with at least 80% killed and no unexecuted mutants counted as success. Preserve raw per-mutant results; invalid/empty/stale evidence fails. |
| SEC-1 | Non-root distroless runtime without a package manager or shell, pinned runtime/build bases and dependency hashes; measured compatibility with the CPython ABI and PostgreSQL/HTTP/TLS libraries. Scan the actual deployable artifact for vulnerabilities and licenses; zero unresolved high/critical findings. |
| SEC-2 | Per-build SBOM, license inventory, source/material/tool identity and artifact digest in a verifiable provenance manifest, signed using existing authorized local/account facilities. Secrets are absent from contexts, artifacts and logs. All workflow actions and executable downloads are immutable or checksum-verified. |
| SEC-3 | Production secret material is loaded through a documented encrypted secret-store path with fail-closed permissions and a tested rotation/rollback procedure. Preserve values during migration; never print them or put them in Git, bus or CI. No automatic credential rotation is necessary to prove storage migration. |
| REL-1 | Each real external dependency boundary uses an exercised circuit breaker and explicit fail-soft/fail-closed behavior. PostgreSQL/auth failure cannot grant access, Redis/session failure cannot cross user ownership, and voice/image/search loss preserves usable text chat. No provider substitution or duplicate accepted image work. |
| REL-2 | Disposable dependency chaos and recovery tests demonstrate breaker transitions and recovery, bounded cleanup, no leaked sessions/tasks and correct readiness. Prove graceful candidate shutdown before database closure. |
| REL-3 | Automated backup verification restores into isolated resources and checks data integrity. The release deployer performs staged preflight, uses a verified prior compatible artifact for automatic rollback on sustained readiness/SLO breach, and preserves current runtime settings. Never restore production data as part of a rollout failure. |
| OBS-1 | Real per-route RED metrics use bounded route-template labels; request/trace correlation is redacted, and unknown URLs cannot grow cardinality. Traces have an explicit sampling policy. Verify emitted signals with requests and collection queries. |
| OBS-2 | Machine-readable endpoint SLOs drive docs, burn-rate recording/alert rules and rollback health budgets. Dashboards and every alert/runbook anchor validate. Verify actual scrape, ingestion and routing; configuration presence is insufficient. Document and verify retention. |
| PERF-1 | Pre-merge performance gates run against an isolated candidate and a bound baseline under comparable resource/configuration conditions. A greater-than-20% p99 regression fails; missing/invalid samples or unreachable targets fail. Document real endpoint paths and SLOs. No changing a baseline to hide a regression. |
| PERF-2 | Nightly performance evidence is timestamped, revision-bound and retained on existing approved capacity, visible through the existing observability stack. Missing collection is an explicit failure. |
| DOC-1 | README, architecture, ADRs, failure-mode runbooks, migration/rollback and provider/GPU policies match the final code and commands. Run onboarding from a clean isolated checkout and record actual steps/results/date. Validate links and commands. |
| PROC-1 | A pull request contains the complete reviewed source. Blocking required jobs include Python and Flutter checks, real integration/browser tests, image/license/secret scans, architecture/complexity and performance. Missing tools or capabilities fail instead of warning/pass. Preserve required review/protection. |
| PROC-2 | Builds use pinned inputs and isolated workspaces with recorded tool identities. Build once, test/scan that exact artifact, retain it on existing free private capacity, transfer and deploy by verified digest; no unverified rebuild after CI. Prove reproducibility by a second controlled build where deterministic inputs allow it. |
| PROC-3 | Final CI is green for the exact reviewed commit; the deployed image and PWA manifests bind to that commit and the tested artifact digests. Smoke public authentication, WebSockets/revocation, archive isolation and actual user-facing journeys. Read back the live state; healthy old containers do not prove rollout. |

S+ adds additional calendar and shared-library requirements and is not the
minimum requested here. Historical 90-day S+ soak claims must remain unproved
unless dated evidence actually establishes them. Mutation measurements made
today are not described as a historical week of observations.

## Approach

Use coordinated, disjoint changes in the existing isolated worktree. Keep
observable contracts fixed during module extraction. Move behavior to a clear
owner and update test injection seams deliberately; do not add layers merely
to satisfy a line counter. Compare original and replacement behavior on the
existing independent character/API/browser fixtures and new boundary cases.

Start with truthful audit results and immutable release identity. Then improve
backend structure/types, Flutter structure and runtime resilience in separate
file ownership domains. Integrate a distroless artifact, complete isolated CI
and deterministic release deployment, and finish with fresh independent review,
the full evidence ledger and live acceptance. Any missing criterion stays open.

## Verification and release boundaries

Local checks use disposable PostgreSQL/Redis/Qdrant and deterministic provider
test doubles, with production keys and GPU/account endpoints absent. Live
provider acceptance is a separate bounded operation under the owner's existing
provider authorizations and spending controls. Never infer provider account
acceptance from its worker's health endpoint.

The release path must not use the ambiguous unqualified production Compose
command. Snapshot actual running settings privately, compare resolved/runtime
values, select explicit immutable image/PWA artifacts, verify backups and
compatible rollback, and poll health/SLOs. Backups and private inspection data
remain outside Git. Ordinary deployment rollback never deletes newer writes.

Complete only after every ledger row has direct passing evidence for the final
source, all required CI jobs pass, independent review has no unresolved critical
or important findings, and the same artifacts are verified live. A green audit
alone is insufficient unless its checks cover these requirements.
