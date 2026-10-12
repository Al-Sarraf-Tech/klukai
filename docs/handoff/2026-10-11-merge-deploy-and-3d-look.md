# Handoff: Fable branches merged and deployed; 3D look goal paused after research

Date: 2026-10-11. For Claude, Codex and the owner. It covers two pieces of work from one session:

1. **Done and live.** The two Fable cloud branches were merged, deployed to amarillo and
   dominus-nobara, and pushed.
2. **Paused.** The "make the 3D model look perfect" goal stopped after research and before any
   implementation. Nothing from it is in the repo or in production.

## 1. Merge and deploy (done)

### What is on `main`

`origin/main` is `1bcee2d`. The amarillo git server holds the same commit.

| Commit | What |
|---|---|
| `12155fc` | merge `claude/klukai-standby-rqu97r`: standby handling, the stage, the gateway, proactive messages, the app |
| `9afbfe0` | merge `claude/voice-fixes-kso5o9`: push-to-talk hardening and the findings backlog (8 conflicts resolved) |
| `881a38b` | fix(core): a failed mid-turn send ends the WS loop instead of raising an ASGI traceback |
| `1bcee2d` | chore(web-build): index.html points at the deployed bootstrap hash |

The `personal` remote (`jalsarraf0/klukai`) is 74+ commits behind. It was not pushed.

How the conflicts were resolved is in the `9afbfe0` commit message. In short:

- **Standby's side won** in `chat.py`, `routes.py` and `logic.js`, because it is a superset.
- **`/api/stt`:** standby's code, plus a 502 for a body that is not a JSON object.
- **Voice service:** standby's temp-file handling, plus `MAX_STT_AUDIO_B64`.
- **`audio_recorder.js`:** the voice branch's generation counter, plus standby's error settling.
  Four node tests cover it.

### Deployed

- **amarillo, `companion-core` and `companion-events-bridge`:** rebuilt and recreated.
- **amarillo, PWA:** `scripts/deploy-web.sh`. The served `/app/js/audio_recorder.js` and
  `/app/companion/*.js` match the repo, both locally and through `klukai.appnest.cc`.
- **dominus-nobara, `lmstudio-compat` and `companion-voice`:** rebuilt and recreated with explicit
  service names. `llama-router` and `comfyui` were not touched.
  - Source files went into `/mnt/nvmer0/services/ai-stack/source/klukai`.
  - The compose contract was verified before recreating.
- **dominus-nobara, Docker drop-in:** `/etc/systemd/system/docker.service.d/20-tailscale-address.conf`
  installed with `daemon-reload` only. Docker was **not** restarted; the change applies on its next start.

Verification:

- Live e2e (`scripts/e2e_live.py`, user `claude`): 11/11, run twice.
- 0 tracebacks in core after the fix.
- Gateway health `ok`.
- `100.107.121.5:1234`, `:8301` and `:8390` listening.
- Publish-repair dry run returned `noop-healthy`.
- Gateway `mem_limit` 512m and `pids_limit` 256 are applied.

### Behaviour change: the gateway now enforces its bearer token

Model load/unload, `/api/v1/gpu/lease/*` and `/api/v1/comfy/*` now return 401 without the
`LM_STUDIO_TOKEN` bearer, even from the tailnet. These stay token-free: `/v1/*`,
`GET /api/v0/models` and `GET /api/v1/models`.

Audited callers:

- **companion-core:** sends the bearer everywhere. It was checked live, and the call got past auth.
- **shoppingmcp-app-1:** the only other live client. It uses `/v1/chat/completions` only.
- **aichat GPU-TTL unload:** points at the dead Windows `dominus` (`100.78.39.76`), so it is unaffected.
- **rabbitmq-ops lease watcher:** reads the marker file, not the gateway.

A new tool that needs load/unload or a lease must send the bearer.

### Rollback

- **amarillo:** `companion-companion-core:rollback-pre-standby-voice-20261010` and
  `companion-events-bridge:rollback-pre-standby-voice-20261010`. Re-tag and `docker compose up -d`.
- **dominus:** `dominus-companion-voice:rollback-pre-20261010` and
  `dominus-lmstudio-compat:rollback-pre-20261010`.
  - Prior source files and the old `/etc` drop-in are in
    `/mnt/nvmer0/services/ai-stack/backups/klukai-pre-standby-voice-20261010/`, with `SHA256SUMS`.
  - Restore the files, re-tag the images to `:2026-08-01`, then run `docker compose --env-file … up -d
    --no-build --no-deps --force-recreate lmstudio-compat companion-voice`.

