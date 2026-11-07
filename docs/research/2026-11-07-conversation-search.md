# Klukai feature research — 2026-11-07

Researcher: independent Codex research subagent. Target: `/home/jalsarraf/git/klukai`, detached source revision `8ae48344fa177c56aa339fb9fa8fe048cfbf7c71`, verified by `git rev-parse HEAD`. This is source inspection and public research, not observation of the running service. No private conversations, production memories, credential files, databases, or external accounts were read. No repository files were changed.

## Recommendation

Build **Search our conversations**: authenticated search over original text messages, with explicit speaker/date filters, exact dated excerpts, and an action to open the surrounding exchange. This fills a verified user-facing gap while reusing sacred, already-stored history. It can work entirely in PostgreSQL and Flutter, without cloud inference, new subscriptions, local embeddings, scheduled work, or mutations to historical messages.

The practical value is direct access to something the user or Klukai actually said: a recommendation, a decision, an inside joke, an important date, or wording they want to revisit. The proposed feature improves the user's ability to retrieve and inspect history. **Do not claim that it improves Klukai's LLM recall, emotional outcomes, or conversational fidelity unless those separate outcomes are measured.**

Research supports the design rationale rather than proving this UI's effectiveness. Two independent conversational-memory benchmarks identify summary information loss and difficult temporal reasoning. A current human-data study highlights the difference between user-supplied context and automatically constructed memory. An established messaging UI provides a concrete source-navigation pattern. Details below distinguish research findings from design inferences.

## What already exists

| Capability | Source evidence at the target revision | Implication |
|---|---|---|
| Durable original chat messages, with role, content, content type, timestamp, UUID | `docker/core/migrations/010_companion.sql:15`; per-user index in `070_multi_user_auth.sql:41` | Search can use the source of truth; no synthesized history is required. |
| Authenticated chronological history endpoint, most recent 50 by default, bounded to 200, older pages by timestamp+UUID | `docker/core/app/routes_extras.py:234–312` (`GET /api/messages`) | The current endpoint scrolls history but has no text query, speaker filter, date range, or centered target lookup. Timestamp+UUID tie handling already sets a useful contract. |
| Flutter transcript pagination and explicit failed-history state | `flutter_app/lib/screens/chat_screen.dart:296`; `flutter_app/lib/screens/chat_history_state.dart:1` | Keep the existing chat draft and live transcript intact when entering/leaving search; retain retry vs empty-result distinction. |
| Photo archive search | `docker/core/app/routes_extras2.py:366–406` (`GET /api/memories/search`) | This searches kept image-memory annotations and prompts with ILIKE. It does not search `companion_messages` and does not eliminate the text-history gap. |
| Model-selected episode/exchange recall | `docker/core/app/memory_recall.py:1–120`; `docker/core/app/memory_exchange.py:28`; migration `260_memory_full_text_search.sql` | FTS exists for episodes/exchanges and feeds chat. It is separate from a user browsing original messages. Current recall bounds query/text candidates and may select/clip content. |
| Recent-thread continuity | `docker/core/app/conversation_continuity.py:52–115` | A new generic "remember our last chat" feature would overlap existing work. |
| Facts, relationship facts, inside jokes, milestones | `docker/core/app/memory_facts.py:14–157`; `docker/core/app/fact_store.py:75–108` | Important-fact storage exists. The fact store uses upsert/replace, so a correction UI needs a deliberate additive overlay/history contract. |
| Promises, followups, manual resolution API | `docker/core/app/promises.py:42–221`; `docker/core/app/routes_extras3.py:356–388` | Another automatic reminder mechanism duplicates existing behavior. A user-facing commitments view would expose/control an existing capability. |
| Event brief/sendoff/report sequence | `docker/core/app/op_brief.py`; `docker/core/app/op_brief_render.py`; `docker/core/app/proactive/engine.py:457` | Calendar-like followups already have implementation. Do not recommend them as entirely new. |
| Long-authored Thread, image album, POV and dreams | README sections “The bond”, “Her POV and dreams”; `routes_thread.py`, `memory_archive_query.py` | These address different needs from original text retrieval. |

