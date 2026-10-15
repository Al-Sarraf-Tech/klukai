# Klukai S-tier Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Prove S across all eight quality dimensions, pass exact-commit CI and deploy the tested immutable artifacts.

**Architecture:** Preserve public behavior while extracting coherent backend and Flutter responsibilities. Replace static checklist certification with executable evidence, then bind isolated CI, scanned artifacts and automatic guarded deployment to one source revision.

**Tech Stack:** Python 3.14/FastAPI/asyncio, Flutter 3.44.2/Dart 3.12.2, PostgreSQL17/Redis/Qdrant, Docker, GitHub self-hosted heavy/light CI, existing Prometheus/Loki/Tempo/Grafana.

**Spec:** `docs/superpowers/specs/2026-10-15-s-tier-completion.md`.

## Global Constraints

- All spec acceptance IDs apply. Keep character, archives, session/image ownership and provider controls unchanged.
- No new spending or GitHub-hosted runners. Heavy jobs use `[self-hosted, Linux, X64, ci-heavy]`; validated light jobs use exactly `[fleet-ci-v1, ci-small]`.
- No secrets in Git/logs/bus/CI; preserve production values and encrypted-store access. No history deletion or destructive data recovery.
- Preserve branch protection and required review. No banned attestation actions. Use existing capacity for artifacts.
- Work only in the existing isolated worktree; main and production stay stable until verified rollout.

## Review Focus

- Pending/cancelled authentication and image work preserves admission bounds, revocation and accepted-job ownership.
- Refactored private/public import seams preserve state ownership and do not duplicate service instances.
- Backend outages fail authentication closed while optional providers degrade to usable text without unauthorized fallbacks.
- Missing, stale, empty or mismatched test/artifact evidence cannot turn a gate green or permit deployment.
- Rollout failure restores the compatible prior code/artifacts and exact settings without restoring over newer data.

## Execution ownership

Root owns audit, CI/release/deployment, supply chain, secret storage, documentation and final integration. Python-structure worker owns core code except the runtime worker's reserved files. Flutter worker owns `flutter_app/` only. Runtime worker owns `db.py`, `memory.py`, `llm_router.py`, `voice_client.py`, `weather_client.py`, `mcp_client.py`, `circuit_breakers.py`, `metrics.py`, `main.py`, `observability/` and new dependency/telemetry modules plus their focused tests. Workers do not stage, commit, deploy or edit shared workflow/config files; root integrates and commits after verification. Communicate cross-domain interface changes before editing another domain.

### Task 1: Replace false-positive audit checks

**Files:** `scripts/s-tier-audit.sh`, new `tools/quality/audit.py`, `tools/quality/policy.json`, `tests/quality/test_audit.py`.

**Interfaces:** Audit commands take a repository root, target tier, optional dimension and evidence directory; emit JSON with source identity, individual check status/evidence and unexamined dimensions. Nonzero means missing/failed required checks. Quality policy supplies numeric bounds and required gate names, not inferred grades.

- [ ] Write regression fixtures for empty selected dimensions, radon C average returning zero, comment-only distroless, stale-content mutation evidence with fresh mtime, missing scanners, malformed results, failed subprocesses and mismatched revisions.
- [ ] Run `python -m pytest tests/quality/test_audit.py -q`; observe the current false positives fail.
- [ ] Implement explicit numeric/semantic validators and artifact-bound evidence; retain CLI compatibility while distinguishing S requirements from S+ calendar checks.
- [ ] Run the audit tests and a baseline audit; record actual open checks without a fabricated overall S.
- [ ] Commit the audited harness and baseline ledger.

### Task 2: Backend structure and strict typing

**Files:** Python worker's core files, new cohesive submodules, `docker/core/mypy.ini`, architecture/complexity policy, focused core tests. Root applies shared formatting/config after workers stop editing.

**Interfaces:** Public imports and protocol data remain compatible. Runtime worker's reserved clients keep their interfaces; communicate typed boundary changes. Architecture validation consumes a documented allowed module graph; complexity validation consumes radon JSON.

- [ ] Capture the original function/API/golden behavior and the strict baseline (442 errors in 82 files at base).
- [ ] Extract oversized orchestration/routes/state responsibilities; add independent contract/differential cases before changing behavior.
- [ ] Type public/internal boundaries, remove existing module error suppressions and optional-null relaxation, and document justified external dynamic seams.
- [ ] Enforce core module size, dependency direction and numeric per-function complexity with negative fixtures that demonstrate gate failure.
- [ ] Run strict Mypy, Ruff/format, architecture/complexity, focused tests and the complete core suite; retain all original behavior assertions.
- [ ] Commit the verified backend changes.

### Task 3: Flutter maintainability and browser behavior

**Files:** `flutter_app/lib/screens/chat_screen.dart`, `memory_archive_screen.dart`, `her_pov_screen.dart`, related focused controllers/views/models and `flutter_app/test/`.

**Interfaces:** Preserve `ChatScreen` constructor injection seams, WebSocket wire messages, voice epochs, reconnect/logout rules, archive ownership and visible theme/accessibility behavior.

- [ ] Run locked dependency resolution and the existing analyzer/VM/browser baseline; record SDK and browser versions.
- [ ] Extract cohesive presentation, transport/state and voice/archive responsibilities into understandable units below the policy limit.
- [ ] Add visible browser journeys for reconnect/revocation, voice cancellation and archive/provider failure states, using deterministic transport/provider fixtures and independent expected UI assertions.
- [ ] Run `dart format --output=none --set-exit-if-changed lib test`, `flutter analyze --fatal-infos`, and separate VM/Chrome test suites; do not hide platform failures with blanket skips.
- [ ] Build the PWA using `scripts/deploy-web.sh --build-only`; preserve local fonts, CanvasKit and privacy assertions.
- [ ] Commit the verified frontend changes.

