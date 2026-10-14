# Expanded zoom and everyday/playful animation library

The owner requested more zoom and animation sources/wiring, then chose everyday
and playful movement. This release adds eight CC0 motions to the existing
avatar, a categorized picker, and a wider camera range. It is a static companion
frontend release; backend, private models, GPU, voice and provider settings are
unchanged.

## Behavior

- Zoom distance now ranges from 0.2 to 3 times the normal framing, previously
  0.6 to 1.8. Buttons, scroll and pinch use the same limits. Vertical movement
  brings the face into view at close zoom; saved view, full-body Reset view,
  resize and unlimited horizontal rotation remain available.
- The camera far plane grows only when needed, preventing disappearance at
  wide zoom on short landscape screens with a large dock.
- The **⋯** button beside **Interact** opens **Animations**, grouped into
  Everyday and Playful. Sixteen choices include eight original clips and eight
  new motions: Greeting, Nod, Confused, Bow, Charleston, Groove, Cheer and
  Listening. The original nervous clip is now exposed as Shy fidget.
- Tapping her or Interact cycles accepted reactions. The two dances require an
  explicit choice. Listening joins the existing five passive gestures; the
  12–24-second quiet interval and speech, thinking, sleep, camera interaction,
  visibility and reduced-motion guards still apply. Each one-shot returns to
  the current mood, and speech immediately takes priority.

## Source and implementation

[Research and source comparison](../research/2026-10-14-avatar-animation-sources.md)
compares Mesh2Motion, Quaternius, Mixamo, 100STYLE, CMU and Kenney, using official
documentation, pinned downloads and native/Brave/SearXNG discovery. The selected
Mesh2Motion add-on is pinned by commit and SHA256. Its original CC0 notice and
rebuild instructions accompany the pack in
`flutter_app/web/companion/motions/`. No account, upload or purchase was needed.

The 601,945-byte JSON contains 67 source nodes and eight clips, with source
provenance embedded. It contains no private target geometry, textures or bind
pose. `motion-library.js` retargets source world-space rotation deltas onto a
stripped copy of the current authenticated appearance's rig, preserving target
bone lengths/rest axes. It scales hip travel, limits horizontal movement to
12 cm and corrects minimum foot contact against each model's rest floor.
This is not collision simulation or foot IK; future motions need visual review.

The shared source request has an eight-second timeout and independent caller
abort handling. A replaced appearance stops waiting without cancelling another
caller's download. Failed or malformed downloads can retry on a later appearance
load. The optional pack can delay initial model readiness while loading, but
failure retains the original embedded motions and text chat. Retargeted clips
are cached per rig in memory. No public target-derived asset is generated.

The pinned Three.js vendor bundle now includes the animation-track constructors
needed by this module. No package was installed or upgraded. Existing build
outputs and authenticated GLBs were not rewritten.

## Verification

`AVATAR_TEST_GLB=/home/jalsarraf/git/klukai/assets/build/klukai.glb make test-js`
passed **233 tests**: 101 companion and 132 avatar, zero failures or skips.
This uses the actual deployed fixture; the unrelated stale development GLB
was not changed. Syntax compilation and `git diff --check` also passed.

New tests cover source-only extraction, discrete channels, rest-orientation and
hip scaling, unchanged live bones, real default/GARF foot contact at 30 Hz,
load retry/abort, selection availability and exclusion of dances from automatic
reaction cycling. Camera tests cover endpoint projection and the dynamic far
plane.

Real browser checks passed on desktop 1280×800 and phone 390×844 with the actual
default and GARF rigs. All eight motions changed rendered poses, completed,
restored idle, yielded to speech and disposed cleanly. Reduced motion suppressed
passive playback. Malformed-pack fallback/retry and superseded-load recovery
passed, with no page errors. Representative rendered poses were inspected.

Zoom browser checks passed for buttons, wheel and touch pinch, 0.2/3 bounds,
saved view after reload/resize/appearance changes, and full-body reset. At the
568×320 wide endpoint the camera used a 48 m far plane and retained 8,511 visible
mesh samples. Layout checks passed at 1280×800, 390×844, 320×740, 844×390 and
568×320. Picker bounds, scrolling, 44-pixel action targets and Escape dismissal
passed on desktop, phone and short landscape.

Evidence, screenshots, source hashes and test logs are under
`/home/jalsarraf/Documents/Codex/2026-10-14/klukai-zoom-motion-library/`.

## Publication and rollback

Publish the motion JSON/license/readme, vendor, motion module, avatar, app and
CSS before HTML, using fresh dependency hashes. Replace only these static files
in `web-build` atomically. No container restart or Flutter/core build is needed.

The prior five live files and SHA256 manifest were preserved under
`/home/jalsarraf/.local/share/klukai/deploy-backups/zoom-motion-library-20261014T164511Z/`.
Rollback restores those five files, dependencies first and HTML last. The newly
introduced module and motion directory can remain unused or be removed after
the old HTML is restored. Do not alter unrelated assets.

Published commit `c9fbdc4` at **2026-10-14 16:58:59 UTC**. All nine installed
files match their committed source bytes. Public JS, JSON, CSS, license and
README responses returned HTTP 200 and matched SHA256; public HTML contains
the new dependency hashes plus the existing Cloudflare injection. Cache
prefixes are motion pack `04911b8d948b`, motion module `6c7c0d59a925`, vendor
`ba766fd1f3da`, avatar `fcbb2d7504d9`, app `3fe2d399503a` and CSS `4ee5d7b6e499`.

An authenticated Brave check against
`https://klukai.appnest.cc/app/companion.html` loaded the real model, verified
all 16 animation choices, selected Greeting, reached the 0.2 close zoom limit,
reloaded a saved view, reset to full-body framing and confirmed the lowered
controls. There were zero page errors. The temporary test session was revoked;
no chat or generation request was sent.

Core remained healthy on image
`sha256:4f68e55053d6a0190acf460bc5a85b0f2db6e3afef8d0dfb8a6e11962ef00ff5`
with its unchanged start time `2026-10-14T15:31:33.685247618Z`. Deployment,
public-file, live-browser and health evidence is saved in the evidence directory
above. The frontend release required no restart.
