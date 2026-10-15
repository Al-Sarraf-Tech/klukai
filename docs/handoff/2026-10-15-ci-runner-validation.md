# CI runner validation and merge order

The owner approved repairing and merging PR #6 before updating and merging
PR #5 on 2026-10-15, after a review of both PRs. This explicitly supersedes
the old `dominus` selector restriction in the September 25 power-on handoff;
the change remains entirely on existing self-hosted runners.

## Repair scope

- Preserve all 13 CI/nightly/release jobs and their existing blocking gates.
- Keep hashed core dependencies separate from test-tool installation. Bridge
  has its own test environment because it pins Redis 5.2.1 while core pins
  Redis 7.4.0. Voice fixtures require python-multipart; avatar tests require
  their committed npm lock and Node 24. Operations fixtures require the Docker
  Compose v2 plugin and Bubblewrap; install missing tools from the CI VM's
  distribution packages without upgrading existing packages. These are guest
  tools, not changes to Nobara's host or its AI stack. No inference/model
  runtime is installed.
- Run opt-in migration, durable handoff and account-image recovery tests
  against a digest-pinned disposable PostgreSQL service, never production.
  Keep the existing 75-skip ceiling and 95% coverage requirement.
- Keep application deployment outside this merge operation. The owner's
  23:30 America/Chicago nightly hard stop and 23:45 Nobara shutdown deadline
  remain in force; no scheduling or host-service change is made here.

## Validation

Original PR #6 CI run `38782950144` reached the new runners. Lint, mypy,
dependency audit and image scanning completed successfully. Core reported
5,033 passed with 95.68% coverage, but 84 skips exceeded the ceiling of 75.
Component setup failed because pip's hash mode rejected unpinned pytest;
the combined requirements also contain conflicting Redis pins.

Repair run `38793807790` confirmed 5,060 core tests passed, 57 skipped,
and 96.51% coverage on the real runner. It then exposed missing Compose v2
and `/usr/bin/bwrap` in the VM during operations tests. The workflow now
prepares those native tools explicitly and selects supported Node 24 before
running the unchanged suites. No tests were skipped to accommodate missing
tools.

Fresh local validation on the repaired tree passed: core 5,060 passed,
57 skipped, one performance test deselected, with 96.42% coverage; bot 36,
operations 198 (plus four subtests), voice 33 and bridge 23 passed. Companion
JavaScript passed 113 tests; avatar JavaScript passed 111 with 21 optional
asset tests skipped. ShellCheck, both isolated environments' `pip check`,
workflow YAML parsing and `git diff --check` passed. Core ran against a new
local PostgreSQL 17 container with no production credentials or provider use.
Independent review found no remaining blockers after moving the service-port
expression to step-level environment, where the `job` context is supported.

Final CI results are recorded in the PR description before merge.
Neither a skipped performance probe nor a cancelled run counts
as a passing performance measurement. PR #5's per-frame authorization lookup
still requires streaming latency/pool-load measurement before deployment.
