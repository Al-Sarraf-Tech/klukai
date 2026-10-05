# Klukai 3D avatar: asset pipeline and viewer

Builds `assets/build/klukai.glb` and ships the three.js viewer module
`flutter_app/web/companion/avatar3d.js`. The GLB has:

- one skinned body mesh, plus a head mesh carrying the `blink` morph, sharing
  one skin;
- 15 named clips;
- webp textures and meshopt compression, about 1.4 MB in total.

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
node build.mjs             # → assets/build/klukai.glb (~1.4 MB, budget 15 MB; written via temp + rename)
node build.mjs --debug     # + assets/build/klukai.debug.glb: every OBJ group, PNG, no compression
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
   OBJ export de-duplicates UV values. A few visually checked
   `atlasOverrides` (the cap crown, the iris discs, a shadow shell) handle the
   cases the heuristic gets wrong.
4. **Dropped groups:**
   - other-outfit layers (Astral Luminous, Speed Star) and the outline shells;
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
7. **Clips:** 15 clips with their lowercase names: `idle talking happy bashful thinking thankful
   salute excited calling defeated dismiss nervous kneel sleep stand`.
   - Only quaternion tracks plus the hips position are kept.
   - Horizontal hip drift is removed so she stays centred.
   - `sleep` lowers the hips to the floor, because Mixamo leaves them at
     standing height.
   - Each clip's loop flag is in `extras.loop`.
8. **Export and optimise:** `GLTFExporter` handles geometry, skin, morph and
   clips. Then glTF-Transform:
   - **Textures:** attached as webp, at most 1024 px (face 1024, eyes 512,
     body skin 512).
   - **Materials:** each gets `extras.role` (skin, hair, face, cloth or
     overlay).
   - **Draw calls:** parts sharing a texture, role and sidedness are merged
     into one material (7 draw calls).
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
av.setSpeaking(level);      // 0..1: layers the talking clip + subtle head motion
av.setFraming('full'); av.resize(); av.dispose();
```

- **Look:** `MeshToonMaterial` with stepped ramps (a softer ramp on the face),
  a fresnel rim light, additive eye highlights, and a transparent canvas so
  the page backdrop shows through. There is one toon program per sidedness,
  and two lights (a hemisphere fill and one key).
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
- **Adaptive resolution:** the pixel ratio steps by 0.25, between 1 and the
  device cap, depending on frame time. Pass `adaptiveResolution: false` to
  disable it (the test pages do).
- **Motion:** 0.45 s crossfades, with idle as the base loop. One-shot states
  (`greet` = salute) settle back to idle. There's a procedural blink (with the
  occasional double blink) and eyes stay shut in `sleep`.
- **Camera:** framing with headroom for the cap, an upper-body or full-body
  option, and a side view for `sleep`. OrbitControls are limited to about
  ±25° azimuth with a small polar range, no pan or zoom.
- **iOS and robustness:**
  - DPR is capped at 2 (1.5 on small screens) and antialias is off on iOS.
  - The loop pauses while `document.hidden`.
  - `webglcontextlost` pauses rendering and `webglcontextrestored` resumes it.
  - `dispose()` frees geometries, materials, textures and the context.
  - Without WebGL, or on any load error, it calls `onError` and returns a
    no-op stub.
- three.js is **vendored as one tree-shaken bundle**,
  `vendor/three-avatar.min.js`: 679 KB (171 KB gzip), down from 980 KB
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

KTX2/Basis was evaluated and **not adopted**:

- Texture memory is already about 24 MB.
- The Basis transcoder would add about 576 KB (JS + WASM) plus WASM compile
  time on every cold load, which is the opposite of "draw faster".
- No KTX encoder is in the toolchain.

On the server side, two things are worth doing (outside this tool):

- **Compression:** the GLB gzips to about 0.95 MB, but `/api/avatar/model`
  sends it uncompressed. gzip or brotli on that route and on `/app/companion/`
  JS would save another ~30%.
- **Cache:** `Cache-Control: private, max-age=86400` means a rebuilt GLB
  reaches a browser only after its cached copy expires (up to a day). Bump a
  version query or ETag-revalidate (`no-cache`) to roll out immediately.

## Other outfits (research only, nothing downloaded)

The dorm-kit mesh is the only one in `assets/source-models/`.

- **Official route.** MICA/Sunborn publish **official MMD (PMX) models**, Klukai
  included in several outfits, on their 模之屋/aplaybox account
  (aplaybox.com/u/636064186) and the global art page
  (gf2exilium.sunborngame.com/main/art, since 2025-05-22).
  - **Stated terms:** non-commercial, no redistribution.
  - **Unconfirmed:** secondary sources also report "no modification / video
    production only". Read the readme in the archive before converting.
  - **Why it's the best route:** MMD models carry face morphs (blink and
    a/i/u/e/o visemes), which would give real lip-sync.
- **Effort:** a PMX→GLB path (three's MMDLoader in Node, with Japanese MMD
  bone names), plus retargeting the 15 Mixamo clips onto the MMD skeleton
  locally (SkeletonUtils), plus morph export. About 1–2 days for the first
  outfit, then hours per extra outfit. Uploading to Mixamo for auto-rigging
  would send the model to a third party; local retargeting avoids that.
- **Alternatives (game rips, same personal-use footing as today):**
  - an Open3DLab .blend with all skins (needs Goo Engine);
  - a Garry's Mod 7-outfit pack on the Steam Workshop (re-rigged and
    decimated);
  - DeviantArt MMD conversions;
  - self-extraction from the owner's own install, where the SSR0101/0102/0103
    meshes sit in the same Unity bundles as the textures we already have.

## Known limitations

- There are no mouth or viseme morphs in the source, so speech is the talking
  clip plus head motion, not lip-sync.
- The cap brim shades her eyes when a clip looks down (`talking`).
- `calling`, `kneel` and `stand` ship in the GLB but no state uses them yet.
  `calling` is a crouching gesture.
- No hair or cloth physics. In `sleep` the hair hangs as if standing, and an
  ankle strap stretches between the feet.
- Screenshots come from SwiftShader; real GPUs render with MSAA and look
  smoother.
