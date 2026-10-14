# Local GPU voice and Sonnet primary

The owner requested production deployment of the tested local XTTS voice and
Claude Sonnet at medium thinking effort as primary, with Dominus secondary.
After being told API use incurs charges, the owner explicitly said “Override
it” to the no-spending restriction for this integration. Use the existing
Anthropic key, never a new account or purchase. This supersedes the older
local-only chat rule; it does not authorize unrelated spending.

## Chat

Interactive authenticated chat and the existing tool loop use the official
Anthropic Messages API, model `claude-sonnet-5-5`, adaptive thinking and medium
effort. Native tool calls execute through Klukai's existing permissions.
Background extraction remains local. API failure before visible text falls
back to the existing Dominus selection, including gaming behavior. Never join
two providers' partial answers; persist the actual responding model. Defaults
remain local-only unless explicitly enabled. Do not log keys or thinking.

## Voice

Use a separate native Amarillo service on `100.111.198.19:8301`, authenticated
with the existing voice bearer token. Load the checksum-verified XTTS v2
snapshot lazily, condition on CPU, synthesize FP16 on Arc A380, and encode at
24 kHz. Adopt the selected source excerpt 6.536–13.402 seconds in a new local
release lock; preserve all Dominus artifacts.

Supervise a killable worker with a 600-second synthesis ceiling, 68°C thermal
guard, 300-second idle release, 8 GiB memory limit, no swap, two Torch threads,
and 200% CPU quota. Keep the existing Granite service running. Health must
not load a model. Cancellation, deadline, and thermal failure release the
worker and do not claim GPU idle while native inference continues.

Local `/api/tts` returns an authenticated, ownership-scoped asynchronous job.
Poll and cancel endpoints hide foreign IDs as 404. Bound global admission,
allow at most two outstanding jobs per user, serialize inference, and bound
result retention. Preserve legacy synchronous Dominus TTS. Browser/Flutter
playback waits for queued results and cancels abandoned work. Keep complete
reply chunk ordering. Separate `VOICE_STT_URL` so the TTS switch does not
reroute transcription to an unsupported endpoint.

## Production acceptance

Run focused provider/lease/queue/cancellation tests plus existing regressions,
build core and web, and test authenticated production chat and speech. Prove
the live model and effort, 24 kHz nonempty audio, cancellation/unload, and
unchanged Granite service. Dominus may remain unavailable while shut down;
test fallback logic with mocks and report its live availability separately.
Commit conventionally and sync only to the Amarillo git server. Rollback uses
the previous core image/web files and explicit backend environment switches.
