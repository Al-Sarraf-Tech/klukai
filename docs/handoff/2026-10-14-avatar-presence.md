# Saved framing and expressive companion view

The owner requested vertical positioning that stays where the user leaves it,
click reactions, and expressive mouth/hand movement during speech, with direct
production deployment. This extends the existing full-body, 360-degree view.

## Behavior

- One-pointer horizontal dragging rotates; vertical dragging shifts framing
  without tilting the camera. Pinch and wheel zoom remain available.
- View offsets are stored with the existing browser preferences and survive
  reloads, mode changes, resizing and appearance changes. Reset restores the
  full-body view without changing voice, music or appearance preferences.
- Tapping her actual model cycles a salute, acknowledgment and pleased reaction.
  Dragging, holding, background taps and pinches do not trigger reactions. A
  cooldown avoids repeated interruptions; the underlying mood/thinking state
  resumes afterwards. Speech takes priority over a reaction.
- Speech playback's existing Web Audio meter drives lip opening and the talking
  clip, with restrained wrist/forearm emphasis. The mouth closes during silence;
  reduced motion keeps mouth feedback while suppressing added body gestures.
- Up/down and Interact buttons provide keyboard access. Landscape uses one
  compact control row to keep the body clear of the dock.

The six installed models have a blink morph but no authored mouth-open target.
The viewer adds a runtime morph to their known face geometry, calibrated in
decoded bind-pose metres, while retaining the existing mouth cavity and closed
resting face. Other models with a recognized authored mouth target use that
target. Model assets, source attribution, locks and deployed GLB bytes are
unchanged. This is loudness-driven lip movement, not phoneme-level lip sync.

## Verification

`AVATAR_TEST_GLB=/home/jalsarraf/git/klukai/assets/build/klukai.glb make test-js`
passed all 208 tests (101 companion, 107 avatar). The explicit asset selector
tests the actual deployed GLB. Without it, two pre-existing checks fail against
the stale private `assets/build/dev/klukai.glb` fixture (neck and stomach
surfaces); those same failures reproduce on unchanged main. The deployed asset
passes both checks. No private development model was rewritten.

The browser harnesses use the real renderer, model geometry, mouse/touch events,
Web Audio playback and app wiring, with synthetic HTTP/WebSocket responses.
They do not call a model provider or the GPU services. Camera tests cover
projected vertical displacement with unchanged pitch, persistent rotation and
zoom, reload/reset, appearance switches and control isolation. Presence tests
cover all three reactions, gesture rejection, actual deformed mouth vertices,
hand travel, silence release, and the distinct GARF appearance.

Independent review corrected staggered pinch release, replacing one finger
while another remains held, adoption of a reaction
clip by a new state, and obsolete appearance instances overwriting saved
framing. Active-view ownership and a commit-time camera snapshot protect
appearance replacement. Browser snapshots at 320×740 and 844×390 also verify
the controls and composer remain on screen.

Desktop (1280×800) and phone (390×844) browser checks passed with no JavaScript
errors. A vertical drag moved the projected model by 89.20 and 88.87 pixels,
respectively, with no camera-pitch change. A delayed appearance load preserved
camera adjustments made while loading. Real Web Audio playback produced about
6.9 mm of visible mouth displacement and 13.8–17.4 cm of hand travel; silence
returned mouth displacement to exactly zero. The actual GARF variant also
showed 7 mm of lip displacement. These are measured local fixture checks, not
a new voice-model benchmark.

## Deployment

Only the standalone companion assets need publication: the vendor bundle,
`avatar3d.js`, `app.js`, `companion.css`, and `companion.html`. Their dependency
URLs carry content hashes, including both avatar imports and the vendor import.
Copy dependencies first and HTML last into Amarillo's bind-mounted `web-build`.
No core rebuild, Docker restart, GPU operation or voice configuration change is
required.

The prior five deployed files and SHA-256 manifest are saved privately in
`/home/jalsarraf/.local/share/klukai/deploy-backups/avatar-presence-20261014T155603Z/`.
Rollback restores those files in the same dependency-first order.

Published commit `01e2678` at 2026-10-14 16:07 UTC. All five installed files
match their committed sources. The four public JS/CSS responses also match
byte-for-byte; the public HTML contains the new dependency hashes plus
Cloudflare's existing injected scripts. Cache identifiers are:

| Asset | SHA-256 prefix |
| --- | --- |
| Vendor bundle | `8ae676317440` |
| Avatar | `0453d86fa728` |
| App | `2b40b27bec49` |
| CSS | `4d533928c458` |
| Origin HTML | `76f6767dc211` |

An authenticated Brave check against
`https://klukai.appnest.cc/app/companion.html` loaded the real production model,
moved her up, reloaded the saved view, reset it, and displayed the salute.
No page errors occurred. The temporary `claude` test session was revoked;
no chat or generation request was sent. The public phone gesture regression
also passed with no errors, including staggered lift and finger replacement.
Core health remained healthy throughout this static deployment.

Screenshots, measurements, origin/public manifests and live results are preserved
under `/home/jalsarraf/Documents/Codex/2026-10-14/klukai-avatar-presence/`.
