# Clear interaction states and a coherent interface

## Intent

Finish audit UI04–10 after the shared conversation renderer. Use the existing navy/cyan/orange character identity, approved assets and native controls. Essential labels must be legible, controls keyboard-operable, failures distinguishable from empty success, and text chat usable without voice. No new services, artwork, fonts, dependencies or provider work.

## Flutter scope

- Use existing semantic colors (`textMuted` etc.) for essential placeholder, empty-state, timestamp and status text. Verify actual composited colors, not only palette swatches. Normal essential text meets4.5:1; disabled controls may be visually distinct. On orange actions use a dark foreground with sufficient contrast.
- Header disconnect, composer send and voice controls have labels, focus indication, keyboard action and practical44px targets. Reflow dense controls rather than shrinking them below operable size. Preserve appearance/camera navigation.
- Extract speech input into an explicit state owner: idle, requesting permission, recording, transcribing and recoverable error. Block overlapping voice captures; allow text drafting/sending when voice is unavailable or transcribing. Preserve typed drafts when a transcript returns; never replace unrelated typed text to send voice. Support keyboard start/stop and explicit cancellation. Denied permission, empty take, start/stop failure, STT timeout/offline, disposal and late completion are observable and safely reset. Reuse existing recording bridge and STT feedback rather than adding a new backend.
- Her POV gallery, profile statistics/milestones and subscription status distinguish loading, empty success, unavailable/error and retained last-good data. Provide explicit bounded Retry. Authentication expiry keeps existing logout/reauth behavior. A subscription request failure must not look like a verified elite status. Do not retry an image-generation POST when refreshing status/gallery.
- Keep presentation components small where state extraction solves a real problem; do not split solely to meet a line count.

## Stage, login and build source

- One small shared visual vocabulary: existing system/Inter body fonts, mono for compact labels, readable body/label scale, consistent borders/radii/focus. Do not add remote font or icon dependencies. Keep legal credits and all approved 3D controls discoverable; avatar geometry/camera/motion internals are out of scope.
- Allow browser page zoom; correct document language and login label associations. Login errors/status have appropriate live roles. Source-own login under `flutter_app/web/login.html`; copy the existing public checked-in login there, then make build/deploy derive it from source. Remove the temporary `/tmp/login_backup.html` preservation dependency. Verify build-only output includes the current login; do not publish generated web-build during implementation.
- Wake overlay has one clear keyboard-operable action and excludes covered controls from focus. Wake initializes visual/text mode even when AudioContext is absent or resume rejects; show a recoverable audio status instead of stranding the app. Restore meaningful focus after wake. Audio toggles are labeled with state and can retry without requiring a page reload.
- Keep subtitle follow-tail only when reader was near the bottom. A deliberate Latest action returns to the live tail. Completed replies get one polite announcement; tokens do not flood an aria-live region. No forced scrolling over a reader's position.
- One reduced-motion preference owner covers portrait/ambient decorative animations, CSS thinking pulse and mouse parallax. React to preference changes and reset transforms. Preserve manual camera actions and the existing avatar's own reduced-motion contract.
- Portrait unavailable/error text is provider-neutral. Retain last-good frames. Any retry only reads existing portrait status/frames, is bounded/backed off and never triggers unauthorized generation. User retry is discoverable.

## Verification

Use controlled fixtures, no live login/voice/image/history calls. Flutter tests cover keyboard/semantics, actual contrast, state races and auth/error/empty/last-good screens. Stage tests use existing Node and localhost/full-shell fixture where supported, with source hashes. Produce rendered evidence for320x740,390x844,1280x800 and narrow landscape568x320 (also844x390 where practical), normal/200% text, keyboard focus and reduced motion. Show long reply/table/artwork,4000-character input and failure states without clipping or lost actions. Existing camera save/reset, appearance swap and motion tests stay green. Clearly label mock fixtures and any unverified real-device behavior.
