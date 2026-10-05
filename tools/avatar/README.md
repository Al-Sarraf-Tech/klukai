# Klukai 3D avatar: asset pipeline and viewer

Builds `assets/build/klukai.glb` (one skinned mesh, 15 named clips, a `blink`
morph target, webp textures, meshopt) and ships the three.js viewer module
`flutter_app/web/companion/avatar3d.js`.

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
node build.mjs             # → assets/build/klukai.glb (~2.2 MB, budget 15 MB)
node build.mjs --debug     # + assets/build/klukai.debug.glb: every OBJ group, PNG, no compression
npm test                   # node --test (pure parts; needs no assets)
python3 shot.py            # screenshots → assets/build/shots/
node vendor.mjs            # re-vendor three.js into flutter_app/web/companion/vendor/
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
   - attaches textures as webp, ≤ 1024 px (face 1024, eyes 512);
   - writes `extras.role` per material (skin, hair, face, cloth, overlay);
   - runs `resample`, `dedup` and `prune`, then `meshopt` with quantization.

Debug helpers: `debug/inspect.html` plus `debug/snap.py` render any material
subset, clip, time or blink weight with arbitrary texture overrides. That's
how the atlas, UV and eye problems above were found.

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
  the page backdrop shows through.
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
- three.js and its addons are **vendored** (`vendor/`, bare `'three'` imports
  rewritten), so there's no CDN and no import map.

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
