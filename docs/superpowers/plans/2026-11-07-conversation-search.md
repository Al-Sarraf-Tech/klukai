# Conversation search implementation plan

> Execution: superpowers:subagent-driven-development. Owner authorizes autonomous implementation and deployment; no repeated permission prompt.

**Goal:** Let the user find original saved messages and read their surrounding exchange without losing their current chat.
**Architecture:** Dedicated authenticated PostgreSQL search/context queries, fixed keyset pages, Flutter pushed search and source-reader routes; additive measured index only.
**Tech Stack:** Existing Python/FastAPI/psycopg/PostgreSQL 17 and Flutter/Dart/http. No new dependencies or model calls.
**Spec:** `docs/superpowers/specs/2026-11-07-conversation-search-design.md` (binding API and UX contract).

## Global Constraints

- Agent research/review uses existing OAuth sessions only, never paid API inference. Never read auth credential files, production chats/memories/databases, or unrelated restricted repositories. No new spending or installs.
- All original history, memories, affection and episodes are sacred: preserve them. Search is read-only; every query scopes authenticated user, context also scopes conversation. No provider, Fish voice, persona, prompt or memory-semantic changes.
- Fixed search page 20; initial context 10 before + anchor + 10 after; directional context pages 20. Query trimmed 2–200 characters, literal case-insensitive substring. Use the exact API field names and validation rules in the spec.
- UTC API bounds are timezone-aware, start inclusive/end exclusive. UI From/Through dates are inclusive local calendar days, next calendar midnight for Through. Tuple timestamp+UUID cursors, no offset pagination or recent-history cutoff.
- Search text belongs in POST body, never logs/URLs. All new route responses no-store. Database timeout 3000ms transaction-local, unavailable is 503 rather than empty. Existing GET history contract unchanged.
- Work only in this isolated worktree; preserve unrelated files. Conventional commits, no AI co-author. No push, merge, deploy or external messages by implementers; controller handles release through existing gates.

## Review Focus

Ownership and same-conversation source boundaries; literal wildcard escaping; equal timestamps; pagination/date correctness; failure privacy; async stale state and authentication; preserving the mounted chat; synthetic real-PostgreSQL evidence; additive migration recovery; layout at narrow widths and large text.

### Task 1: Search and source-context backend

Read the binding spec and this task's Global Constraints. Implement `docker/core/app/message_search.py` and `routes_message_search.py`; register through `routes_extras.py`. Keep existing GET `/api/messages` unchanged. Model exact request/response contracts from the spec, bound all SQL parameters, user/role and conversation scopes, literal ILIKE escaping, exact original excerpts, stable keyset pages, timezone validation and private failures. An injected auth resolver on registration follows existing dedicated-route patterns. Use transaction-local timeout.

Create focused `docker/core/tests/test_message_search.py` for route validation/auth/error/private headers and query behavior seams, and `test_message_search_postgres.py` for real PostgreSQL semantics using only `KLUKAI_MIGRATION_TEST_DATABASE_URL`, random disposable schema and cleanup like `test_memory_recall_postgres.py`. Write meaningful failing tests before implementation; no substring/AST tests that mirror implementation. Cover the spec's backend acceptance criteria including >200 old messages, other users/system rows/other conversations, literal special characters and multilingual content, tie/new-arrival pagination, all context pages, 404 and 503.

Use final synthetic index evidence in `/tmp/klukai-search-feasibility-20261107.md` (controller will provide if pending). Add `270_message_search.sql` only with measured benefit; extension/index schema placement must work under per-test search_path and production public. No content/table drops. Determine statement timeout behavior with two-character queries. Document migration requirements in report for controller's recovery plan.

Run existing focused history/auth boundary tests as baseline before edits, then new unit/real-PG tests and relevant regressions. Use already installed environments/dependencies only. Ask controller for disposable DB if unavailable; never use production. Run linter configured for changed code. Self-review and commit only your files. Report exact commands, counts, red/green evidence, index evidence, limitations and commits to the designated report file. Do not dispatch subagents.

