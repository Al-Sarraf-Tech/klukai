# Klukai original-conversation search feasibility — 2026-11-07

Scope: read-only source inspection at base `8ae48344fa177c56aa339fb9fa8fe048cfbf7c71`, plus synthetic PostgreSQL experiments. No production database, private conversations, credentials, provider calls, repository edits, installs, downloads, or production settings were used. Worktree: `/home/jalsarraf/git/klukai/.worktrees/conversation-search-20261107`. Source inspection used the app-behavior-to-spec skill. Current source was read, not executed as a deployed app.

## Decision

Exact literal, case-insensitive substring search over all of the authenticated owner's saved original messages is feasible at 100,000 own messages. Add a **GIN trigram index on original content**. Keep existing indexes; the extra owner/time/id B-tree tested is unnecessary for this feature at the tested scale. Retain scope, speaker/date filters, exact tuple pagination, and original context. Do not limit search to recent messages or replace the original wording with summaries.

Use parameterized values and escape the query in this order:

```python
pattern = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
```

The real SQL comparison tested was `content ILIKE $2 ESCAPE E'\\'`, with escaped pattern `$2`; all values were supplied to PostgreSQL `PREPARE`/`EXECUTE` parameters. `owner_id` is the authenticated user, stored as `companion_messages.user_id`, never a trusted client owner field. Roles were `ANY($3)` with `{user,assistant}` by default, timestamps were `timestamptz`, and the exclusive cursor was `(created_at,id) < ($6,$7)` with descending order and `LIMIT 21` for a 20-row page. Full text is searched; no SQL substring/truncation precedes matching.

Use `conn.execute(statement, params, prepare=False)` for varied search terms, or locally force custom plans in the read transaction. Binding remains parameterized. A forced generic plan made the common phrase take 1,905.343 ms instead of 0.587 ms because it chose bitmap GIN plus date scans and sorted the candidates. The same generic plan took 12.323 ms rare and 10.997 ms no-match. This is a deliberately forced stress case, not proof that PostgreSQL's default auto plan selection would choose it for the actual application workload.

## Measurement evidence

Only cached image `sha256:650b442ec4f71f47b92505de963bbc4054f8c7df0df30956aef30e27d7e4dea4` was used. Its repository digest was `postgres@sha256:6f30057d31f5861b66f3545d4821f987aacf1dd920765f0acadea0c58ff975b1`; runtime reports PostgreSQL 17.9, x86_64 Alpine, en_US.utf8 collation/ctype, UTC. It is also the repository's pinned `tests/runtime_backend_support.py` PostgreSQL image. No image pull occurred (`--pull=never`).

The fixture held 300 conversations and 300,000 messages: 100,000 Alice messages, 200,000 messages across ten other owners. Messages averaged 602.62 characters, with deterministic rare phrases, literal wildcard characters, and mixed Japanese/English. Each owner had tied timestamps (groups of three), user/assistant rows, and occasional excluded system rows. The table and all shipped message indexes totaled 260 MB before candidate indexes. The data is deliberately synthetic and repetitive; its index size is not a production storage forecast.

Container limits: one CPU, 2 GiB memory ceiling, 1 GiB tmpfs data directory, `--network=none`, no published ports, no bind mounts, and no existing volumes. `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` measurements below are three-run medians in milliseconds, using warm resident blocks. Query plans retained PostgreSQL's default parallel-worker setting of two, sharing the one-CPU cgroup limit. Timings include executor/scheduling behavior on this host and do not predict cold disk or production load.

| Query | Existing indexes ms | Existing + GIN only ms | Returned rows (limit 21) |
| --- | ---: | ---: | ---: |
| rare | 647.774 | 0.906 | 4 |
| no_match | 698.108 | 0.271 | 0 |
| common | 0.590 | 0.587 | 21 |
| japanese_english | 662.279 | 16.367 | 21 |
| literal_wildcards | 631.178 | 3.064 | 10 |
| speaker_date_cursor | 0.601 | 0.593 | 21 |
| single_character_no_match | 614.657 | 634.536 | 0 |

Rare/no-match/mixed queries initially used `idx_msg_user`, scanned Alice's 100,000 rows, and sorted the matches. The new GIN path used `Bitmap Index Scan` + `Bitmap Heap Scan` + sort for these selective terms. Common phrases used `idx_comp_msg_created` and incremental sort, needing only 40 shared blocks. Speaker/date/cursor used 63 blocks. The GIN index occupied 35 MB on this fixture.

