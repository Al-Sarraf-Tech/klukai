# Proposed Klukai UI: a personal terminal aboard Elmo

**Proposed design for owner review, 2026-10-15 Central (October 16 UTC). No implementation or deployment.** Recommend a conversation-first interface: charcoal chrome, readable conversation, pale saved-prose surfaces, restrained character artwork, and limited `#ccff1a` identity accents. The optional style preference remains unanswered; this is a recommendation for owner review.

This design addresses the owner's request for a cleaner, sleeker, lore-friendly desktop and mobile UI. It preserves the existing companion, chat, voice, archive, authorization and durable-job behavior. Ordered GPU image backups are a separate implementation. Approval of this document authorizes preparing its implementation plan; it does not certify tests or deployment.

## Evidence and alternatives

The retained current-app audit binds 118 source/test/HTML/CSS files to frontend revision `4afa6db586365b60b584d8c8395a629bbc308460`. Its claims are source-derived, not live visual observations. Previous 254 VM/109 Chrome cases establish a finite baseline, not this redesign. Physical-device IME, screen-reader and rendered accessibility remain unverified.

The official role-to-asset record establishes Klukai's `#ccff1a`; retained official art shows pale hair, charcoal equipment and purple details. An Elmo personal terminal is our interpretation, not a copied game screen. [Official Klukai](https://gf2exilium.sunborngame.com/main/roleInfo?id=1052), [official Elmo/site](https://gf2exilium.sunborngame.com/main).

Linear's documented quieter shell supports content hierarchy; pinned SillyTavern source supplies inspectable composer/IME/scroll questions. These are finite references, not live-tested equivalents. [Linear refresh](https://linear.app/now/behind-the-latest-design-refresh), [mobile navigation](https://linear.app/changelog/2026-01-22-customize-your-navigation-in-linear-mobile), [SillyTavern pinned composer](https://github.com/SillyTavern/SillyTavern/blob/7e8663cd9c184a550b37238218bdd32c6efc68e9/public/scripts/RossAscends-mods.js).

Alternatives: Companion-first gives more character presence but less reading room and greater motion/focus complexity; journal-first favors browsing but makes conversation less immediate. Conversation-first best follows today's Chat home. This is design judgment, not measured superiority.

## Desktop and mobile behavior

Use available logical width, height and measured text fit, never device names:

| Available width | Proposed composition |
|---|---|
| Below 600 px | One task column; compact header; labelled Chat, Memories, Her POV, Dossier bottom navigation. |
| 600–1279 px | Leading labelled rail, one bounded content lane; supporting context through existing profile/her-day actions. |
| 1280 px+ | Approximately 200 px navigation, 720 px conversation lane, optional 248 px context pane. Gutters/padding give a 1264 px fit budget. Extra width stays whitespace. |