### Task 4: Dependency resilience and emitted observability

**Files:** Runtime worker's reserved files, new typed dependency adapters and telemetry/SLO modules, focused core tests. Root owns generated docs/alerts/dashboard configuration.

**Interfaces:** Existing client APIs remain stable. New breaker wrappers expose typed callable results and bounded monotonic state transitions. SLO configuration produces route budgets and recording/alert inputs. Metrics label routes by matched template with a fixed unknown bucket.

- [ ] Write failing real-boundary tests for each dependency's open/half-open/recovery behavior, optional-provider text degradation, auth/session fail-closed behavior and cancellation cleanup.
- [ ] Wire actual call sites through breakers; remove the unused instrumentation import and emit working bounded RED/breaker metrics and sampled correlated traces.
- [ ] Introduce machine-readable SLO definitions for actual registered routes and validate generated docs/alerts against them.
- [ ] Test concrete metrics/trace output, cardinality under random paths, outage/recovery and graceful shutdown; run strict typing and focused suites.
- [ ] Root verifies actual existing collector scrape/ingestion and alert/runbook links without changing unrelated services.
- [ ] Commit verified resilience and observability.

### Task 5: Distroless supply chain and encrypted production secrets

**Files:** `docker/core/Dockerfile`, Compose healthcheck, pinned build/tool inputs, release scripts, secret-store systemd wrapper and runbook, artifact/provenance/license tests.

**Interfaces:** Artifact manifest includes source revision, material/tool IDs, image ID/digest, PWA digest and scan/signature references. Secret loader supplies the same environment values without logging them; missing/decryption/permissions failure aborts before rollout.

- [ ] Prove negative packaging/permission/provenance cases before implementation; inspect available pinned bases and actual CPython ABI/library closure.
- [ ] Build a non-root distroless image with Python-based healthcheck, no shell/package manager and working TLS/database imports.
- [ ] Generate SBOM/license inventory/provenance and permitted signatures; scan the actual image and fail unresolved high/critical or unreviewed license policy violations.
- [ ] Migrate existing production secret storage through the available encrypted store without changing values; test missing/decryption/rollback paths with disposable fixtures first.
- [ ] Verify current configuration equivalence, no secret-bearing artifacts, image functionality and controlled build reproducibility.
- [ ] Commit tested packaging and operator interfaces.

### Task 6: Blocking CI and immutable artifact handoff

**Files:** `.github/workflows/ci.yml`, `nightly.yml`, `release.yml`, pinned CI requirements, `scripts/ci/`, isolated Compose/test providers, performance validator and tests.

**Interfaces:** Every required job reports passing evidence for the PR head revision. CI's release manifest identifies the one built/tested/scanned artifact. Export it to existing approved private capacity; deployment verifies digest/signatures/CI/review/source and does not rebuild.

- [ ] Add failing workflow/gate contract tests for warning-and-pass skips, mutable executable inputs, absent Flutter/integration checks, missing artifacts and revision mismatch.
- [ ] Run disposable real PG/Redis/Qdrant and deterministic providers with no production credentials or network fallbacks; exercise API/browser failure journeys and candidate shutdown.
- [ ] Gate candidate p99 against a comparable bound baseline at 1.20 and absolute SLOs; missing samples fail. Keep nightly samples revision/time-bound and actually retained.
- [ ] Run fresh critical-path mutation testing to at least 80%; validate per-mutant outcomes and preserve raw results.
- [ ] Publish the reviewed branch/PR, wait for all required self-hosted jobs on its exact head, inspect every job and repair genuine failures without weakening gates.
- [ ] Commit any required fixes, rerun affected checks and CI on the final exact head.

### Task 7: Automated safe deployment and recovery evidence

**Files:** `scripts/release/`, backup verification/isolated restore tests, deployment runbook, README/architecture/SLO/ADR updates, onboarding evidence.

**Interfaces:** Deployer consumes only the validated release manifest and explicit approved runtime selection. Rollback selects the previously verified compatible artifact and settings; data is never rolled back automatically.

- [ ] Write disposable rollout tests for missing CI, mismatched image/PWA, altered settings, unhealthy candidate, rollback failure and newer database writes.
- [ ] Implement guarded core/PWA activation and sustained readiness/SLO monitoring with automatic compatible-code rollback.
- [ ] Test isolated full backup restoration and integrity, clean onboarding and runbook commands; validate every alert/runbook anchor.
- [ ] Conduct independent whole-branch review against all spec IDs and resolve critical/important findings with red-green tests.
- [ ] Merge through the protected PR path after checks/review, then run the exact tested-artifact deployment. Preserve owner provider/game/GPU policies.
- [ ] Verify public visible journeys, authentication/revocation, archive isolation, bounded permitted provider acceptance, collector signals and the actual deployed digests/revision.

### Task 8: Completion audit

**Files:** Versioned quality evidence, final handoff and fleet run report; private raw evidence outside Git.

- [ ] Re-read every spec acceptance ID and bind it to direct final-revision evidence; no directory/file-name or stale timestamp substitutes.
- [ ] Run complete applicable core/component/Flutter/quality suites and all required CI checks; inspect skips/warnings/coverage/mutation/performance/scan results.
- [ ] Run the truthful full S audit and verify every dimension meets S; retain any missing criterion as open.
- [ ] Read back live commit/artifact IDs, subsystem status, backup/rollback identity and local/GitHub/mirror refs.
- [ ] Publish a precise final report with actual results and limitations; mark the goal complete only when all acceptance IDs are proved.
