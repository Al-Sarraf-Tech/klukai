# Klukai CI acceleration — 2026-10-21

The CI controller and workflows now prioritize quick checks, pack independent
work within the existing eight-vCPU ceiling, and reuse verified build materials.
Production application services, owner schedules and thermal/memory guards are
unchanged. Fresh one-job VMs remain the execution model.

## Measured runs

The final exact-code full run [39702742680](https://github.com/Al-Sarraf-Tech/klukai/actions/runs/39702742680)
passed all thirteen jobs for `eeec54e53cec5ff534b56befaea71ff19376644f`.
Created at 00:56:44 UTC and completed at 01:14:19 UTC on 2026-10-22, it took
**17m35s total**, including **1m56s waiting for the superseded run**. From the
first job at 00:58:40 it took **15m39s**. Total owner wait fell **42%** against the
30m21s successful baseline below; the result did not reach the earlier aspirational
sub-twelve-minute full target. No clean-queue duration is presented as measured.

The primary successful baseline is [39655282278](https://github.com/Al-Sarraf-Tech/klukai/actions/runs/39655282278):
30m21s from creation to completion (29m49s from the first job), all twelve jobs
successful. The later successful [39695523339](https://github.com/Al-Sarraf-Tech/klukai/actions/runs/39695523339)
took 34m21s, including unusual final aggregate queueing; it is retained as a
secondary comparison. The earlier 30m18s run passed substantive gates but failed
formatting and is not used as the success baseline.

The first accelerated full run [39700097964](https://github.com/Al-Sarraf-Tech/klukai/actions/runs/39700097964)
passed every gate in 20m16s (19m14s from the first job); this intermediate revision
exposed additional scheduler idle time subsequently corrected.

The font cache benchmark fetched all 725 pinned fonts cold in 25.580s and verified
and copied the complete warm set in 0.443s with zero downloads. In that full run,
the frontend build phase fell from 164.15s to 48.714s; the whole frontend job was
4m53s versus approximately 6m22s previously. The final run retained all 405 VM
and 215 Chrome tests with zero skips, took 5m02s overall and 46.869s in its build
phase. These are job timings, not a measured
frontend-only pipeline duration.

The documentation-only push [39704656604](https://github.com/Al-Sarraf-Tech/klukai/actions/runs/39704656604)
for `717d51a8329879b5029c2c71f4e30a6d50ccc3f3` passed in **3m06s**
(01:15:46–01:18:52 UTC). Source admission and the Ruff preflight had passed within
39s of run creation. The only changed paths were this handoff and the acceleration
plan. The actual source-job selector output was:

```json
{"components": false, "core": false, "frontend": false, "full": false}
```

Five jobs succeeded: source admission, lint, typecheck, security and the affected
aggregate. Eight jobs were skipped as expected: the executor image, components,
core suite, real backends, image scan, frontend, performance and full aggregate.
The actual affected aggregate validated these exact results. The read-only release
relay rejected this green routine revision because it lacks full certification,
while accepting the exact full `eeec54e` run above. Evidence files are
`docs-selection.json`, `docs-source-policy.log`, `docs-routine-final.json`,
`relay-accept-full.json` and `relay-reject-routine.json` in the task directory.
No frontend-only or backend-only end-to-end timing is claimed without a run.

## Execution and cache behavior

- Source admission precedes checkout. Ruff then runs in the same first VM using
  its existing pinned hash closure; the final measured preflight took four seconds.
- Small jobs use two CPUs, frontend four, executor/image builds four, serial suites
  two each, and performance eight exclusively. Admission backfills free capacity
  and reconciles queue/assignment information from the same deduplicated snapshot.
- The controller polls every five seconds. The global eight-vCPU ceiling, four-GiB
  available-memory headroom and 62/68/60 C start/pause/resume thresholds remain.
- Pip downloads, npm data, Trivy data, SHA256-pinned fonts and verified Docker/OCI
  materials survive disposable VMs. Missing or corrupt reusable data triggers
  repair or a cold rebuild; each run still creates current source-bound evidence.
- The full image scan retains the independent no-cache reproducibility build.
  Performance loads the verified scanned candidate and verifies a cached exact
  baseline or builds it from reusable dependency layers.
- The performance harness installs eleven packages copied exactly from the
  existing CI hash lock. Services still run from the exact baseline/candidate
  images. The benchmark remains **three baseline/candidate pairs (six measurement
  runs), 100 samples and ten warmups per endpoint, concurrency one**. Password
  hashing and the paired comparison contract are unchanged.
- Retired Telegram tests and bot-only CI dependencies are removed; shared runtime
  dependencies remain. No Telegram account operation is performed.
- Idle BuildKit pruning keeps the newest two exports per role, current pointers
  and a one-hour grace period. Existing terminal evidence archival remains.

## Routine checks and release certification

Known component paths select affected suites. Unknown/shared/workflow/dependency
changes run all gates. Empty or unavailable diffs conservatively select full.
Every workflow dispatch runs full CI for its exact expected revision.

The three protected contexts are `Authorized exact source`, `Affected CI gates`
and `Required CI gates` (GitHub Actions app 15368). The full aggregate is skipped
for routine runs; GitHub may display skipped required checks as successful.
The affected aggregate checks precisely the selected successes and expected skips.
The source check remains required so admission/preflight failures cannot become
accepted aggregate skips.

A routine green run is insufficient for release. Both release workflow and relay
require all twelve named full jobs to have succeeded for the exact release SHA.
Dispatch full CI before releasing any revision with only routine evidence.

## Deployment, verification and evidence

The release relay was exercised read-only against GitHub and accepted the full
code revision `eeec54e53cec5ff534b56befaea71ff19376644f` as
`39702742680-1`. Its acceptance does not certify later revisions automatically.

The deployed controller SHA256 is
`290ad3b0dd7a2aec5c168c95dd0f4065d83cd049ba51c393e97e3639e0960e38`.
The original controller is retained at
`/opt/klukai-ci/klukai_ci_runner.py.pre-speedup`.
Controller updates were installed atomically and preserved active guest state.

Local verification included 1,396 broad pinned tests plus four subtests at
`aaf601b` before the final narrow release/scheduler changes, 69 native
interpreter-sensitive cases, and focused workflow/controller/cache/selection and
release-gate regressions. The final scheduler snapshot change passed 100 targeted
runner/workflow cases. Actual full CI verifies the complete exact tree, including
real services, frontend/Chrome, scans, reproducibility and performance.

Durable local evidence is under
`/home/jalsarraf/.local/state/codex-tasks/klukai-ci-20261021/`, including complete
GitHub job snapshots/timelines, CPU samples, font benchmarks, deployed controller
identity and before/after required-check settings. Local broad log:
`/tmp/klukai-ci-final-broad.log`.

Preexisting generated `web-build/index.html` work was left untouched. No new paid
service, production release, application service restart or owner schedule change
was introduced.
