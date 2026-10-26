# Image reliability and optional APIs

The user wants consistent, successful Klukai images and suitable image APIs.
Perfect output cannot be guaranteed; success means reference-based rendering,
explicit quality checks, recovery from transient transport faults without a
second paid submission, and accurately reported failure states.

The implementation choice is to strengthen the existing fal/Claude pipeline
first and prepare additional reference-edit adapters disabled by default.
Replacing fal outright would discard the owner's approved provider and has no
comparative quality evidence. Automatic paid cross-provider fan-out would
violate the current spending and provider rules. The provider-selection
clarification is pending; fal remains the only active provider.

Preserve on-demand-only generation, existing attempt caps, adult anime identity,
Commander text-only description, intimacy controls, and provider safety checks.
Never inspect credential files or private conversation/memory data during this
work. No paid generation test is authorized. Work in an isolated worktree;
the production checkout is deployer-owned.

Implementation requirements:

- Retry only fal observation GETs after transient network/429/5xx failures,
  within the existing total render deadline. Never retry the submission POST.
- Validate fal job endpoints and refuse redirects before sending credentials.
  Malformed responses produce bounded, prompt-free errors. Policy refusal
  takes precedence over service-error classification.
- A configured checker must explicitly pass before delivery. A checker outage
  may recheck the same candidate within the existing deadline, never rerender
  solely because review was unavailable. Preserve major issues when bounding
  checker output.
- Optional API adapters must share the worker's reference, identity, review,
  and request-deduplication contract. Activation requires separate explicit
  selection and positive provider-specific daily/monthly caps; default is off.
  Research contracts from current official documentation, without claiming
  comparative visual quality absent actual evaluation.

Verification uses offline transport fixtures, actual decoding, durable worker
state, and deadline/failure cases. Live quality, actual API entitlement and
production deployment remain unproven until separately exercised.

## Interruption recovery continuation

Recovery must retrieve paid work rather than merely record its identity.

- Persist each actual provider payload and candidate bytes in the private worker
  database before review. Existing request identity and authorization remain the
  recovery boundary. This stores image-job context, not additional conversation
  history, credentials, or Commander reference images.
- Add `agy_art_runtime.GenerationPending`, a non-failover exception for uncertain
  accepted work. Worker requests/attempts remain pending on this exception.
- A renderer may expose `render.recover(request, references, *, timeout=30.0)`.
  It returns an existing PNG or `None` for pending/unknown and may never submit,
  reserve spend, cancel a job, switch providers, or render another image.
- fal recovery looks up an exact request ID/hash in its persisted queue ledger,
  revalidates endpoint identity, and makes only status/result GETs. Missing old
  records cause no private reads or HTTP requests. Exhausted generation caps
  cannot prevent retrieval of a job already paid for.
- `/recover` authenticates and verifies the original payload digest before any
  observer or checker. If the worker slot is busy, return pending. A saved
  candidate is reviewed without calling the renderer again. Resume may perform
  bounded quality review, so it is observational with respect to image generation,
  not a promise of zero local state updates or zero reviewer inference.
- Review attempts are durably reserved, at most two per candidate including
  attempts interrupted by process death. A persisted pass can complete delivery
  after restart without another review. Rejection during recovery is terminal;
  recovery never starts a corrective render. Existing explicitly configured
  checker-none and legacy intimacy exceptions remain unchanged.
- A missing handle, unknown submission outcome, or interrupted unsupported
  synchronous provider stays pending; it must not authorize another paid image.

Also repair the known repeat-window boundary if the durable core contract allows
it without changing what constitutes a distinct human request. Cover the exact
599/601-second boundary and preserve the original image-job identity.

## Worker release behavior

Keep the owner's no-sandbox/no-permission-prompt settings in the checker while
retaining its disabled tools and isolated customization settings. A coordinated
worker stop must let active request/checker children finish within their current
budgets: signal the main process first, join request threads, then close state.
The 420-second systemd stop allowance covers the 360-second generation deadline
and shutdown margin. Install this policy before draining an older worker, stop
before source checkout changes, and start after the certified deployment or its
rollback reaches a final revision. Preserve durable request/review/cap state.