### Open items from the merge

- **CI result on the push is unchecked.** `gh` is not logged in on amarillo; check the Actions tab.
- **Voice/UI backlog.** `docs/handoff/2026-10-11-voice-ui-findings-backlog.md` has a
  "Status after merge" section.
  - SJ-04, SJ-11/UI-05, UI-01, VP-10 and VP-18 were fixed later the same day (see §3).
  - The other ~170 items are unverified.
- **tgbot suite not run.** It needs `python-telegram-bot`, which is not installed on amarillo.
  Neither branch touched tgbot.
- **Deploy notice wording.** `Makefile` line ~140 still says "transcription disabled". That is VP-18:
  Whisper STT on :8301 is live.

## 2. Goal: make the 3D model look perfect (research; shipped later the same day, see §3)

The owner asked for 3 research agents (with deep research) and 2 implementation agents, run fully
autonomous. State file: `.claude/goal-3d-model-look-perfect.md` (gitignored).

- **Research:** done. All three agents hit the session usage limit at about 23:30 CDT on
  2026-10-10. They were resumed and wrote their reports on 2026-10-11, when the owner asked to wrap
  up.
- **Implementation:** not started. No repo files changed and nothing was deployed.
- **Where the reports live:** `.claude/` is gitignored. The reports are only on **amarillo's disk**,
  in `~/git/klukai/.claude/research-3d-look-20261011/` (43 MB, with game-derived renders, so they
  must not be committed). They do not reach dominus-nobara or the git server. Work on this goal
  from amarillo, or copy the folder over Tailscale.

### Baseline (what she looks like today)

- **Renders:**
  - `assets/build/shots/*.png` (phone and desktop).
  - The scratchpad 12-angle sheet, `baseline-sheet.jpg`. It sits in a /tmp scratchpad that may be
    gone; regenerate it with `cd tools/avatar && python3 debug/views.py OUT`.
- **Problems seen:**
  - No outline. The build drops the game's outline shells, so she reads as flat 3D, not anime.
  - Hair is flat and streaky, with no highlight band. The twin tails read as sheets.
  - Flat two-light toon rig, and no contact shadow: she floats.
  - Probable skin gap at the lower back, between the jacket hem and the shorts.
  - The chest band and straps read as flat bright white or chrome; check the atlas assignment.
    The shorts read metallic.
- **Current numbers:** 7 draw calls, 1.41 MB GLB, 679 KB vendored three bundle, antialias off on iOS.
  `tools/avatar` `npm test` passes 21/21.

### Facts that matter for whoever implements it

- **Building is a live deploy.** `assets/build` is bind-mounted into core as `/avatar`. Running
  `node build.mjs` writes `assets/build/klukai.glb`, and that file is served at once.
  - Develop with `AVATAR_OUT=assets/build/dev/klukai.glb node build.mjs`. That path is gitignored and
    not served.
  - Inspect a dev build with `python3 debug/views.py OUT /assets/build/dev/klukai.glb`.
- **Model caching is already fine.** `/api/avatar/model` sends `Cache-Control: private, no-cache`
  plus an ETag, and `net.js` fetches with `no-store`, so a rebuilt GLB reaches devices on the next
  open. The README's "max-age=86400" paragraph is stale; fix it when touching the README.
- **Viewer edits are not live until deployed.** Changes in `flutter_app/web/companion/avatar3d.js`
  ship only after `scripts/deploy-web.sh` copies them into `web-build/`.
  - That script deletes stale `web-build/companion/vendor/*.js`. This is fine: the stage imports
    only `three-avatar.min.js`.
- **GLB positions are quantized** (the meshopt `scene` volume, about 0.78 m per unit). Shader maths
  on vertex position (face-sphere normals, hair ring latitude) must use those units, not metres
  (R1 §0).
- **Keep these constraints:**
  - No `polygonOffset` (the hollow look on Metal).
  - Vendored three subset: `node vendor.mjs` regenerates the exports from `THREE.*` uses, and a test
    checks them.
  - The GLB is game-ripped. Never commit `assets/source-models`, `assets/textures` or
    `assets/build`, and serve the GLB only through the authenticated API.
  - amarillo thermal limit 70 C. Renders run on SwiftShader, so they are CPU-heavy.

### Planned split (not started)

- **I1, build pipeline:** `tools/avatar/{build.mjs,avatar.config.mjs,lib/*}` plus a new
  `tools/avatar/test/build-*.test.mjs`. Covers atlas fixes, outline mesh, texture sizes, gap fix.
