# S-tier packaging work in progress

This is development evidence, not a release or an S-tier certification. The
production service still runs the previously verified `a56979d` image. The
complete acceptance ledger remains
`docs/superpowers/specs/2026-10-15-s-tier-completion.md`.

## Runtime contract

The core Dockerfile uses digest-pinned CPython 3.14 and Debian 13 distroless
inputs. It preserves CPython 3.14.8 and the native wheel ABI rather than placing
a 3.14 virtual environment on the distroless Python 3.13 interpreter. The
runtime is UID/GID 1000, matching the existing image/audio volumes, with
root-owned code. Compose adds a read-only root filesystem, dropped capabilities,
no-new-privileges and a bounded temporary filesystem.

Build-only tools are hash-locked separately. Runtime downloads must match the
existing hashes; the source-only http-ece package is built without network
access or implicit build-tool installation. Generated wheel hashes and native
library provenance are retained under `/usr/share/klukai-runtime/`. The runtime
has no pip, ensurepip, shell, compiler or package-installation tooling.

The native dependency discovery rejects unresolved libraries. Auditwheel's
bundled libraries are discovered together, since individual libraries can rely
on the importing extension's RPATH. This discovery-only search path is not the
runtime search path. The actual final image is tested separately.

Desktop-only Tk, terminal readline/curses, C test extensions and build objects
are excluded. The optional native UUID time/MAC accelerator is also omitted;
Klukai uses the supported pure Python UUID parsing, random UUID4 and deterministic
UUID5 paths. Fixed-vector probes check those paths. Compression, SQLite,
PostgreSQL bindings, TLS trust, Pillow and uvloop remain exercised in the image.

The Python healthcheck defaults to `/api/health/live` for the existing autoheal
label. A required-backend outage must not cause an indefinite application
restart loop. Its explicit `--readiness` mode checks `/api/health/ready`, accepts
usable optional-provider degradation and rejects required-backend failures.
Deployment must independently verify readiness and the SLO budget. A regression
test exercises healthy liveness and failed readiness during the same outage.

## Observed development evidence

On Amarillo, the disposable packaging probe image was
`sha256:0aeaab004d10082c88d4c5139a8ad99fe9ddb271e64b73bc9109b3097a25f32f`.
It was built from an intermediate, uncommitted worktree with the deliberately
unverified revision label, so it must not be deployed as a reviewed release.

- Three actual-image tests passed for native functionality, non-root UID and
  absent shell/installers. The original healthcheck test inspected command
  metadata only; it did not execute that command. Subsequent real command probes
  reject this old development image because its default still checks readiness.
  The updated source defaults to liveness, but needs a new artifact and passing
  command probes before release.
- Application import passed under read-only, no-new-privileges, no-capabilities
  settings, with networking disabled and no provider credentials.
- Trivy found zero HIGH/CRITICAL vulnerabilities without ignoring unfixed
  findings. Lower-severity findings remain in the retained report. The initial
  scan had six HIGH findings in unused terminal/native UUID libraries; these
  components were removed rather than suppressing their findings.
- CycloneDX SBOM and raw vulnerability/license inventory were generated.
- The quality suite passed 56 tests. Packaging regressions cover wheel identity
  and hashes, duplicate distributions, unresolved and bundled native libraries,
  and real HTTP readiness responses. Architecture tests enforce domain/storage
  boundaries against HTTP/chat delivery dependencies.
- The new hash-locked CI environment uses patched pytest 9.0.3 and the supported
  Starlette httpx2 test backend. Runtime idna moved from 3.15 to 3.20 for the
  maintained backend's dependency floor; other runtime versions were retained.
  Runtime and CI dependency audits reported no known vulnerabilities.

Complete private logs are under
`~/.local/state/klukai/s-uplift-20261015/`. They are development observations,
not final-revision CI evidence. The final build must repeat the image checks and
bind SBOM, scans, license review, signatures and provenance to one artifact.

The later full core development run passed 5,390 tests with 57 skips and one
explicit performance deselection under warnings-as-errors. Statement coverage
was 97.96% and branch coverage 95.28%, across all 160 runtime modules then
present. This precedes the reviewed cancellation fixes and the application
shutdown-ownership changes, so it is not final-source proof. Independent reviews
have resolved the gateway error-response lock leak, notifier finalizer
interruption, module-graph import-scanning gap and stale Her POV polling races.
Shutdown ownership is integrated in checkpoint `090d1c9`; the full final rerun
remains an acceptance requirement.

An actual Python 3.14.8 CI tool image was built on the existing persistent heavy
guest. Its executed probe matched all 131 packages in the complete hash lock,
and its private probe container was removed. Its revision label is explicitly
unverified. This proves toolchain capability, not a successful GitHub workflow.
The subsequent clean-checkpoint run failed before collection because that
image's pytest-asyncio 1.3.0 imports a Python 3.14 deprecated API under the strict
startup warning policy. Its failed output and successful resource cleanup are
retained. The CI lock now selects the upstream 1.4.0 fix, with explicit fixture
loop scopes; see `docs/VETTING.md`. A new tool image and full run are required.
The frontend runner's invalid `--platform vm` invocation has been replaced with
Flutter's default tester invocation; Chrome retains its explicit platform.
Full clean-source runner execution and final PWA artifact binding remain open.

## Clean checkpoint core evidence

At 01:16 UTC on October 16 (October 15 Central), the full core producer reported
success for clean source
`cbbe65f127a279a5013e50c6754a0b58a5465838`. It ran in newly labeled CI tool image
`sha256:38d4125e7816ba15d02ca22f406be6ea58e53015d25abb3fae60d3f57828b803`,
with CPython 3.14.8 and all 131 packages matching CI lock
`cd64b56d600f64971115f5fdeba2c7196b385a07bce2ab1addf3212932a0a8cc`.
The image is a test executor, not the application release artifact.

