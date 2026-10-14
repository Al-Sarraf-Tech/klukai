# Lower controls and expanded avatar animation

The owner requested a lower menu and more interactive/passive animation after
the avatar-presence deployment. This is a static companion frontend update.

## Behavior

- The subtitle now sits above the camera/interaction controls. This lowers the
  controls by about 50 pixels on desktop and portrait phones, while keeping the
  chat composer within reach. Short landscape screens hide the hint and reserve
  two reply lines so growing text cannot cover her feet; controls share one row
  where width permits.
- Model taps and **Interact** cycle seven authored reactions: salute,
  acknowledgment, pleased, thoughtful, dismissive, bashful, and playful.
  The current mood resumes after each reaction. Speech preempts reactions;
  drags, held pointers, pinches, and background taps do not trigger them.
- Five passive gestures add an acknowledgment, pleased movement, thoughtful
  glance, weight shift, and ready-posture adjustment. They start after 12–24
  seconds of uninterrupted idle time, never repeat consecutively, and crossfade
  back into the current state. Speech, thinking, sleep, interaction, camera
  movement, reduced motion, and hidden/paused views interrupt or suppress them.
  Returning to the page starts a fresh quiet interval, without replaying missed
  gestures.

The existing model clips supply these movements. Model files, voice, image
generation, provider selection, and GPU services are unchanged.

## Verification and publication

`AVATAR_TEST_GLB=/home/jalsarraf/git/klukai/assets/build/klukai.glb make test-js`
passed all 224 tests (101 companion, 123 avatar), using the actual deployed
model. Python harness compilation and `git diff --check` also passed.

The focused scheduler tests cover reaction availability, both ends of the idle
delay, no consecutive repetitions, priority interruption, suspension, current
mood restoration, and held fingers after partial pinch release. Browser checks
use actual rendered rig motion, clips, and input events with local API fixtures.

Desktop (1280×800) and phone (390×844) checks passed for all seven reactions,
passive variation, real-time automatic triggering, speech/reduced-motion/state
priority, pinch recovery, and appearance disposal. Real Web Audio still drives
mouth and hand motion and releases the mouth to its closed resting pose.
No page errors occurred.

Layout checks also passed at 320×740, 844×390, and 568×320. The menu sits
50 pixels lower, controls remain reachable, and her full body clears the
top bar and dock with both empty and long replies. The short-screen camera now
honors measured dock coverage instead of capping it at 45% of the screen;
normal framing remains unchanged. Independent review also removed an obsolete
animation-listener cleanup reference that would have broken appearance disposal.

Only `companion/avatar3d.js`, `companion/app.js`, `companion/companion.css`, and
`companion.html` need publication, in that dependency order, with fresh content
hashes in each importing file. The existing vendor bundle is unchanged. Static
files are atomically replaced in Amarillo's bind-mounted `web-build`; no core
rebuild, restart, or GPU operation is needed.

The previous five companion assets and SHA-256 manifest were preserved in
`/home/jalsarraf/.local/share/klukai/deploy-backups/avatar-animation-expansion-20261014T161800Z/`.
Rollback restores those files, dependencies first and HTML last.

Release evidence belongs under
`/home/jalsarraf/Documents/Codex/2026-10-14/klukai-avatar-animation-expansion/`.
