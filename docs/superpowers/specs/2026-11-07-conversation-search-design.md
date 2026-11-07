# Search our conversations

Date: 2026-11-07. Owner asked for another improvement, deep research with another agent, and a new feature. Standing authorization permits implementation and deployment without another permission prompt. Agent research/review must use existing OAuth sessions, never paid API inference.

## Decision and evidence

Add authenticated search of original saved conversations, with speaker/date filters and a reader that opens the exact source exchange. Existing serial transcript pagination and image-archive search do not provide this. Independent research is recorded in `docs/research/2026-11-07-conversation-search.md`.

LongMemEval and LoCoMo describe information loss in summaries and difficult temporal retrieval. This supports preserving access to original dated messages; neither paper proves this UI improves model recall or wellbeing. Apple Messages documents the source-navigation pattern. Saved text moments would only help after prior curation; additive memory corrections would also change extraction/prompt conflict semantics. Search fills the retrospective gap with fewer behavioral risks.

## Product behavior

- Chat menu opens **Search conversations** in a pushed route. The active chat, socket, draft and scroll state remain mounted. Search does not send chat messages or invoke a model.
- Search is a literal, case-insensitive phrase/substring using PostgreSQL ILIKE semantics, across all saved user/assistant message text. Characters such as `%`, `_`, backslash and quotes are literal. No stemming, inferred dates or fuzzy matching. Query is trimmed, 2–200 characters, no NUL. The UI explains “Search an exact word or phrase”.
- Optional speaker filter: Everyone / You / Klukai. Optional From / Through local calendar dates. Local midnight starts the range; local midnight after Through is the exclusive end, converted to UTC before sending. Construct the next calendar day, not a fixed 24-hour duration (DST). Filters combine. Reversed ranges are rejected visibly. Filters can be cleared.
- Results are newest first, 20 per page, with author, full local date/time and an exact excerpt (up to 400 source characters, plus truncation ellipses) near the match. No fabricated or generated excerpts. Selecting one opens a read-only source reader.
- The reader initially shows the chosen message with up to ten original user/assistant messages before and ten after, scoped to the same conversation and authenticated user. The chosen message is visibly marked and scrolled into view. Earlier/later controls load another 20 messages into the reader in chronological order, without duplicate IDs or losing the original target. Full original text is selectable. No source content is edited or deleted.
- Loading, initial instructions, no matches, unavailable/retry, loading more and source-not-found states are distinct. Expired authentication uses the existing app callback; mounted/stale-response checks prevent old responses overwriting a new search, disposed screen, or expired session. Controls work at 320px width and enlarged text; labels are accessible.

## API contract

Dedicated `app/message_search.py` query functions and `app/routes_message_search.py` registration, integrated from `register_extras`. Existing GET `/api/messages` is unchanged. All queries use bound parameters, authenticated user ownership and user/assistant roles. Read-only POST bodies keep search phrases out of access-log URLs. Successful and error responses from these routes use `Cache-Control: no-store`. Never log query text or message contents; unavailable errors log only operation and exception class.

`POST /api/messages/search` body:

```json
{"query":"rainy night","speaker":"all","start":null,"end":null,"cursor":null}
```

`start` inclusive and `end` exclusive are nullable timezone-aware ISO instants; naive, malformed, reversed/equal bounds fail validation. `speaker` is `all|user|assistant`. A cursor is `{"created_at":"2026-11-01T00:00:00+00:00","id":"<uuid>"}`. Unknown fields fail validation. Fixed page size is 20, fetching one extra to determine continuation. Tuple `(created_at,id)` gives stable descending keyset pagination, including equal timestamps and new arrivals.

Response: `{"messages":[{"id":"<uuid>","role":"user","excerpt":"...","created_at":"<ISO with offset>"}],"next_cursor":null}`. Non-null next_cursor names the last returned row when more exist. No total count/full-history fetch.

`POST /api/messages/context` body:

```json
{"anchor_id":"<uuid>","before":null,"after":null}
```

At most one of before/after may be a cursor of the same shape. Anchor lookup validates ownership and user/assistant role first; absent, system and other-user anchors all return 404. Every context query also scopes user AND conversation. No cursor can broaden that scope. Initial response contains at most 21 messages, subsequent directional pages at most 20. All are ascending. Response is `{"anchor_id":"<uuid>","messages":[{"id":"<uuid>","role":"assistant","content":"original text","created_at":"<ISO with offset>"}],"older_cursor":null,"newer_cursor":null}`. An edge cursor names the corresponding first/last returned row only when more exist beyond that edge. The client merges by ID and updates only the loaded edge for directional requests.

Both routes: 401 unauthenticated; 422 invalid body; 404 inaccessible context anchor; 503 unavailable/timeout. Bound database work with transaction-local statement timeout 3000ms (do not alter pooled-session defaults). Do not swallow failures into empty results.

## Storage and verification

No history cutoff, new provider, inference, embeddings, credentials, private-data inspection, background task or content mutation. Synthetic PostgreSQL 17 evidence determines the additive index migration; all existing rows and schemas remain intact. Any migration ships with the release system's reviewed, data-preserving recovery plan. Preserve Fish voice, provider selection, existing budgets, prompts/persona and memory semantics.

Meaningful tests cover original messages older than 200 rows; user/role/conversation isolation; wildcards/backslash/quotes/Unicode; combined speaker/time filters; timezone validation; equal-timestamp cursors and newly arriving messages; complete context traversal without duplication; missing anchors; authentication; unavailable versus empty; no-store responses. Run real PostgreSQL tests on a disposable instance only, never production. Measure selective/no-match/common/short queries on synthetic history and document index/timeout limitations.

Flutter service/widget tests cover request shape/date handling, loading/empty/error/retry, paging and context selection, stale responses, auth expiration, navigation preserving draft and incoming messages, long Unicode and narrow/text-scaled layouts. Validate a real web build visually with synthetic content, and run existing cloud-only/sacred-data checks plus required release gates.