- **I2, viewer:** `flutter_app/web/companion/avatar3d.js`, `vendor.mjs` and the vendored bundle,
  `tools/avatar/test/avatar.test.mjs`, and the test pages. Covers outline shader, toon ramps, hair
  highlight, rim, lights, contact shadow, AA, motion.
- **Lead:** integration, the visual loop (views.py and shot.py, dev GLB first), then the live build,
  deploy-web, e2e, commit, push.

### Research findings

There are three reports, each with a ranked executive summary, evidence and code-level notes:

- `R1-rendering.md` (rendering);
- `R2-assets.md` (asset fidelity);
- `R3-presentation.md` (presentation and motion).

They live in `.claude/research-3d-look-20261011/`, with the prototypes, renders and scripts beside
them. Every render is SwiftShader; nothing was checked on a real iPhone. Brave/search-mcp weren't
connected, so web sources came from built-in search, iopwiki, a Steam Workshop rip of the game
model, and Grok-on-X. Single-source claims are marked in the reports.

#### Owner decision first: which outfit?

- **The mesh is her default outfit "Blazing Star" (SSR01 atlases), not the dorm outfit** (R2 §E1).
  The dorm outfit is a grey tunic-dress, black shorts, brown thigh-highs and purple slides. Its
  texture `c_ClukayDorm_slg_cloth_d.png` exists, but its mesh is not in the OBJ.
- Her hair is one high ponytail plus side locks, not twin tails.
- The plan below assumes the default outfit. Fix the README's "dorm-kit" wording.

#### Asset fixes (R2), build side

1. **Shorts use the hair atlas.** They are 2×441 faces inside `Klukai_Hair` and render as
   light-blue streaks. Verified fix: give them a black fabric source. For a proper fix, let an
   `atlasOverride` carry its own `role` and `doubleSided` (`build.mjs` ~L141–144), or allow a
   flat-colour part.
2. **The lower-back skin is an atlas bug.** Island #284, the hem band, picks cloth2's skin region.
   The fix is an override to cloth1. Verified.
3. **Seven prototype `atlasOverrides`** for the chest band outer, hem band, hood/collar, zipper
   tapes, torso straps, hip bag and cat pouch. The exact entries are in R2 §C1; the tested config
   is `r2/build/cfg-fix.mjs`, and the before/after is `r2/cmp/fix_vs_ref_full.jpg`.
   - The existing cap-crown override also needs `box: [-0.2,1.40,-0.2, 0.2,1.6,0.2]`. Without it,
     it catches about 10 glove/arm islands.
4. **Still to isolate:**
   - the chest band inner layers 81/102 and the diagonal sling (they read white; should be
     charcoal);
   - the shoes (purple; should be black with neon-green soles);
   - the collar goggle rims.

   Root cause: `lib/atlas-assign.mjs` rewards smoothness, but on GFL2 atlases the correct region is
   often the detailed one. Log islands with a score ratio < 1.5× as ambiguous in `--debug`.
5. **Unused maps:**
   - `_rmo`: R = roughness, G = metal mask, B = baked AO.
   - `_n`: a normal map.
   - Ship AO (R8 webp or packed) for toon creases, and the metal mask for highlights on hardware
     only. There are no ramp, ILM, SDF or hair-mask textures, and no alpha cards.
6. **Missing geometry:** the cap goggles, the blue cross hairclips, and the glossy black bodysuit
   under an open jacket.
   - The dropped `Klukai_Cloth_1_(Astral_Luminous)` group is the same jacket, open at the chest.
     Swapping it in needs a UV remap of the bust islands (medium effort, large gain).
   - Alternatively, source a cleaner rip.
7. **Texture budget:** raise only hair to 2048 (+16 MB, about 40 MB total). Leave the rest as is.
8. **`Outline` is an 80-face eyelid strip, not an outline shell** (R1 §0 and R2 §E4). Outlines must
   be generated.

#### Rendering (R1), viewer side

The prototype is `research/proto/companion/avatar3d.js`, with flags
`fx=light,shade,rim,face,ring,ao,outline`. Before/after: `R1-compare.jpg`.

1. **Light and ramp maths.** The ramp is read as linear and the fill is too strong, so shadows are
   only about 14% darker (about 5% on the face). Use 2 bands with a `smoothstep`/`fwidth` edge,
   tinted shadow colour per role, hemisphere 1.75 → 1.0, and a key at about 1.0× albedo. Costs
   0 draw calls.
