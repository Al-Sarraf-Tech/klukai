# Image reliability and optional APIs — source preparation

Worktree: `/home/jalsarraf/.codex/worktrees/5466/klukai`, branch
`fix/20261026-image-reliability`, based on `2c3b4a7` for PR validation.
Production is unchanged. This work has not made a paid image request, inspected
credentials, or read private conversation/memory data.

## Behavior

- fal polling/result GETs retry transient transport failures, HTTP 408/429/5xx,
  against the same remote job within the existing 150-second render deadline.
  Each HTTP timeout is capped by the remaining budget. Submission POSTs never
  retry. Unknown submission outcomes and observation deadlines cannot trigger
  provider failover.
- Authenticated fal requests require HTTPS `queue.fal.run`, refuse redirects,
  and bind status/result paths to the returned job ID. The submission disables
  fal's equivalent-model fallback to retain the selected Seedream model.
- fal queue IDs and endpoints are saved in `fal_jobs` in the existing attempt
  ledger before polling, with a local request ID/hash and timestamp, without
  prompts, reference bytes or keys. The hash includes the worker's durable
  `_render_attempt` index, so identical retry prompts cannot recover an earlier
  attempt. Missing/malformed submission handles remain uncertain. Recovery
  looks up only the exact attempt and makes GETs, even with exhausted caps;
  it never submits, reserves spend, cancels, or switches provider.
- Configured Claude review must explicitly pass. Invalid, unavailable or
  contradictory reviews recheck the same image at most once within the worker
  deadline. Reservations persist before each review: at most two per candidate,
  including calls interrupted by process death. Exhaustion fails with
  `check_unavailable`; an elapsed worker deadline with review allowance left
  stays pending for recovery. Review outages never cause a new render.
  Major issues are evaluated before truncation and retained first for correction.
  Existing explicit checker-none and legacy provider intimacy behavior remain.
- Worker tables `attempt_payloads` and `review_attempts` preserve the exact
  provider variant, candidate pixels and bounded review history across restart.
  `/recover` verifies the original caller/request digest, holds the worker slot,
  and resumes observation/review only. A saved pass completes without another
  check; a saved rejection never redraws on recovery. Completion stores output
  and clears the duplicate candidate blob atomically. Legacy pending rows without
  recovery identity stay pending. Unknown synchronous submissions are never
  resubmitted. The private worker DB now retains the image-job prompt; it does
  not collect additional chat history, credentials or Commander portraits.
- Core tracks recovery in its busy-wait counter, so other human pictures wait
  for a candidate under review. Repeated chat requests now claim an elapsed
  600-second window atomically in PostgreSQL instead of crossing fixed clock
  buckets. User, original sentence, scene, dimensions, people and intimacy grant
  remain distinct; original payload, archive context and age are never renewed.
  The existing journal/index suffice; no core schema migration is required.
- Optional `openai` and `gemini` providers use the same locked anime brief,
  approved Klukai reference, text-only Commander and worker review. The service
  unit remains fal-only. No automatic images or new paid fallbacks are enabled.
- The checker honors the owner's service-wide no-sandbox instruction with
  `bypassPermissions`, `sandbox.enabled=false`, and no permission prompt. Its
  tools/MCP remain disabled and its subscription account selection is unchanged.
- The service unit uses `KillMode=mixed` and `TimeoutStopSec=420`. On stop, only
  the main process receives the initial signal; its non-daemon request threads
  can finish their existing generation/review budget before group termination.

## Optional provider contract

| Selection | Model | API | Quality/output |
| --- | --- | --- | --- |
| `openai` | `gpt-image-2.5-sunburst-2026-09-08` | Images edits | `max`, PNG, requested dimensions |
| `gemini` | `gemini-nano-banana-2.1` | Interactions | 2K, closest supported aspect ratio, inline image normalized to PNG |