At large text or short landscape height, remove context and reduce columns before squeezing content. If navigation labels cannot fit, a labelled Menu opens a scrollable destination list in the same order. Never shrink scaled text to preserve a breakpoint. [Flutter adaptive guidance](https://docs.flutter.dev/ui/adaptive-responsive/best-practices), [Android adaptive navigation](https://developer.android.com/develop/adaptive-apps/guides/build-adaptive-navigation).

Wide layout:

```text
Navigation | Chat header / readable transcript | Portrait / her day
           |                                  | Mood / bond
           | Voice / draft / Send              | Open Companion
```

Compact layout:

```text
Klukai / connection / More
Conversation / Latest
Voice / draft / Send
Chat / Memories / Her POV / Dossier
```

The context pane reuses fetched state; drawing it adds no request, poll or avatar initialization. On soft-keyboard opening, keep the composer visible and omit bottom navigation temporarily without changing selection. Restore it on close. Preserve `SafeAreaShim`; do not double-apply keyboard/browser insets.

Keep one ChatScreen transport/controller owner and existing route lifetimes. Resize changes presentation only. Do not eagerly mount unvisited destinations or duplicate clients. Returning preserves draft, selection and reading position.

## Visual and character identity

Proposed tokens: canvas `#111318`, surface `#1A1D24`, raised `#242932`; text `#F1F2EC`, secondary `#B8BEC8`, active hint `#959EAC`; paper `#ECECE6` with ink `#22262F`; control boundary `#677385`. Reserve official lime `#CCFF1A` for the active destination and main action, with dark text. Purple remains an artwork/detail accent. Status colors have labels/icons and independent meaning.

Opaque calculations: text/canvas 16.50:1; secondary/surface 9.03:1; hint/surface 6.24:1; ink/paper 12.77:1; dark-on-lime 15.83:1; boundary/raised 3.04:1. These are token calculations, not whole-app certification. Test actual compositing and every state. Replace faint alpha-based instructions, including the current source-derived 1.57:1 composer hint.

Use existing fonts/icons: 16 sp body, about 1.5 line height, readable supporting labels, sentence-case actions; monospace only for short identity metadata. Bound prose around 640 px within the lane. Keep authorized portrait artwork initially. Do not copy publisher assets into the product, bundle a private Commander reference, mirror the facial marking, or automatically adopt a generated sample. Preserve outfit-specific identity and the selected portrait's viewer-right tear.

## Conversation and truthful feedback

Keep selectable native Markdown, local dates, media viewing/export and completed-message voice replay. Bubble limits use lane constraints, not 75% of the full window. Wrap ordinary text/URLs; contain code/table scrolling.

Keep existing send, lock and draft ownership: failed WebSocket write retains the draft and creates no echo; successful submission clears it once. Preserve soft-keyboard Send. Proposed physical Enter sends once, Shift+Enter inserts newline, IME commit never sends, and modal focus takes precedence. No shortcut may intercept ordinary typing. Composer grows to four visible lines with internal scrolling, bounded by usable height.

Preserve microphone permission/epoch cleanup, release/cancel, STT timeout and typed-draft retention; text remains available without voice. Keep existing replay/audio behavior and provider order.

Proposed scroll improvement: initial history and deliberate successful send follow the end. Incoming content follows only while near bottom; scrolling up preserves a message/offset anchor through streaming, image decoding and composer growth. A labelled Latest resumes follow. Transcript navigation keys operate only with transcript focus.

Show known states: Connected describes the socket; Waiting describes silence; actual server thinking/tool/job events supply stages; done.text supplies canonical completion. Replace 7/22-second inferred model-loading claims. Disconnection retains partial text without claiming completion/cancellation. The model defaults status to read while read_receipt is ignored, so never treat that default as verified acknowledgement. Do not invent percentages, backup readiness or provider transitions.

No new Stop, attachment, global-search or provider controls: their contracts were not established. Existing recording/dialog cancellation remains.

## Preserved destinations and behavior

- **Memories:** retain category/month filters, The Thread, ID-keyed entries, authenticated original exports and confirmed discard. Filters scroll independently. Narrow views use list/detail navigation; sufficiently wide inner space permits list/detail and then filters/list/detail. Preserve Back focus/scroll, retry versus empty, and deletion cancellation/failure.
- **Dossier:** retain relationship/timeline, wardrobe locks/reasons, outfit callbacks and her-day refresh; make portrait/timeline/wardrobe keyboard-focusable.
- **Her POV:** replace fixed 460/300 px mobile panels with intrinsic stage/gallery layout. Preserve accepted job ID, one poll, budgets/refusals and explicit retry boundaries. Disposal detaches UI, not accepted server work. Fetch/decode failure never requests another generation.
- **Companion:** preserve desktop new-window/iOS standalone same-window policy, return path, framing, pause/motion/audio, fallback portrait and safe areas; no automatic 3D startup.
- **More:** group existing ambient audio, Companion, read-only Status & usage and Sign out. Preserve verified/unverified/error account states; no checkout/upgrades/new preferences. Sign out retains token revocation, socket disposal and service-worker unregister before redirect. Auth expiry stays authoritative.

## Acceptance and implementation outline

All rows are required planned checks, not passes. Aim for 48 px controls; WCAG's 24 CSS px minimum has exceptions. Require visible dark/pale focus rings, modal focus restoration,320 CSS px reflow, 200% text / 400% zoom and static equivalents under Reduce Motion. [Flutter accessibility](https://docs.flutter.dev/ui/accessibility-and-internationalization/accessibility), [reflow](https://www.w3.org/WAI/WCAG22/Understanding/reflow.html), [focus not obscured](https://www.w3.org/WAI/WCAG22/Understanding/focus-not-obscured-minimum.html), [target size](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html), [motion](https://developer.apple.com/design/human-interface-guidelines/motion).

| Acceptance | Responsible surface / verifier |
|---|---|
| 320/390/600/1024/1280/1440/1920; boundary resize and large text; no duplicate requests | New small presentational shell/layout policy; widget constraints and browser request-count journeys. |
| IME, Send/Shift+Enter, double submission, failed-send draft, recording cancellation | Existing composer/voice owners; counted transport, real keyboard/permission journeys. |
| Older-reading anchor, late image decode, reconnect merge, selectable content | Chat scroll/view and message widgets; browser geometry/event cases. |
| 24–36 months history, correct artifact ID, discard cancel, wardrobe locks | Archive/profile widgets; existing fixtures plus keyboard/modal journeys. |
| Accepted Her POV resize/disposal, gallery errors, Companion window policy | Existing lifecycle/browser tests; zero extra POST/poll/engine initialization. |
|Contrast, actual hit regions, focus, motion, keyboard/notch | Rendered states plus physical-device/assistive-tech verification; emulation alone is insufficient. |

Phases for root's final plan: (1) semantic tokens in `theme/gfl2_colors.dart` and Companion CSS, retaining mood meaning; extend contrast/mood tests. (2) presentational shell plus `chat_view`, `chat_header`, navigation and composer presentation; retain service/controller owners and existing chat/voice/reconnect tests. (3) archive/dossier/Her POV reflow and focus, preserving their services; extend existing screen/lifecycle/browser journeys. Then run the relevant frontend gate/release build with a compliant sandboxed browser.

Open risks: owner visual preference; real-device/accessibility coverage; navigation remounts; misleading legacy statuses; Flutter/Companion drift; artwork rights/fidelity. Ordered GPU backups remain separate backend work, never activated by this UI draft. Root integrates the final spec/plan; no implementation starts here.
