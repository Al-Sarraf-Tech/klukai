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
Shutdown ownership and the full final rerun remain integration requirements.

An actual Python 3.14.8 CI tool image was built on the existing persistent heavy
guest. Its executed probe matched all 131 packages in the complete hash lock,
and its private probe container was removed. Its revision label is explicitly
unverified. This proves toolchain capability, not a successful GitHub workflow.
The frontend runner's invalid `--platform vm` invocation has been replaced with
Flutter's default tester invocation; Chrome retains its explicit platform.
Full clean-source runner execution and final PWA artifact binding remain open.

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
