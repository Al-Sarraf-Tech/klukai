# Agy artwork integration implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an account-authenticated Gemini advisory artwork reviewer and verify the owner's requested image-generation capability through Agy.

**Architecture:** An operator-run manifest selects up to six public fictional images. A pinned Agy process runs in a sparse Bubblewrap filesystem, using the existing official session keyring with HOME unchanged. A read-only custom agent produces validated streaming evidence and structured reviews. Generation is a separate, explicitly requested one-off experiment, not a production image-provider change.

**Tech Stack:** Python, Pillow, Bubblewrap, Antigravity CLI 1.3.5, pytest, existing RabbitMQ bus.

**Spec:** Owner conversation: both Klukai artwork workflow and Gemini bus membership; existing AGENTS.md no-spending rule; latest instruction explicitly requests an image-generation test.

## Global constraints

- No new charges, API credentials, ADC, purchased credits, or fallback provider.
- Existing account baseline quota only; useG1Credits=false.
- No copied credentials, archive writes, automatic retries, or GPU-stack changes.
- Production image allowances remain zero. Only the explicitly requested generation test may use the account tool.
- Preserve unrelated concurrent fleet changes; notify all 19 peer identities through bus.

## Review focus

- Changed or malformed images must fail before upload.
- A successful process without actual image reads is not a review.
- Added tools, denied actions, partial output, and timeouts must fail closed.
- Global settings/hooks and paid environment selectors must not enter the worker.
- Hidden visual details must remain uncertain; no automatic pass/fail or image replacement.

### Task 1: Isolated account runtime and reviewer

Files: scripts/agy_art_runtime.py, scripts/gemini_art_review.py, ops/amarillo/tests/test_gemini_art_review.py.

- [x] Write failing tests for Agy environment, stream status, image-read evidence, unexpected tools, timeout behavior, and old response rejection.
- [x] Implement sparse filesystem launch and read-only custom agent; preserve official keyring auth.
- [x] Adapt reviewer to Agy structured output and verified reads.
- [x] Run pytest ops/amarillo/tests; expected all pass.
- [x] Run an authorized image pilot; inspect stream evidence and compare all six results against independent observations.

### Task 2: Generation capability, documentation and fleet integration

Files: docs/handoff/2026-10-14-agy-artwork.md, scripts/README.md (if existing), AGENTS.md.

- [x] Run one account-quota generation request with credit overages disabled; verify actual image artifact, or record concrete service denial.
- [x] Document usage, pilot limitations, generation result, and bus registration evidence.
- [x] Obtain independent code review; fix material findings and rerun affected checks.
- [ ] Integrate verified files into main workspace; send final accurate bus status to all peers.

## Execution evidence

- Explicit `Both` and `Keep going` authorized implementation; no additional design approval was needed.
- Initial tests failed before implementation. Final 46 ops tests pass.
- Six local outfit renders reviewed through Agy; independent comparison in private pilot evidence.
- One generation test produced a verified 768 × 1376 JPEG through the existing account.
- Independent review found finish/lifecycle validation gaps; regression tests reproduced them, fixes pass, and retained native events replay successfully. Native finish may be a separate step type; both forms are validated.
- Custom-agent startup reports the global tool registry; the read-only tool gate is the enforcement boundary. A Secret Service-only proxy replaces direct session-bus exposure.
- Integration into the original main workspace follows the owner's approved add-and-continue task. No GitHub publication.