An optional `(user_id, created_at DESC, id DESC)` B-tree reduced common/filter pages to about 0.24 ms and 7–9 shared blocks, but rare/no-match queries remained 0.62–0.66 s. The existing sub-millisecond common path and existing context index do not warrant adding that additional index for this feature.

Short-query limits remain visible: with GIN and the optional B-tree present, PostgreSQL correctly chose the owner index because these patterns yield no useful full trigrams:

| Query | Median ms | Returned rows |
| --- | ---: | ---: |
| two_character_missing | 630.457 | 0 |
| two_character_japanese | 640.863 | 21 |
| two_character_punctuation | 612.995 | 10 |

The punctuation case is literal `%_`, not wildcards. The two Japanese characters are `約束`; the missing pair is `ΩΩ`. These costs fit the root plan's three-second statement timeout in this synthetic experiment, but this is not an assurance under production load. Keep truthful timeout/error responses. A two-character minimum permits these useful short searches; do not silently weaken their scope.

An original anchor plus ten messages before and ten after, restricted to its saved conversation and authenticated owner and ordered by `(created_at,id)`, returned 21 rows in 1.201 / 1.291 / 1.187 ms using the existing conversation/time index. The target lookup used the primary key. Foreign-owner anchors returned zero rows. A synthetic system-row anchor also returned zero because the visible-role filter excludes it.

## Migration implications

The cached PostgreSQL image already contains pg_trgm. No new dependency or package is needed. A new immutable migration can use:

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;
CREATE INDEX IF NOT EXISTS idx_messages_content_trgm
    ON companion_messages USING gin (content public.gin_trgm_ops);
