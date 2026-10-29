# Chat polish

Owner goal: “do some extra polish up here and make her great.” This pass improves the daily conversation experience in the existing Flutter HUD. Implementation choices below are the agent's judgment under that request.

## Approach and constraints

Keep the current visual language and single chat state owner. A broad redesign would disrupt an established interface; cosmetic-only changes would leave recovery problems intact. Improve layout, feedback and recovery together. No new dependencies, provider changes, automatic pictures, voice autoplay, or changes to stored memories. No production deployment is part of this checkout change.

## Evidence and acceptance

Source baseline: ca76660. These were source-derived findings, not observations of the owner's private conversations.

| Claim | Baseline evidence | Required behavior and verifier |
|---|---|---|
| composer.space | `ChatComposerBar` puts all controls and text in one fixed row | At 320–430 logical pixels and 200% text, the field has useful width and all controls fit; widget geometry and rendered review. Wide layouts remain compact. |
| composer.send | SEND is enabled for blank text; the field has no explicit accessibility label | Blank/whitespace and active IME composition cannot submit. Typed and programmatic drafts update availability. Picture remains independent. Widget tests. |
| history.recovery | `_loadHistory` shows a six-second snack bar; `_loadOlder` swallows errors | Loading/error/confirmed-empty are distinct. Initial and reconnect failures retain a retry action; loaded messages remain readable. Older-page failure pauses auto requests until explicit retry. Browser widget tests with controlled HTTP. |
| history.order | Reconnect can overlap recent-history requests | Stale recent-history results cannot clear or replace the latest request's state; auth expiry remains authoritative. Browser request-completion-order tests. |
| reply.feedback | `_startWarmupHint` says a local model is loading after seven seconds | Slow-reply copy makes no inferred backend claims; real status, reply, disconnect and error clear the fallback timer. Existing/new browser tests. |

## Verification scope

Run composer widget cases, chat browser cases, static analysis, release build and inspect a rendered fixture. These verify client behavior, not live provider quality or physical-device accessibility. Keep test inputs synthetic and requests isolated from production.


## Claim audit

All five required client claims above are verified by the final test runs recorded in the implementation plan. The responsive pass also corrected large-text compact-header and tab-bar overflows. The history notice is bounded by transcript space; a short viewport can scroll the notice without obscuring SEND. Waiting feedback is replaced by real status and cleared on disconnect. Composer previews were inspected with bundled fonts. This is verification of the changed client behavior and build, not a claim of live-provider testing or physical-device accessibility certification.