The model IDs and schemas were checked against the current official
[OpenAI edit reference](https://developers.openai.com/api/reference/resources/images/methods/edit)
and [Google Interactions reference](https://ai.google.dev/api/interactions-api.md).
These are reference-edit candidates, not an empirical ranking of Klukai's
visual quality. Neither API has been called for this change.

The host worker reads keys through the existing encrypted `compose-env` wrapper
at dispatch only: `OPENAI_API_KEY` or `GEMINI_API_KEY`. Keys must never be placed
in unit files, example config, logs, commits or messages. Each provider requires
both positive allowances in the worker environment:

| Provider | Daily | Monthly | Optional ledger path |
| --- | --- | --- | --- |
| OpenAI | `KLUKAI_OPENAI_IMAGE_DAILY_LIMIT` | `KLUKAI_OPENAI_IMAGE_MONTHLY_LIMIT` | `KLUKAI_OPENAI_IMAGE_LEDGER` |
| Gemini | `KLUKAI_GEMINI_IMAGE_DAILY_LIMIT` | `KLUKAI_GEMINI_IMAGE_MONTHLY_LIMIT` | `KLUKAI_GEMINI_IMAGE_LEDGER` |

Defaults are zero; missing, zero or invalid allowances fail before private
reference/key reads. Every dispatch reserves an attempt atomically under UTC
daily/monthly caps. These limits count attempts, not dollars. The production service
selection remains `KLUKAI_ACCOUNT_IMAGE_PROVIDERS=fal`; adding adapter code is
not spending authorization or evidence of a configured account. Do not change
active providers under this handoff alone.

The new adapters submit once, accept exactly one inline image, decode and
normalize it, reject reference echoes and invalid dimensions, and refuse
redirects. Refusals do not fall through to another provider. A timeout or 5xx
after dispatch is uncertain completion and does not permit another generation.
No new dependencies were installed.

## Privacy and recovery limits

fal `sync_mode` excludes image output from request history; this is not a
blanket zero-retention guarantee. The new Google adapter sets `store=false`
and does not create a persistent interaction for later retrieval. OpenAI edits
have separate abuse-monitoring retention rules. See the official
[fal queue](https://fal.ai/docs/documentation/model-apis/inference/queue),
[fal platform headers](https://fal.ai/docs/documentation/model-apis/common-parameters),
[Google interactions](https://ai.google.dev/gemini-api/docs/interactions-overview),
and [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data).

Remaining goal work: evaluate real reference-based quality and deploy through
the existing production workflow. A source test pass cannot prove live provider
entitlement, image quality, or deployment. Provider expansion and paid live
evaluation remain constrained by the owner's existing spending/provider rules.

## Validation

Final `PYTHONPATH=scripts make test-ops`: **982 passed, 5 subtests passed**
(65.36 seconds). Full offline core after integration with `2c3b4a7`:
**6,824 passed, 108 skipped, 1 deselected**, 97.43% coverage (95% gate passed).
New repeat-window contracts: **12 passed** against disposable PostgreSQL,
independently rerun on the final branch in 3.06 seconds. The broader
focused DB/human-origin/archive/grant checks also passed. The temporary database
container and its owned socket directories were removed.

End-to-end worker/renderer recovery tests use actual SQLite state and the fal
adapter with fake HTTP only: restarting after submit, during review and after
approval cannot trigger a second image. All nine cases passed. fal tests: 90;
optional adapter tests: 73. Compile, full application Ruff formatting/checks,
mypy (174 source files), targeted script correctness checks and diff checks
passed. The core suite reports one existing Starlette/httpx deprecation warning;
no dependency was changed for it.

The final checker/unit delta passed all 35 focused tests and is included in the
982-test operations rerun. No implementation source changed after these final
suites; subsequent edits record validation and release notes only.

Independent review reproduced three transport defects: polling authorization
errors allowing failover, accepting a final response after the deadline, and
missing interrupted-HTTP handling. Six regression failures reproduced those
findings before fixes; the focused and full suites above passed afterward.

A second review reproduced unknown submission replies becoming terminal and
identical prompt variants aliasing an earlier queue attempt. Five integration
failures reproduced those cases before the pending-state and attempt-identity
fixes; the final suites above include them. Independent review of the repeat
claim found no actionable defect.

Production advanced independently to `8a7b951` during this work. The production
checkout was clean and its image service active when observed; none of these
changed image source/test files overlapped the intervening production commits.
The work remains isolated from production. On 2026-10-26 the owner explicitly
requested completion and a PR, authorizing publication of this change. The
latest-main integration review found no overlapping source change or API conflict.

## Release boundary

The existing deployment path requires an approved PR and certified CI for its
merge commit, then the deployer-owned checkout and signed core artifact. Local
unit/integration results are not that certification. PR publication is authorized;
merge, deployment and provider activation are separate from opening the PR. Before release,
account for the private worker's additive SQLite tables: old code can still read
the original tables, but cannot perform the new resume behavior. The image
worker must be restarted after the deployed source changes; preserve its state
directory so request IDs, spend caps and review reservations survive.

### Observed runtime and coordinated rollout

The production image worker was active with PID 72549 and a start time of
2026-10-24 17:18:06 CDT. This establishes process age, not provider health or
successful image generation. Its installed unit still had a 10-second stop
timeout and `KillMode=control-group`. The installed Claude CLI's help confirmed
the checker options; help-only inspection made no inference request.

`ops/amarillo/scripts/klukai-deploy-on-merge.py` deploys checkout/core/web and
performs health checks, but does not restart this separate host worker. The
release coordinator must account for it explicitly:

1. Obtain the existing approved-PR and exact-commit CI release evidence. Follow
   [deploy-on-merge](../runbooks/deploy-on-merge.md); local test logs and a patch
   do not substitute for certification. PR branches require an explicit `ci.yml`
   workflow dispatch with `expected_revision` set to the final branch commit;
   a branch push or PR event alone does not trigger this repository's CI.
2. Serialize with the active deployer using its existing deployment coordination
   and shared lock. Prevent another checkout switch during worker draining.
   Install/reload the new unit's stop policy **before** stopping the old worker;
   otherwise its old 10-second/group-wide policy can interrupt the checker.
3. Drain and stop the worker before switching source. Preserve its private
   state directory and fal ledger; do not clear uncertain requests to force a
   retry. Legacy pending jobs without stored recovery identity remain pending.
4. Let the certified deployment finish, including rollback if needed. Start the
   worker only after the checkout reaches its final revision. Verify the new
   process start time, socket health and selected fal provider. Restarting the
   unit alone does not certify image quality or activate optional providers.

No production unit, timer, service, checkout, credential, or provider setting
was changed by this task. The existing unit parser accepted the prepared unit;
it emitted only an unrelated installed `git-sync.service` documentation-URL
warning. Independent review found no actionable issue in the shutdown/CLI
delta. Live image quality and any new provider spending still require separate
evidence and authorization under the standing provider rules.