2. **Inverted-hull outline.** A second `SkinnedMesh` sharing geometry and skeleton, with
   `MeshBasicMaterial` + `onBeforeCompile` so skinning and the blink morph apply. Constant pixel
   width, a view-space z push (not `polygonOffset`), texture-tinted colour, and smoothed normals
   baked in the build. Draw calls go 7 → 12, measured.
3. **Feed `_rmo` AO into the toon threshold** for painted crease shadows.
4. **Face:** blend the normals toward a sphere before skinning, and use a high-key face ramp. An
   offline SDF face-shadow map is phase 2.
5. **Rim:** stepped, key-side only, tinted by albedo. This replaces the uniform white fresnel.
6. **Hair angel ring:** a latitude band on a head sphere that slides with view elevation. Don't use
   Kajiya-Kay: it streaks on straight hair.
7. **Colour:** keep `NoToneMapping` and sRGB output, with ramps and tints in linear. Never ACES or
   AgX.
8. **AA:** turn MSAA back on for iOS (`antialias: true`) and keep the DPR caps. No post-process AA.

#### Presentation and motion (R3)

1. **The head is not skinned to the head bone.** It has zero weights in the FBX and the GLB, so
   the face, cap and bangs ride on the neck. Every clip's head motion (8–55°, about 28° in talking)
   is lost, and the `setSpeaking` head tilt does nothing.
   - Fix in the build: add the head bone to the skin and move neck weight to it over
     y 1.27–1.31 m.
   - R3's in-page emulation of the fix rendered the same as today. That is probably an emulation
     bug, so verify with a real build.
2. **The canvas has a hard edge on the stage** (thighs on phone, torso on desktop). Fix with a CSS
   mask fade, a full-height `.model` behind the dock, and framing insets.
3. **Look-at:** damped, clamped neck/head toward the camera (and the pointer on desktop), with
   small saccades (ChatVRM constants). She faces about 9° off in idle today.
4. **Lighting for the real dark stage.** The current shots use a bright test backdrop. Use a warmer
   front-left key, less fill, and a directional rim tinted by the mood colour app.js already sets.
5. **Stiff arms:** idle-only additive offset, about +6° outward and +10° more elbow bend.
6. **Life layers:** breathing, speech-driven nods, blinks on gaze shifts and phrase ends,
   occasional fidget clips, and hysteresis on mood changes.
7. **Hair motion:** there are no hair bones; the hair is skinned to the spine, arms and even
   thighs (82% at the tips).
   - Proper fix: build-time bone chains plus a spring simulation adapted from three-vrm.
   - Interim: a vertex-shader sway.
8. **Camera:** 22° FOV for upper framing on phone, eased framing changes, a slight push-in while
   talking, slow drift, and orbit springing back to the front.
9. **Contact shadow:** a blob quad, only for full framing and sleep. Not shadow maps.
10. **GLB facts:** 52 Mixamo bones, 51 skin joints (Head is the exception), 2 meshes sharing one
    skin, and the only morph is `blink`. The clip list with durations is in R3.

#### Merged plan, in order

| # | Task | Owner | Notes |
|---|---|---|---|
| 1 | Owner picks the outfit (default assumed) | owner | the dorm mesh would have to be sourced |
| 2 | Head bone skinning | I1 | biggest motion win; test with a real build, check talking and look-at |
| 3 | Atlas overrides C1 + shorts C2 + cap-crown box; isolate band/sling/shoes | I1 | port `r2/build/cfg-fix.mjs`; add tests asserting island→atlas for the known ids |
| 4 | Ship AO (+ metal mask) and smoothed outline normals; hair 2048 | I1 | stay inside the budget; check the iOS memory estimate |
| 5 | Light/ramp maths, face normals, stepped rim, angel ring, AO in the ramp | I2 | port from `research/proto/companion/avatar3d.js` |
| 6 | Inverted-hull outline (+5 draw calls), MSAA on iOS | I2 | regenerate the vendor bundle (`node vendor.mjs`); keep the polygonOffset test |
| 7 | Look-at, breathing, arm offset, talk nods, camera FOV/drift, contact shadow | I2 | after #2 lands |
| 8 | Stage CSS edge fade and lighting tinted by mood | lead | companion.css/app.js; node tests |
| 9 | Open-jacket swap (Astral Luminous jacket + UV remap) or a better rip | I1 | optional, large fidelity gain |
| 10 | Hair spring bones | I1 + I2 | optional; vertex sway as the interim |

