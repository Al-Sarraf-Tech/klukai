# Conversation authority and connection lifecycle

## Problem and intended behavior

Two already-open devices currently retain separate Redis snapshots. The second device can erase the first device's recent context even when messages are sequential. Simultaneous responses can interleave. Delayed extraction and compaction can overwrite newer state. Socket membership changes during an awaited broadcast can throw, and failed last sends can leak tracked tasks.

For the deployed single-process core, each user needs one serialized turn boundary. Fetch current state after entering it, process the complete message/voice exchange, and admit the next turn only after the response is complete. Different users remain independent. Connection initialization uses the same authority; it must not replace a live turn's mood/state. PostgreSQL message history remains append-only.

## Design constraints

- Introduce a small explicit session coordinator or MemoryManager context manager, shared by connect, text/voice turn processing and conditional background state patches. Reclaim idle lock entries safely. Current Docker command runs one uvicorn worker; document that process-local turn ownership requires this invariant. Do not advertise multi-replica safety or introduce distributed locking infrastructure incidentally.
- Fetch the latest Redis session inside admission, not before the lock. If Redis is unavailable, never substitute an old socket-local state and overwrite newer state. A controlled retryable error is preferable to a fabricated successful save. Initialization creates the conversation and session consistently with existing history contracts.
- Add monotonically advancing state revision with a backward-compatible default plus conversation identity. Capture immutable snapshots for background extraction/compaction. A newer admitted turn invalidates an older mood/mission result, including after an aborted/reverted user turn. Use atomic conditional application under the same coordinator; mood broadcast, proactive timers and PostgreSQL mood persistence must not bypass the stale-result guard.
- Compaction uses latest stored state, original summary and exact compacted prefix. Preserve appended newer turns and unrelated mood/mission changes. Skip when the original prefix/summary or conversation no longer matches. Never mutate a stale object and call whole-state save.
- Foreground mission commands and user-turn rollback remain within turn ownership. Preserve public handler signatures where practical; tests must exercise the endpoint/coordinator integration, not just a standalone lock class.
- WS fan-out snapshots membership. Centralize removal and last-device cleanup so failure and endpoint-finally paths agree. Detach the old generation's tasks before any await, then cancel and await them; never cancel the current task itself or a newly connected generation. Retrieve background exceptions. A failed initial connected send must clean up.
- Bound slow socket sends using an explicit conservative timeout. Preserve ordering for each serialized user response and all existing event wire fields. Durable accepted image jobs are addressed separately and must not be newly coupled to WS lifetime.

## Acceptance

Two open devices retain both exchanges sequentially and concurrently; same-user output does not interleave; different users progress independently. Delayed extraction cannot overwrite a newer mood/mission, including a failed subsequent turn. Compaction completing after a new exchange preserves that exchange and newer metadata. Concurrent initialization creates one live session. Redis read/write failures do not silently pass as persisted turns. Connect/remove while a send awaits is safe; last-send failure followed by finally cleans old tasks; reconnect while cleanup awaits preserves new tasks; task exceptions are retrieved. No provider, live account, GPU or production data calls during tests.
