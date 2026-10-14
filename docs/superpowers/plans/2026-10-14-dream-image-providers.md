# Dream and scene provider implementation plan

Implementation remains in the isolated codex/dream-image-providers worktree.
Owner continuation permits preparing and testing reversible code; paid
activation remains separately gated by the existing no-spending directive.

1. Add app/creative_budget.py and behavior tests. Public async function
   reserve_attempt(kind: str) -> str accepts image or text and returns a UUID.
   Raise CreativeBudgetError on disabled/invalid/exhausted/unwritable limits.
   Environment: KLUKAI_CREATIVE_LEDGER (default /images/.creative/attempts.sqlite3),
   KLUKAI_CLOUD_IMAGE_DAILY_LIMIT, KLUKAI_CLOUD_IMAGE_MONTHLY_LIMIT,
   KLUKAI_CLOUD_TEXT_DAILY_LIMIT, KLUKAI_CLOUD_TEXT_MONTHLY_LIMIT; all limits 0.
   Use UTC periods and SQLite BEGIN IMMEDIATE for cross-process reservation.
2. Add app/image_provider.py and focused tests, then dispatch in image_gen.py
   before local gates. Environment KLUKAI_IMAGE_PROVIDER=comfyui|openai,
   OPENAI_API_KEY, KLUKAI_OPENAI_IMAGE_MODEL (pinned Sunburst default),
   KLUKAI_OPENAI_IMAGE_QUALITY=max, KLUKAI_IMAGE_REFERENCE_FILES (JSON list of
   absolute local paths). Expose uses_cloud_images() and cloud readiness.
   Retain direct local portrait/inpainting implementation.
3. Add app/creative_text.py and focused tests. Environment
   KLUKAI_CREATIVE_TEXT_PROVIDER=local|anthropic, ANTHROPIC_API_KEY. Expose
   uses_cloud_text() and complete_creative_text(system, user, max_tokens=1024)
   for cloud requests; reserve text attempts and use the canonical Anthropic
   endpoint and existing message conversion. Change only dream and Her POV
   composition call sites, preserving existing local behavior. Historical
   memory seeding stays local and refuses before DB/GPU/API work whenever
   either creative provider is remote; source-aware durable jobs are needed
   before migrating its partial-failure/retry behavior to paid services.
4. Integrate background picture readiness and the seeder's remote refusal
   with provider-aware checks. Add default-off Compose/example config; isolate new credentials and
   flags in tests/conftest.py. Document actual cap semantics and activation.
5. Run focused tests, full core suite, compilation, diff/lint checks. Independent
   review fixes precede the final report. Report research evidence, prepared
   code, test results and exact remaining authorization/reference adoption.
