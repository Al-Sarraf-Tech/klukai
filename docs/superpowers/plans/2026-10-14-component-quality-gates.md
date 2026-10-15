# Component quality gates and accurate evidence

Goal: existing isolated component suites become an explicit verification path, and documentation describes measured checks instead of an aspirational rating.

Design: keep core coverage at95%, separate pytest roots to avoid app/tests import collisions, use existing component Make targets plus bridge. Permit explicit existing Python interpreter selection (the bot uses its installed venv on Amarillo). Add component CI steps on the existing self-hosted dominus runner only, using already-declared project dependencies; never silently skip missing tooling or call live model/GPU/account paths. Flutter/browser prerequisites must be verified before deciding its CI placement; local Flutter acceptance remains mandatory. No runner retargeting, new infrastructure or new runtime dependencies.

- [ ] Make isolated test interpreters explicit; add bridge target once implemented.
- [ ] Add isolated Python component CI and JS/shell checks with existing tooling, preserving no-provider defaults.
- [ ] Verify commands independently, inspect runner capability evidence and document unavailable remote checks rather than claiming green CI.
- [ ] Correct active coverage metadata and distinguish45 prompt snapshots from generated-response evaluation.
- [ ] Record exact revisions/commands/skips, then include in whole-branch review.

Prior evidence: ops197+4 subtests, voice33, bot36(existingvenv), shell clean. Source fixes elsewhere still require integrated core/UI verification. No new test framework or mirror-only CI tests.