### How to resume

1. Read `.claude/goal-3d-model-look-perfect.md` and the research reports. Copies are in
   `.claude/research-3d-look-20261011/`, which is gitignored and durable. The /tmp scratchpad
   originals may be gone.
2. Turn the ranked findings into tasks for I1 and I2, keeping their files disjoint.
3. Each implementer works against the dev GLB and its own renders. After each step the lead
   compares 12-angle sheets with the baseline.
4. When it looks right:
   - Run the live build (`node build.mjs`) and `scripts/deploy-web.sh`.
   - Run `npm test` in `tools/avatar`, `make test-js`, and `python3 scripts/e2e_live.py`.
   - Commit, sync to amarillo, push.

## 3. Update, 2026-10-11 evening: the 3D look shipped

Resumed with "finish up the work". Two implementers (I1 build, I2 viewer) worked from the
research above on disjoint files; the lead integrated, reviewed, deployed and committed.
Branch `feat/3d-look`, fast-forwarded into `main` and synced to the amarillo git server.
**Not pushed to GitHub** (`origin`/`personal`): do that on request.

### Live now (amarillo)

- **GLB** `assets/build/klukai.glb` sha256 `cee5863e…` (1.97 MB, 8 primitives, 67 joints):
  - Head is a real joint, so every clip's head motion shows.
  - Atlas fixes: hem band (no bare lower back), hood, zipper, buckle box, collar goggle unit,
    shoes, knee pad, thigh strap, bag, cat pouch, and a flat black bodysuit for the inside-out
    shorts and collar.
  - Open jacket over the black top, and the duplicate hair copy (red streaks) removed.
  - ORM maps on the cloth, hair at 2048, `_OUTLINE` normals, hair bone chains with spring extras.
- **Viewer** (`deploy-web.sh`; served files match the repo):
  - Two-band toon with per-role tints, AO creases and a metal glint.
  - Face sphere normals, stepped rims plus a mood rim, angel ring.
  - Inverted-hull outlines (13 draw calls, +1 contact shadow); MSAA on iOS 17+.
  - Look-at with saccades and an eye guard (the cap never hides her eyes in the stage's states).
  - Breathing, arm relax, speech nods, blinks, fidgets, mood hysteresis, eased camera.
  - Spring-bone hair physics.
- **Stage:**
  - Full-height canvas fading under the top bar and dock, with insets passed to the viewer.
  - Mood rim tint.
  - SJ-04, SJ-11/UI-05, UI-01 and SJ-03.
- **Verification:**
  - `make test-js`: 54 stage + 83 avatar tests.
  - Live e2e 11/11 after each of the three deploys.
  - Two code reviews (viewer/stage, then springs); their findings were fixed before deploy.
  - Real-stage renders at phone and desktop size.

### Commits

- `206ca6c` core TTS log
- `727343f` Makefile
- `8b30662` viewer
- `ecda6af` stage
- `8baba09` build
- `6b7fae0` README
- `04e3cd8` open jacket + hair copies on
- `dcc27d8` springs
- `11ae8b6` chains on

### Rollback

All files are in `assets/build/rollback/` (gitignored).

- **GLB:** copy one of these to `assets/build/klukai.glb` with a temp file and `mv`, so the
  served file is always complete:
  - `klukai-pre-3dlook-20261011.glb` (before this work);
  - `klukai-wave1-20261011.glb`;
  - `klukai-w2a-20261011.glb` (no chains).
- **Web:** check out the older `flutter_app/web/companion/*` and run `scripts/deploy-web.sh`.
  Copies of the pre-3D-look `avatar3d.js` and vendor bundle are kept for reference.
- A GLB with chains needs the spring viewer. An older viewer swings the ponytail like a stick, so
  roll back both together.

### Open / not verified

- **Real devices:** nothing was checked on a real iPhone or in Brave. All renders are SwiftShader.
  Watch `av.stats` (`fps`, `drawCalls`, `outlines`, `springMs`). Adaptive quality drops the
  outlines when a device can't keep up.
- **Missing source geometry:** the cap goggles and the blue hairclips.
- **`calling` crouch** (no state plays it): the back chain's first tail can sit inside the neck
  capsule. Shrinking that collider's radius to ~0.075 would help.
- **VP-10** needs a `companion-core` rebuild to go live (no restart was done).
- **Working notes:** `.claude/look-work/` (reports `i1/REPORT.md`, `i2/REPORT.md`, renders,
  lead harness `lead/shoot.py`).