The run passed 5,443 cases with 57 skips and one explicit performance
deselection. Warnings remained errors. Coverage was recomputed from retained
SQLite observations and checked against the full 162-module source inventory:
13,990/14,281 statements (97.9623%) and 3,742/3,932 branches (95.1679%). The
producer reported both 95% gates passed, unchanged source and successful cleanup.
Independent validation then found the retained SQLite database contained no
file or arc observations, so its coverage certification was rejected. The
5,443 passing JUnit cases remain valid. Private original manifests, JUnit,
coverage and command
output are retained under `heavy-core-validation-cbbe65f-20261016T011256Z/`
in the private evidence root above.

The earlier strict-startup failure was resolved with the vetted pytest-asyncio
1.4.0 lock and explicit fixture loop scopes. Three test modules then received
scoped setup corrections for deliberately stubbed warmup and in-memory tracing;
the global CI guards, assertions and coverage thresholds were preserved.

CI tool-image reproducibility uses the latest commit timestamp of its five
material inputs for `SOURCE_DATE_EPOCH` (1792111937 here), while the OCI revision
label and tested source bind the complete checkpoint. This image contains no
application or test source. The five inputs were unchanged from `83feed6`, and
their cached layers were reused in one new image build, followed by one probe
and one full core run.

The coverage defect is in source-only analysis: `Coverage()` inherited the
observed `COVERAGE_FILE` instead of using an in-memory database, then replaced
observations with empty source-analysis arcs. Regressions reproduce database
mutation in both successful and rejected validations. The correction selects
`data_file=None` and explicitly closes the parser database. The driver also
closes its read-only SQLite handle, rejects empty observations and verifies the
retained database hash/counts before and after source analysis. Focused real
SQLite regressions pass, including corrupt/empty inputs and handle closure;
the combined quality/helper suite passes 531 cases under warnings-as-errors.
The full clean-source rerun and independent retained-database recomputation
are still required. Original reports are preserved unchanged,
including their rejected success flags. This result does not certify coverage,
other jobs, GitHub branch protection, the application release image or deployment.

## Corrected clean-checkpoint validation

Clean source `4afa6db586365b60b584d8c8395a629bbc308460` completed one new CI-image
build, one full core producer and one managed-backend producer on the existing
heavy guest. The CI image was
`sha256:b8653846686e8698b365a6bc71f2ded193c32c50c247875385ae1e19b6bffbb9`.
Its strict native probe verified CPython 3.14.8 and all 131 locked packages.

Core passed 5,443 cases, with 57 skips and one performance deselection. The
retained SQLite database contains 162 files and 27,271 arcs; its SHA256 is
`960c129c12f2d088caa8f5cecb1c2662973a04c65fa743f721388e1937d5d27c`.
An independent networkless analysis reproduced 13,990/14,281 statements
(97.9623%) and 3,742/3,932 branches (95.1679%), with the database unchanged.
The first observer exposed an unclosed observer-only file handle; that evidence
is retained. A corrected observer completed with empty stderr, without rerunning
the core producer. The original rejected `cbbe65f` database remains rejected.

All nine managed real-backend cases passed with PostgreSQL 17, Valkey 8.1.6
and Qdrant 1.17.0 under the intended non-root, read-only service profiles.
All 21 owned resources were independently confirmed absent and tracked source
remained unchanged. Eighteen expected disposable-service startup notices are
retained and classified; they are not suppressed or described as empty logs.
Private evidence is in
`heavy-managed-validation-4afa6db-20261016T013508Z/validation-report.json`
(SHA256 `ae05c20aae06e63bb9eb0eacf5f91feed34943457c50407dbb67911e68636771`).

The same clean checkpoint completed 254 Flutter VM and 109 Chrome tests with
zero skips, then built an 818-file PWA bundle with digest
`0aa86daaae31eed85dea545532a5b03e212727156d63f9aea78a8e1877399261`.
Production `web-build` was untouched. A separately requested JS run passed
its assertions but was rejected as sandbox-compliant evidence because its
avatar launcher explicitly used `--no-sandbox`. A subsequent local source fix
requires the same native Chrome instance to report active namespace and
Seccomp protections; its full JS suite passed 238 cases with 21 existing
optional-asset skips. Exact committed guest validation of that fix remains open.

The canonical CI-image builder is committed at `1c34860`; it validates complete
material history and a closed, digest-pinned Dockerfile grammar. Independent
review reproduced and closed a hidden-extra-stage parsing defect. Its first
actual clean-commit build remains open. These checkpoint results do not certify
the complete GitHub workflow, final release artifact, all S-tier dimensions or
deployment.

## Secret-store capability probe

A dummy credential successfully round-tripped using systemd 259 with
`--uid=1000 --with-key=host+tpm2 --tpm2-pcrs=7`. A real temporary user unit read
it through `LoadCredentialEncrypted=`. Using the wrong credential name failed
with `243/CREDENTIALS` before its command could run. The units were collected.
This establishes that the existing host can support the planned encrypted
production store; no production credential has been migrated by this probe.

The production migration still requires permission checks, preserved-value
comparison, rotation/rollback tests, operator instructions and startup wiring.
The GitHub login keyring remains a separate prerequisite for authenticated
branch-protection administration.

## Remaining release work

The old CI image/performance jobs no longer pass when their capabilities or
samples are missing, and the performance comparison is restored to 1.20. They
still need the planned isolated candidate/baseline harness, pinned tool/action
inputs and immutable artifact handoff. This change alone does not prove CI/CD
completion. Full integrated tests, strict warnings/coverage, mutation results,
license policy verification, secret migration, collector checks, independent
review, protected exact-commit CI and deployment remain open.