```

Both extension placement and operator-class qualification matter. Existing real-PostgreSQL fixtures set `search_path` to a random schema **without public**. In the disposable database, an unqualified `gin_trgm_ops` failed under that path, while `public.gin_trgm_ops` succeeded and the extension remained in public. The proof used a transaction and rolled its probe schema back. `CREATE EXTENSION IF NOT EXISTS` does not relocate an extension already installed elsewhere; treat an unexpected namespace as explicit existing state rather than silently moving it. Production extension existence/privileges were not inspected.

`app/migrations.py` applies all pending SQL in one transaction and uses a database-wide advisory lock plus 30-second lock timeout. Therefore `CREATE INDEX CONCURRENTLY` is incompatible with the existing runner. A normal index build takes the usual write-blocking table lock during creation; deployment planning must account for this. Synthetic benchmark creation is not evidence of actual production build duration. Update assertions that currently require exactly 27 migrations; preserve the applied-file checksum contract and do not edit old migration files. pg_trgm is a database-level extension intentionally outside per-test schemas, so isolated tests can reuse it while dropping only their own schemas.

## Source/API bindings and verifier patterns

| Claim | Source evidence / binding | Proposed verification |
| --- | --- | --- |
| User identity and authorization | `docker/core/app/routes_extras.py:45` resolves Authorization Bearer through `auth.get_user_from_token`; `auth.py:246` hashes/revalidates tokens, active users, expiry and absolute age. Source-derived, not live exercised. | Route test with missing/invalid token produces 401 without query; real DB contains two owners; every result/context page stays owner scoped. |
| Registration integration | `routes.py:428` calls `register_extras`; the existing history route begins at `routes_extras.py:236`. A dedicated search routes module can be registered here with the existing auth resolver injected. | ASGI route tests with auth patched at the established seam, plus a registration test. |
| Original history source | `migrations/010_companion.sql` creates UUID id/conversation FK, role user/assistant/system, text content, and non-null timestamptz; `070_multi_user_auth.sql` adds text user_id and its index. Search `companion_messages`, not summaries/exchanges/memories. | Actual migrated PostgreSQL, distinct original content and summary decoys, owned and foreign rows. |
| Stable cursor convention | `routes_extras.py:250–303` normalizes timestamp/UUID, uses tuple cursor with descending timestamp and UUID, then reverses the ordinary chat page. This is existing history behavior, not a requirement to reverse search results. | `test_message_pagination.py` covers invalid UUID/date/NUL, canonical UUID/date spellings, and limits; retain these boundary ideas for the new cursor. |
| Original context | The database gives every message a non-null saved conversation id. Authorize the anchor by `(user_id,id)` before selecting within that conversation, and repeat user_id and visible-role filters on each side. | Foreign/random anchors return indistinguishable 404; no cross-conversation or cross-owner rows; timestamp-tie context pages remain stable; bounded before/after windows. |
| Truthful backend errors | `routes_extras.py:310–318` returns 503 rather than fake-empty history on SQL failures. | Inject query timeout / database failure and assert retryable error, not `messages: []`. |
| Existing real PostgreSQL harness | `tests/test_memory_recall_postgres.py:29` uses only `KLUKAI_MIGRATION_TEST_DATABASE_URL`, creates random schema, applies all migrations, patches `db._pool`, and drops only its schema in `finally`; `tests/test_migrations_postgres.py:25` has the same schema/pool pattern. | Use this explicit disposable URL, never fallback to production DATABASE_URL. Exercise real ILIKE/escape/cursor/GIN and migration replay. |
| Existing history fixture limit | `tests/test_message_history_boundaries.py` explicitly uses SQLite after adapting placeholders/casts and says it is not a substitute for real PostgreSQL. | Keep fast boundary tests, add real-PG search semantics rather than inferring ILIKE/GIN correctness from SQLite. |

The initial feature remains an implementation proposal: this subtask did not add or run a product endpoint. Static bindings are not claims of deployed behavior. Route/UI acceptance and integration tests belong to the implementing agents.

## Functional synthetic PostgreSQL proof

`functional.py` reused the parameterized search expression and asserted:

- 63 owned matches sharing one exact microsecond timestamp paginate as 20/20/20/3, with no duplicates or missing ids.
- A newer matching message inserted after page one never disrupts subsequent older pages.
- Five foreign messages placed in the same synthetic conversation never appear in Alice's pages; Bob sees exactly his five.
- Literal `%`, `_`, and backslash match exactly; a decoy that would match a wildcard interpretation does not appear.
- A quote/SQL-injection-looking string is treated as literal text and returns no match.
- Speaker and inclusive-start/exclusive-end date restrictions hold.
- An existing foreign-owner anchor produces no context rows.

All assertions passed; raw summary is `functional-summary.json`. This is feasibility SQL proof rather than a product test suite. It establishes stable pagination for newer inserts; it is not a transaction snapshot guarantee against arbitrary historical insertion or changes to stored messages.

## Available Python environment

`/usr/bin/python3` successfully imported psycopg 3.3.3, psycopg_pool 3.3.0, pytest 9.0.2, httpx 0.28.1, and fastapi 0.136.1. The packages are already installed under the user's Python 3.14 site-packages (httpx is system site-packages). `/home/jalsarraf/.local/bin/pytest` exists and uses `/usr/sbin/python3`. No install was performed. This confirms these imports, not every dependency needed by the repository's full suite. The repository `tgbot/.venv` lacks psycopg/fastapi and is not the suitable environment.

## Reproduction, resource identity, and cleanup

All retained evidence and executable scripts: `/tmp/klukai-search-feasibility-20261107/`. It includes `resource.json` with the exact Docker argv/image/container id, `setup.sql`, `benchmark.py`, all per-query SQL and EXPLAIN JSON files, phase summaries, `context-10.sql`, schema-placement probe, `functional.py`, and cleanup receipts. Commands used were local `docker image inspect`, `docker run --pull=never` by immutable image ID, `docker exec -i ... psql ...`, and `python3 benchmark.py <phase>`. SQL parameter signatures and every query value are in the retained `.sql` files; data is entirely synthetic.

The first owned container was `3f7e63fc8de09575298392ba0f3545572ddd4bfc5ac8e85db92560368b299d39`. Its 512 MiB tmpfs filled during the synthetic load's WAL write; PostgreSQL exited. The failure, exact first resource, and removal receipt remain in `first-*` files. It was discarded, never used for reported measurements, and recreated with the larger temporary allocation. The successful container was `7f312970487b4fce3a36a54de4f8ea7a388eedb6cf2d8e1dc0cd52984d7c47eb`, name `klukai-search-feasibility-20261107`. At final inspection its tmpfs used about 805 MiB. No production container was inspected or altered.

Cleanup status is recorded in `cleanup.json`; only these two owned container IDs were removed, and their absence was checked with filtered `docker ps -a`. No Docker network, bind mount, persistent volume, repository source change, or paid request was created. Temporary evidence remains intentionally for the parent agent's review. No broad Docker prune occurred.