- [x] Backend implementation and unit/real-PG proof
- [x] Independent task review and fixes

### Task 2: Search and source-reader Flutter UI

Read the binding spec and Global Constraints; consume Task 1's exact API. Add focused `flutter_app/lib/models/conversation_search.dart`, `services/conversation_search_service.dart`, `screens/conversation_search_screen.dart` and `screens/conversation_context_screen.dart`. Small focused helper widgets may live next to the screens when necessary. Integrate a **Search conversations** menu action in `widgets/chat_header.dart`, navigation in `screens/chat_navigation.dart`, imports in `chat_screen.dart`, and action dispatch in `chat_view.dart`. Follow existing Hud colors/text/layout and authenticated HTTP/client-injection/test seams.

Implement explicit submit rather than server calls per keystroke. Query editing/filter changes invalidate in-flight responses and any old pagination; results cannot be mislabeled under changed filters. Keep clear local dates, speaker labels, exact excerpts/timestamps, initial/loading/no-match/retry states and stable paging. Reader centers/highlights original anchor, shows selectable full text, loads earlier/later context without duplicate IDs or losing target. Distinguish not-found from unavailable. Handle authentication expiry once and safely after disposal. Preserve original chat state by Navigator.push; don't replace/unmount chat or send messages. Keep controls usable at 320px and text scale 2.0.

Add meaningful service and widget tests under `flutter_app/test/` with MockClient/completers/synthetic messages. Cover request/cursor/date shapes (including local next-day boundaries), stale responses, loading/empty/503/retry/404/401, pagination, initial target visibility, Unicode/long text, mobile/text scaling. Add navigation integration regression using existing ChatScreen test seams to verify draft and newly arriving message preserved on return. Write failing behavior tests first, then implementation. No production/user data or live model calls.

Run focused tests while iterating; once green run Flutter analyze and the full existing Flutter test suite once (capture failures accurately), then build web using the repository's pinned toolchain. Self-review, commit only task files, report exact verification, red/green evidence and limitations to designated report. No push/deploy/subagents. Controller handles browser inspection, whole-branch review and CI/release.

- [x] Flutter implementation and behavior/layout tests
- [x] Independent task review and fixes

## Controller completion

Preserve the independent research synthesis/citations and benchmark record in repo docs. Create reviewed additive migration recovery record required by `docs/releases/recovery/README.md`; independently review integration, security and deployment safety. Inspect a synthetic web preview. Run required local/release checks once per final change set; push exact source through current authorized protected CI route, deploy certified core/web with existing backup/health/rollback guards, verify public revision/health and read-only synthetic test-user flows. Do not claim deployed until observed. Report the actual outcome and material limits, then mark the active goal complete only when the requested feature is delivered.

### Release maintenance: verified SQLite fixes

CI exposed native security updates required by the existing release gate.
The fixed official SQLite package still matches Debian 13 advisory records
with no fixed-version threshold. Follow the binding supplemental spec
`docs/superpowers/specs/2026-11-07-sqlite-security-adjudication.md`.

1. Implement a strict native verifier and isolated raw/filtered Trivy scan,
   backed by exact vendor package/library evidence and narrow OpenVEX `fixed`
   statements. Test substitutions, unknown findings, malformed evidence and
   scanner failures, then exercise real pinned Trivy.
2. Bind the complete scan evidence through schema 4 of the signed release
   manifest; enforce it at CI, relay and deployment. Preserve historical
   validation explicitly and all existing production and recovery gates.
3. Independently review both implementations and the installation transition.
   Full CI must pass on the exact final candidate. Pause automation before
   advancing main; install the reviewed executing copy through its existing
   command, verify its archived bytes/imports, then relay and deploy using
   the reviewed migration-270 procedure. No production changes before this.

- [x] Security evidence helper, negative tests and real scanner proof
- [x] Schema/CI/relay/deployment integration and regression tests
- [ ] Independent security and executing-copy transition review
- [ ] Exact full CI, certified deployment and live feature verification