The negative feature finding is bounded: searches across `docker/core/app/routes*.py`, `flutter_app/lib/screens`, `flutter_app/lib/services`, and relevant migrations found no text-history search endpoint or Flutter text-search screen. It is not a runtime proof that every route is mounted or every UI has shipped. README provider names are stale relative to AGENTS.md; source and current owner instructions take precedence over README provider descriptions.

## Public evidence and interpretation

### E1. Original dialogue matters; summaries are not equivalent

**LongMemEval**, Wu et al., originally posted 2024-10-14; read arXiv v2 full-paper sections 5.2 and 5.4 through the arXiv MCP. The benchmark covers information extraction, multi-session reasoning, temporal reasoning, knowledge updates, and abstention. Its session/round comparisons found that replacing source rounds or sessions with summaries/facts generally reduced QA performance, with multi-session reasoning an exception. Time-aware retrieval helped in the evaluated setting, but depended on accurate time-range inference. [Paper](https://arxiv.org/abs/2410.10813v2), [full paper](https://arxiv.org/html/2410.10813v2).

**LoCoMo**, Maharana et al., originally posted 2024-02-27; read arXiv v1 full-paper result section 6.1 and limitations section 8. It independently reports weak performance when session summaries lose details, and difficult temporal questions. It uses generated conversations verified/edited by humans, English-only, with older evaluated model generations. [Paper](https://arxiv.org/abs/2402.17753v1), [full paper](https://arxiv.org/html/2402.17753v1).

**Design inference:** let users inspect original dated exchanges and choose date filters themselves. This does not require an LLM to infer the desired time window. Neither paper tests a search UI or Klukai's current models. Their numeric benchmark gains must not be presented as expected Klukai gains. The papers also do not imply that raw logs always outperform all structured memory; granularity, question type, reader, and retrieval quality matter.

### E2. Search results should open their conversational source

Apple's current Messages guide documents searching words/phrases, combining criteria/filters, highlighting matches, and opening the transcript at the chosen result. No explicit publication date is displayed; fetched 2026-11-07. [Apple Messages: Search for a conversation](https://support.apple.com/en-ie/guide/messages/icht6d66aae3/mac).

**Design inference:** a result should include its author and date and open the surrounding conversation. This is a documented product pattern, not comparative evidence that it is the best UI. Klukai need not add Apple-specific image/object search.

### E3. Inspectable, user-controlled memory is a strong alternative

**The Algorithmic Self-Portrait: Deconstructing Memory in ChatGPT**, Dash et al., arXiv initially posted 2026-02-01, v3 read; published in WWW 2026, pages 3471–3482. The study analyzed 2,050 memory entries in donated histories from 80 recruited users; qualifying memory entries were found across 65 of them. In this sample, 96% of memory updates were system-initiated, and 84% were directly grounded in user context. Read full-paper dataset and concluding-discussion/limitations sections, in addition to the introduction. [Published paper DOI](https://doi.org/10.1145/3774904.3792671), [arXiv full paper](https://arxiv.org/html/2602.01450v3), [TU Delft publication record](https://research.tudelft.nl/en/publications/the-algorithmic-self-portrait-deconstructing-memory-in-chatgpt/).

These are sample findings, not Klukai measurements. The study is specific to ChatGPT's sampled memory implementation, with a small Prolific sample concentrated in the US/Europe and imperfect annotation proxies. It motivates visibility/provenance and control; it does not establish that Klukai has the same extraction rates or that a particular correction UI causally improves trust.

Kindroid's official memory documentation, **updated 2026-10-28**, describes editable Learned Context, inspection/deprioritization of recalled memories per reply, separate formation/recall switches, and journal suggestions that become recallable only after approval. [Kindroid Memory](https://kindroid.ai/v2/docs/memory/). Replika's current official help describes viewing stored memory and manually adding details. No article date displayed; fetched 2026-11-07. [Replika memory](https://help.replika.com/hc/en-us/articles/37208679176077-How-does-Replika-s-memory-work).

**Design inference:** explicit memory curation is a plausible useful product capability. Vendor docs corroborate that this is an implemented pattern elsewhere, but not that it measurably improves memory accuracy. Their deletions/reset behaviors are inappropriate to transplant into Klukai's sacred-data contract.

## Ranked feature opportunities

### 1. Search our conversations — recommended now

**Scope:** search original stored user/assistant text; support deterministic documented query semantics, speaker filter, optional date bounds; return excerpts with timestamp and stable message ID; open a read-only surrounding-message view anchored to the exact result. Preserve original message content and speaker identity. Make empty results distinct from unavailable backend.

**Acceptance criteria:**

1. A synthetic user with more than 200 messages can find a unique old message beyond the currently loaded transcript. Search does not silently operate only on the client cache.
2. Query, role and date filters combine; displayed date uses the user's local time, while database comparisons use validated timezone-aware instants. Date-bound inclusivity is documented and tested.
3. Query semantics are explicit. If literal phrase/substring search is offered, `%`, `_`, backslash, punctuation and quotes cannot broaden a literal search unexpectedly. If FTS is offered, stemming/stopword behavior must be accurately labeled; a claim of exact phrase search must actually preserve phrase semantics.
4. Results include exact source excerpt, author, full date/time, UUID; original text is never generated or rewritten by an LLM. Selecting a result shows its immediate neighbors in chronological order with the anchor clearly visible.
5. Pagination is stable for equal timestamps and changing result sets. Test timestamp+UUID ties, duplicate textual messages, page boundaries, and a newly arriving chat message.
6. Every search and context/anchor fetch authenticates and scopes SQL by the caller's user ID. A second synthetic user cannot enumerate the first user's message IDs or context.
7. Invalid dates/UUIDs/roles, oversized queries and page limits return bounded validation errors; an unavailable DB returns a retryable error, not an empty archive. No conversation text or search query should be introduced into application logs/telemetry.
8. Flutter covers loading, no matches, retry, role/date clearing, long Unicode content, screen-reader labels, narrow phones and text scaling. Returning from search preserves the live draft and normal chat history/scroll behavior.
9. No API/model calls, local inference, proactive messages, new external requests, or modifications/deletions of original messages occur while searching. Existing cloud-only and sacred-data tests remain green.
10. Use a realistic synthetic history to check query/index behavior and latency; an index alone is not evidence of bounded latency. Avoid widening a transaction-heavy migration without checking the migration ledger and rollback rules.

**Integration points:** existing `/api/messages` boundary in `routes_extras.py`, dedicated query helper and focused route rather than a growing inline handler; current message model in Flutter; new search/context screen reachable from the Chat header; migrations only if needed for indexing. Preserve the existing `GET /api/messages` payload/contract for old clients.

**Risks:** cross-user context lookups, selecting wrong duplicate/timestamp tie, time-zone off-by-one boundaries, unsafe SQL wildcard semantics, excessive match payloads, slow full-table scans, query exposure through access logging, asynchronous response races when the user changes query, and losing live chat draft/scroll state. These are implementation concerns, not reasons to delay the feature indefinitely.

### 2. What she remembers — additive corrections and provenance

**Scope:** show a bounded list of user facts with origin (user statement vs inference), last observed date, original-source link where available, and explicit corrections. Store a correction/override as a new version/event, preserving the original extraction and all chats. A new overlay wins in prompt construction while the old item remains inspectable.

**Existing overlap:** facts/dossier/inside-jokes exist, but there is no inspected general user correction/provenance UI. The currently inspected `fact_store.store` overwrites by key, so directly exposing that function would not deliver additive corrections or historical provenance.

**Acceptance criteria:** correction history append-only; prompt uses latest explicit user correction; background extraction cannot silently overwrite it; “this changed” differs from “the earlier inference was wrong”; missing historical provenance is labeled unknown rather than fabricated; strict per-user scope; corrections can themselves be superseded by a later append; no original chat or episode removed.

**Why second:** direct user control is valuable and supported by current research/product patterns, but it changes the memory interpretation path, requires provenance not guaranteed in existing facts, and invites conflict-resolution/rollback complexity. Search is a useful prerequisite because it supplies source navigation without changing what Klukai believes.

### 3. Saved moments — deliberate bookmarks of original exchanges

**Scope:** an explicit save action on a user-selected message/exchange, a small saved-moments list, optional user-written label, and a link back to the original context. A bookmark is a reference to an immutable source message, not a generated summary or a new claim for the model to believe.

**Existing overlap:** image memories have keep/discard controls, but bounded searches for bookmark/starred/saved-message/pin-message terms across core, Flutter and migrations found no equivalent text-message bookmark implementation. Outfit favorites and a heart icon are unrelated. Reuse the source-context reader from conversation search.

**Evidence:** Telegram's official Saved Messages announcement (2024-01-31) documents stored messages as notes/bookmarks, source-chat views and searchable tags. [Telegram Saved Messages 2.0](https://telegram.org/blog/new-saved-messages-and-9-more/?setln=en). Kindroid's current official memory guide also describes message favorites, but ties them to future memory consolidation; Klukai should not claim that effect unless intentionally implemented. These are product precedents, not measured evidence of user benefit.

**Acceptance criteria:** explicit authenticated save; stable source-message UUID and correct user scope; duplicate-safe repeated save; preserved original role/text/time; list/detail navigation works after reconnect; no copied/rephrased source masquerading as the original; removing a bookmark never deletes its source message. Given the additive-data directive, model bookmark add/remove as events or otherwise confirm bookmark-only metadata semantics without touching chat history. No automatic favorites or provider calls.

**Why search first:** bookmarks help only after the user deliberately saves something. Search immediately helps across existing history and supplies the context-reader component that bookmarks need. A small bookmark view could be less code than full search, but would leave the core retrospective retrieval gap. Bookmarks avoid memory-correction prompt semantics, making them a cleaner later increment than a broad memory manager.

### Examined but not a distinct top-three feature: commitments view

**Scope:** a compact screen for already extracted open promises, their followup dates and completion states, with source context if available. Add explicit “done”, “defer until…”, and “do not remind me” state transitions only with well-defined semantics. No extra automatic nudges.

**Existing overlap:** `GET /api/promises` and resolve endpoint already exist; Flutter source search found no consumer. This is primarily a missing user control surface, not a new reminder engine. Event-kind rows are intentionally excluded from promises and must remain separate.

**Acceptance criteria:** scoped list, real unavailable state instead of fake empty (`open_promises` currently catches DB failures to `[]`), duplicate-safe actions, accurate followup date, no promise scheduled from roleplay/quoted material unless already intentionally accepted, and deferral actually reschedules instead of merely resolving with the existing `'deferred'` sentiment.

**Why not selected:** immediately practical, but less distinctly new than searchable shared history or saved text moments. Research here is mainly indirect (inspectable ongoing context and existing product patterns); no examined source establishes that a commitments dashboard improves adherence or wellbeing. Reuse the existing engine rather than introducing new extraction or proactive behavior.

## Research coverage and caveats

Used native web search/fetch and the actually connected arXiv MCP for search, abstract/metadata, full-text downloads, outlines, targeted full-paper sections and limitations. The arXiv downloads persist in its local paper store. Newer 2026 memory papers were searched; the most relevant current human-data paper was read in detail rather than citing a recent title alone. LongMemEval-V2 was inspected at abstract level and excluded from the recommendation because it evaluates web-agent environment experience, not personal conversation memory.

No Brave, search-mcp, context7 or Docker Playwright server appeared in this session's actual registry. Wikipedia/YouTube were available but unnecessary for load-bearing primary-source findings. No paid CAPTCHA or model calls were made. Root handles the separate Grok/X attempt and should report its actual result/gap. Kindroid initially returned 404 from the obsolete docs host and a timeout with one canonical URL variant; fetching the current URL without trailing slash then returned the full official article. There is no remaining Kindroid coverage gap.

The controller additionally ran one bounded Grok native-X lookup using the existing account runtime with API-key authentication disabled, no subagents and a four-turn maximum. It returned no concrete matching search/transparency feature posts; adjacent memory-quality comments were not used as demand evidence. No relevant X finding supports the recommendation. No CAPTCHA fallback was used.

No user study, live performance test, or production runtime validation was performed by this research agent. A separate synthetic PostgreSQL benchmark accompanies implementation. Numerical benchmark results from 2024 models are context, not predictions for 2026 Haiku/Grok. Vendor documentation proves described feature availability, not outcome efficacy. The top recommendation is an engineering/product judgment based on the verified local gap, preservation requirements, and relatively small integration surface.
