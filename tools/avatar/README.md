# Klukai 3D avatar: asset pipeline and viewer

Builds `assets/build/klukai.glb` and ships the three.js viewer module
`flutter_app/web/companion/avatar3d.js`. The GLB has:

- one skinned body mesh, plus a head mesh carrying the `blink` morph, sharing
  one skin (67 joints: the Mixamo rig, `mixamorigHead`, which the build
  makes a real joint, and 15 hair bones for the viewer's spring physics);
- 15 named clips;
- a baked `_OUTLINE` vertex attribute for the viewer's outline, and ORM maps
  on the cloth;
- webp textures and meshopt compression, about 2.0 MB in total.

The mesh is her default outfit, "Blazing Star" (SSR01 atlases). The dorm outfit
has a texture in `assets/textures` but no mesh.

## Licensing / handling (read first)

The source model, clips and textures are **game-ripped (GFL2), personal use
only**. The built GLB is a derived model, so the same rules apply to it:

- **Never commit** `assets/source-models/`, `assets/textures/` or
  `assets/build/`. The repo `.gitignore` covers them, and the build also writes
  `assets/build/.gitignore` (`*`) as a second guard.
- **Serve the GLB only through the authenticated API.** The viewer never takes
  a URL; it calls `loadModel()`, which the app implements with its
  authenticated fetch, for example `fetch('/api/avatar.glb', {credentials:'include'}).then(r => r.arrayBuffer())`.
  Don't put it under a public static path or a CDN.
- three.js is MIT (`vendor/LICENSE.three.txt`).

## Requirements

Node ≥ 22. Blender, FBX2glTF and assimp are **not** needed: three.js loaders
and exporter run in Node behind small shims in `lib/node-three.mjs`. For
screenshots: the Python `playwright` package with its bundled Chromium.

```sh
cd tools/avatar
npm ci                     # exact versions from package-lock.json
node build.mjs             # → assets/build/klukai.glb (~2.1 MB, budget 15 MB; written via temp + rename)
                           #   ⚠ assets/build is bind-mounted into companion-core: this IS a live deploy
AVATAR_OUT=../../assets/build/dev/klukai.glb node build.mjs   # development build (not served)
AVATAR_CONFIG=my-variant.mjs …                                # build from a config variant (exports CONFIG)
AVATAR_FEATURES=openJacket,hairChains …                       # force optional features on for one build
node build.mjs --debug     # every OBJ group, PNG, no compression (+ island JSONs, ambiguous-island log)
npm test                   # node --test (pure parts; needs no assets)
python3 shot.py            # screenshots → assets/build/shots/
python3 perf.py            # load time / FPS / draw calls / texture MB (SwiftShader, --throttle 4)
python3 debug/views.py OUT # 12-angle inspection sheet of the live viewer on a contrasting backdrop
node vendor.mjs            # rebuild vendor/three-avatar.min.js (tree-shaken three subset)
```

The build is deterministic: pinned dependencies, a seeded sampler and fixed
config. It fails if a source is missing, if the OBJ and FBX disagree, or if
the output exceeds the 15 MB budget.

## What the build does (and why)

Inputs: `assets/source-models/klukai_for_mixamo.obj`, the 15 Mixamo FBX files
(each contains the same skinned mesh plus one clip), and `assets/textures/*.png`.
All tuning lives in `avatar.config.mjs`.

1. **Base mesh:** `Idle.fbx` loads through `FBXLoader` (65 bones, 4 weights per vertex).
2. **Material groups:** Mixamo flattened everything into one material, but
   its triangle order equals the OBJ face order (verified on every build). So
   each triangle takes its `usemtl` group from the OBJ.
3. **Mixed atlases:** the cloth group samples two atlases: SSR01 `cloth1`
   (jacket) and `cloth2` (gear, stockings, boots). Each UV island goes to
   the atlas that is *smoothest inside it*. On the right atlas an island
   covers one painted region; on the wrong one it straddles outlines. Islands
   join only across edges that share both positions and UVs, because Blender's
   OBJ export de-duplicates UV values. The heuristic rewards smoothness, so it
   gets detailed islands (zippers, logos, stitching) wrong; visually checked
   `atlasOverrides` correct them (cap crown, hem band, hood, zipper, buckle
   box, collar goggle unit, shoes, knee pad, bag, cat pouch, iris discs, a
   shadow shell). An override matches islands by bind-pose `box` (island
   centroid, metres) and/or `uvBox`, and can also set `role`, `doubleSided`,
   `name`, `flip` (inside-out triangles) and a flat colour
   (`atlas: { color }`). The shorts and the high-neck collar sit inside the hair
   group and are inside-out; an override turns them into the flat black
   `bodysuit` cloth material. `--debug` logs islands whose score ratio is
   under 1.5× and that no override covers.

   The source also omits the neck below the jaw and the central torso under
   the jacket. `bodyCore` adds a closed inner high-neck bodysuit, fitted in
   bind-pose metres and blended along the hips, spine, neck and head joints.
   It shares the existing black bodysuit material, adds 896 triangles and no
   extra draw call or texture. The GLB tests raycast the neck and stomach
   from all sides with hair and jacket excluded; the geometry unit test
   checks closure, outward winding and skin weights.
4. **Dropped groups:**
   - other-outfit layers (Astral Luminous, Speed Star) and the `Outline` group,
     which is an 80-face eyelid strip, not an outline shell (outlines are
     generated from `_OUTLINE` instead);
   - `Klukai_Cloth_2`, a near-duplicate layer of `Cloth_1` that z-fights;
   - the leg skin under the stockings, which pokes through them.

   Hip skin under the jacket and shorts is shrunk 5 mm inward instead.
   Exact duplicate triangles are removed.
5. **Face repair:** the source face is a *baked wink*: the right eye is open
   and the left lid is closed over its iris. Mixamo dropped the shape keys,
   and the earlier painted-eyes texture (`c_Clukay_face_open_eyes.png`) put
   the irises on the cheeks, so it isn't used. `lib/eyes.mjs` instead:
   - opens the closed eye by giving each closed-side eye vertex the mirrored
     position of its open-side twin, matched by mirrored UV (u ↔ 1−u);
   - keeps the original closed state as the **`blink` morph target**, for both
     eyes, by mirroring the other way;
   - leaves the iris and highlight discs untouched (they are already
     symmetric), fills unmatched corners from their neighbours so nothing
     spikes, tucks the eye whites behind the closing lid, and fades the morph
     out at brow height so blinks don't move the brows.

   Result: two open green irises on real eye geometry, plus a real blink.
6. **UVs:** V is flipped to the glTF convention, because textures are attached
   later from the original PNGs rather than through `GLTFExporter`.
6a. **Head joint** (`HEAD_SKIN` in the config): Mixamo gave `mixamorigHead` no
   weights, so the face, cap and bangs rode on the Neck and every head
   rotation in the clips (8–55°) was invisible. The build adds Head as a
   joint (inverse bind consistent with the Neck's bind pose) and moves Neck
   weight onto it over y 1.27–1.31 m. Hair behind the ear plane also moves a
   share of its spine weights to Head, fading over y 1.0–1.42, so the
   ponytail bends like a rope instead of tearing in strong turns.
7. **Clips:** 15 clips with their lowercase names: `idle talking happy bashful thinking thankful
   salute excited calling defeated dismiss nervous kneel sleep stand`.
   - Only quaternion tracks plus the hips position are kept.
   - Horizontal hip drift is removed so she stays centred.
   - `sleep` lowers the hips to the floor, because Mixamo leaves them at
     standing height.
   - Each clip's loop flag is in `extras.loop`.
8. **Export and optimise:** `GLTFExporter` handles geometry, skin, morph and
   clips. Then glTF-Transform:
   - **Textures:** attached as webp, at most 1024 px by default (hair 2048,
     face 1024, eyes 512, body skin 512).
   - **ORM:** the cloth atlases' `_rmo` maps (R roughness, G metal, B baked
     AO) are repacked to glTF ORM (R occlusion, G roughness, B metal), 512 px
     lossless webp, and attached as both `occlusionTexture` and
     `metallicRoughnessTexture`. The viewer reads the AO into its toon
     threshold and the metal mask for a glint. No normal maps ship.
   - **Materials:** each gets `extras.role` (skin, hair, face, cloth or
     overlay).
   - **Draw calls:** parts sharing a texture (or flat colour), role and
     sidedness are merged into one material (8 primitives).
   - **Optional features** (`FEATURES` in the config, all on by default since
     2026-10-11):
     - `openJacket`: merges the Astral Luminous group's own bodysuit bust and
       neckline into `Klukai_Cloth_1` (its 294 shifted twin islands are
       skipped), so the jacket opens over her glossy black top as in her art.
     - `hairCopies`: drops a second copy of the hair inside `Klukai_Face` that
       z-fought the real hair (red streaks along strand edges).
     - `hairChains`: `hair_<chain>_<i>` bones (ponytail 5, back 4, side locks
       3 + 3) fitted through the hair and parented to Head; hair reweighted onto
       them (arm, thigh and spine influences stripped); scene extras
       `springChains` (`{ name, bones, tail }`) and `springColliders`
       (spheres/capsules on body bones) for the viewer. Unsimulated, the chains
       swing the ponytail like a stick: ship it only with the spring viewer.
   - **`_OUTLINE`** (normalized int8 vec4 on every primitive; three names it
     `_outline`): xyz is the angle-weighted normal averaged across both meshes
     by position (the source normals have hard splits that would crack an
     inverted hull), w the outline width: 0 on the eyes, brows, mouth and eye
     overlays, 0.6 on hands, 1 elsewhere. Added before meshopt quantises
     positions.
   - **Head mesh:** primitives whose `blink` target moves go to a separate
     `KlukaiHead` mesh, so the ~30k body vertices skip morph work. Both meshes
     share one skin, thanks to a `scene` quantization volume.
   - **Compression:** `resample` (tolerance 1e-3, about 0.1° error), `dedup`
     and `prune`, then `meshopt` at level `high` (quaternion and octahedral
     filters). Animations shrank from about 0.9 MB to 0.2 MB.

Debug helpers:

- `debug/inspect.html` plus `debug/snap.py` (raw materials, via an import map
  to `node_modules`) render any material subset, clip, time or blink weight
  with arbitrary texture overrides. That's how the atlas, UV and eye problems
  above were found.
- `test-page/inspect3d.html` plus `debug/views.py` render the real viewer from
  any angle. `?poff=N` emulates a GPU with N× depth bias, which is how the
  hollow look was reproduced.

## Viewer (`flutter_app/web/companion/avatar3d.js`)

```js
import { createAvatar3D, moodToState } from './avatar3d.js';
const av = await createAvatar3D(el, { loadModel, onReady, onError, framing: 'upper' /* | 'full' */ });
av.setMood(mood);           // any of her 50 moods → a state (moodToState is pure, exported)
av.setState('greet');       // idle talking thinking happy bashful sad greet sleep excited flustered
av.setSpeaking(level);      // 0..1: layers the talking clip + speech-driven nods
av.setTint(0x8fd8ff);       // mood rim colour (number, '#rrggbb', or 'base'|'smile'|'blush'|'annoyed'), eased ~1.2 s
av.setInsets({ top, bottom }); // CSS px of the canvas covered by the stage's top bar / dock
av.setFraming('full'); av.resize(); av.dispose();
```

- **Look** (anime toon, after GFL2; research in the 2026-10-11 handoff):
  - Two-band toon on `MeshToonMaterial` via `onBeforeCompile`: half-Lambert
    with a `smoothstep`/`fwidth` terminator and a tinted shadow colour per
    role (warm skin, high-key face, cool silver hair, purple-cool cloth).
    Lights are calibrated so the lit side is about 1× albedo under
    `NoToneMapping` (never ACES/AgX: they shift her purple and skin).
  - Baked AO from the cloth ORM deepens the toon creases; the metal mask gives
    buckles and zips a hard glint.
  - Face normals are blended toward a sphere around the head (no blotchy nose
    or cheek shading). Shader maths on `position` uses the GLB's quantised
    units, derived from the skin's bind matrices, never hard-coded metres.
  - A stepped key-side rim tinted by albedo, plus a back rim in the mood
    colour (`setTint`); a hair "angel ring" band.
  - **Outline:** an inverted hull. A second `SkinnedMesh` per part shares
    geometry, skeleton and morph influences (blinks follow), draws back faces
    pushed out along `_outline` at a constant pixel width with a view-space z
    push (not `polygonOffset`), coloured by texture × a per-role tint. Older
    GLBs without `_outline` get smoothed normals computed at load. Eyes,
    overlays and flat near-black parts get none.
  - Additive eye highlights and a transparent canvas, so the stage backdrop
    shows through.
- **No `polygonOffset`.** It used to pull double-sided cloth forward to beat
  skin z-fighting. Depth bias is slope-scaled and depends on the GPU, and
  Safari/Metal applies far more of it. The jacket's inner and back layers then
  drew through its front, and she looked **hollow**. The skin that z-fought is
  now removed or shrunk in the build, and a test keeps `polygonOffset` out.
- **Load:**
  - The model download starts in parallel with the (single, cached) vendor
    import.
  - `warmAvatar3D({loadModel})` can start both earlier, for example at page
    init when the saved mode is 3D.
  - Shaders compile through `renderer.compileAsync` before the first frame.
  - `av.stats` reports load timings, draw calls and fps.
- **Adaptive quality:** every ~2 s the pixel ratio steps by 0.25 between 1
  and the device cap, depending on frame time; at ratio 1 and still slow the
  outlines are dropped, and they come back first when there is headroom.
  Pass `adaptiveResolution: false` to disable it (the test pages do).
- **Motion** (all after `mixer.update`, in world space, so bone axes don't
  matter):
  - 0.45 s crossfades with idle as the base loop; one-shot states (`greet` =
    salute) settle back to idle. Idle starts at a random point, and after a
    full idle cycle a short fidget sub-clip sometimes plays (never the same
    twice in a row).
  - Mood changes hold for 4 s unless the new mood is strong, so a chatty
    backend doesn't make her flap. `setState()` is always immediate, and a
    state set that way (thinking, greet) is left at once by `setMood()`.
  - Look-at: she faces the camera (and the mouse on desktop), damped and
    clamped, weighted per state, with micro-saccades; split 40/60 between
    Neck and Head. A guard keeps clips that pitch the face down from hiding
    her eyes under the cap brim.
  - Breathing, relaxed arms in idle-like states, nods driven by her speech
    level, blinks on large gaze shifts and phrase ends (slow blinks in tender
    moods, a slight droop when sad), eyes shut in `sleep`.
  - **Hair springs:** three-vrm's spring-bone joint on the GLB's hair chains
    (inertia, stiffness toward the animated rest direction, gravity, length
    constraint, a per-joint cone, sphere/capsule colliders from
    `springColliders` plus the floor), fixed 1/60 s steps with a 0.1 s clamp,
    in the character root's space. A static "hang" pose stands in on load,
    around sleep, on returning to the tab, after big pose jumps and under
    reduced motion. GLBs without chains fall back to a vertex-shader sway.
    About 0.03 ms per step on a desktop CPU.
  - `prefers-reduced-motion` turns off drift, sway, fidgets and push-in, and
    keeps blinks and look-at.
- **Camera:** upper-body or full-body framing, a side view for `sleep`, eased
  over ~0.8 s instead of snapping. 22° FOV for upper framing on phones (26°
  otherwise, same crop), a 3% push-in while she talks, a slow drift and a
  slow follow of her hips. `setInsets()` frames her in the band the stage's
  top bar and dock leave clear. OrbitControls are limited to about ±25°
  azimuth with a small polar range, no pan or zoom, and spring back to the
  front 2 s after release. A soft contact-shadow blob grounds her in full
  framing and `sleep` (no shadow maps).
- **iOS and robustness:**
  - MSAA is on everywhere, iOS included (Apple GPUs resolve it on-tile; thin
    outlines crawl without it), with `powerPreference: 'low-power'`. DPR is
    capped at 2 (1.75 on small screens).
  - The loop pauses while `document.hidden`.
  - `webglcontextlost` pauses rendering and `webglcontextrestored` resumes it.
  - `dispose()` frees geometries, materials, textures and the context.
  - Without WebGL, or on any load error, it calls `onError` and returns a
    no-op stub (which has every API method, including `setTint`/`setInsets`).
- three.js is **vendored as one tree-shaken bundle**,
  `vendor/three-avatar.min.js`: 681 KB (172 KB gzip), down from 980 KB
  (245 KB gzip) across six files. Its exports are generated from the
  viewer's `THREE.*` uses, and a test checks them. There's no CDN and no
  import map.

## Performance

Measured with `perf.py` at 390×844 (DPR 3, capped to 1.5) with a 4× CPU
throttle. The GPU is SwiftShader (software), so FPS here is CPU-bound and not
representative of an iPhone GPU.

| | before | after |
|---|---|---|
| GLB | 2.32 MB | 1.41 MB |
| vendor JS | 980 KB (6 files) | 679 KB (1 file) |
| load to first frame (localhost) | 0.80–0.91 s | 0.75–0.86 s |
| draw calls | 8 | 7 |
| shader programs | 5 | 4 |
| vertices with morph work | 41k | 11k |
| texture memory (est.) | 28.3 MB | 24.3 MB |
| skins | 1 | 1 |

The table above is the first optimisation pass (2026-10-05). The 3D-look pass
(2026-10-11: head joint, atlas fixes, ORM, 2048 hair, toon rework, outlines,
life layers) changed it like this, same harness:

| | before | after |
|---|---|---|
| GLB | 1.41 MB | 2.10 MB (1.97 MB with the open jacket, hair-copy drop and chains) |
| primitives / draw calls | 7 / 7 | 8 / 13 with outlines (+1 contact shadow in full framing and sleep) |
| shader programs | 4 | 9 |
| texture memory (est.) | 24.3 MB | 43 MB (hair 2048 is 21 MB of it) |
| fps, DPR 1, no outlines | 18.0 | 16.5–17.5 |
| fps, DPR 1, outlines | — | 12.3–13.3 |
| time to ready (throttled) | 0.64–0.73 s | 0.95–1.14 s |

The outlines cost the most here because SwiftShader skins every hull vertex
again on the CPU; a phone GPU pays far less. Adaptive quality drops them when a
device can't keep up. Not yet measured on a real iPhone: check `av.stats`
(`fps`, `drawCalls`, `outlines`) on device.

KTX2/Basis was evaluated and **not adopted**:

- Texture memory was about 24 MB (43 MB with the 2048 hair; still fine for
  current iPhones).
- The Basis transcoder would add about 576 KB (JS + WASM) plus WASM compile
  time on every cold load, which is the opposite of "draw faster".
- No KTX encoder is in the toolchain.

On the server side (outside this tool):

- **Compression:** the GLB gzips to about 0.95 MB, but `/api/avatar/model`
  sends it uncompressed. gzip or brotli on that route and on `/app/companion/`
  JS would save another ~30%.
- **Cache:** done. `/api/avatar/model` sends `Cache-Control: private, no-cache`
  plus an ETag, and `net.js` fetches with `no-store`, so a rebuilt GLB reaches
  devices on their next open.

## Personal appearances

The companion's **Look** selector supports separate private GLBs while keeping the approved default pinned at `avatar-v2026.10.12`. See [the research and implementation record](../../docs/research/2026-10-12-3d-outfit-integration.md) for permissions, sources, validation and remaining download requirements.

Five Garf appearances cover Blazing Star, Dorm, Speed Star, Astral Luminous and Cerulean Breaker. Player Legend's HK416 supplies the Retro 64 rendition. VRoid HK416 remains unavailable pending Pixiv sign-in/download. The stage displays “For personal, noncommercial use only.” Original creator licenses remain applicable; this is not a claim of publisher approval. Official Global MMD downloads are excluded because their terms prohibit software use.

The offline converters require explicit source/output paths and refuse to overwrite `klukai.glb`:

```sh
# Use checksum-verified Blender 4.1.1 with source scripts disabled.
blender -b --factory-startup --disable-autoexec --threads 2 \
  --python tools/avatar/import-garf.py -- SOURCE.blend speed_star RAW.glb
node tools/avatar/retarget-variant.mjs assets/build/klukai.glb RAW.glb VARIANT.glb
blender -b --factory-startup --disable-autoexec --threads 2 \
  --python tools/avatar/import-gfl64.py -- SOURCE_RIG.blend VARIANT.glb
```

Run builds/renders under `.claude/look-work/render.lock`; validate dev outputs before atomically installing the fixed registry filename under `assets/build/variants/`. Assets remain gitignored and authenticated, never copied into `web-build`. Garf variants retain 15 retargeted clips and blink; Retro 64 retains its creator's idle clip. Converters preserve each source's own coverage instead of transplanting the default body core. Registry IDs and attribution live in `docker/core/app/avatar_models.py`.

## Known limitations

- There are no mouth or viseme morphs in the source, so speech is the talking
  clip plus head motion, not lip-sync.
- The cap goggles and blue cross hairclips are not in the source mesh (their
  textures are). The open-jacket bust is recovered from the alternate cloth
  group; the absent neck and abdomen are reconstructed by `bodyCore`.
- `calling`, `kneel` and `stand` ship in the GLB but no state uses them yet.
  `calling` is a crouching gesture.
- No cloth physics. Hair has spring physics (below); in the deep `calling`
  crouch (no state plays it) the back chain's first tail can sit inside the
  neck collider. In `sleep` an ankle strap stretches between the feet.
- Screenshots come from SwiftShader; real GPUs render with MSAA and look
  smoother.
