# Handoff: finish the 3D model ("it's almost there")

Date: 2026-10-11. For the next Claude/Codex session. Also posted on the bus (project `klukai`).

**The owner's verdict on the shipped 3D Klukai is "almost there".** No specifics were given.

**Step 0: find out what still looks off.**
- Ask the owner (bus note to `owner`, or in session) what looks wrong, ideally with a phone
  screenshot.
- Then look on a real iPhone (Safari) and in Brave yourself.

Everything so far was judged on SwiftShader renders only. Don't start tuning before you know what
he sees.

## Where it stands (all live on amarillo, `main` = `d2a89ad`, pushed to `origin`)

- **Full record:** `docs/handoff/2026-10-11-merge-deploy-and-3d-look.md` §3 (what shipped, commits,
  rollback).
- **GLB:** `assets/build/klukai.glb`, sha256 `cee5863e…`. 1.97 MB, 8 primitives, 67 joints.
  - Head is a real joint.
  - Atlas fixes; the open jacket over the black bodysuit; the duplicate hair copy removed.
  - ORM maps on the cloth, hair at 2048, `_OUTLINE` normals.
  - Hair bone chains plus the `springChains`/`springColliders` extras.
- **Viewer:** `flutter_app/web/companion/avatar3d.js`, served sha `5b99f230…`.
  - Two-band toon with AO, outlines, rims, the angel ring and MSAA (iOS 17+).
  - Look-at with an eye guard, life layers, eased camera, spring hair.
- **Stage:** the full-height canvas fades under the top bar and the dock; mood rim tint.

## Rules you must keep (each one has bitten)

- **Building is a live deploy.**
  - `cd tools/avatar && node build.mjs` without `AVATAR_OUT` overwrites the served GLB, because
    `assets/build` is bind-mounted into companion-core as `/avatar`.
  - Develop with `AVATAR_OUT=../../assets/build/dev/<name>.glb`.
  - Deploy a GLB by copying it to a temp file in `assets/build/` and then `mv`.
- **GLB and viewer are coupled.** A GLB with hair chains needs the spring viewer; an older viewer
  swings the ponytail like a stick. Roll back both together. Rollback GLBs are in
  `assets/build/rollback/`.
- **The assets are game-ripped.**
  - Never commit anything under `assets/`.
  - The research renders live in `.claude/research-3d-look-20261011/` (gitignored).
  - **Work on amarillo:** the source model, the textures and the research exist only there.
- **SwiftShader renders are CPU-heavy.** Serialise every render and build with
  `flock .claude/look-work/render.lock …`, and check `sensors | grep Package` first. amarillo's
  limit is 70 °C.
- **Viewer constraints:**
  - No `polygonOffset` (a test forbids it).
  - `NoToneMapping` only.
  - No post-processing passes and no CDN.
  - After adding a `THREE.X` use, run `node tools/avatar/vendor.mjs`; a test checks the bundle's
    exports.
- **Git and money:**
  - Conventional commits, no AI co-author.
  - "Commit locally" = commit + `~/git/git-server/git-sync.sh klukai`.
  - Push to GitHub only when the owner asks.
  - No spending.

## Known gaps, most likely what "almost there" means

1. **Real-device behaviour (unverified).**
   - On the iPhone, read `av.stats`: `fps`, `drawCalls` (13, +1 in full framing), `outlines`
     (adaptive quality drops them when slow), `springMs`.
   - Texture memory is about 43 MB (hair 2048 is 21 MB of it).
   - If it stutters, first try hair at 1024 on phones, or outlines off by default on small
     screens. Don't remove features blindly.
2. **Face.**
   - The shading is sphere-normal toon. The SDF face-shadow map is not done (R1 §2.5 "phase 2":
     generate offline in the build, about 8 light yaw angles → one R8 texture).
   - The mesh has no mouth or viseme morphs, so there is no lip-sync. Speech is the talking clip
     plus nods.
   - Real lip-sync needs the official MMD/PMX model (free; README "Other outfits"). That is an
     owner decision.
3. **Missing geometry.** The cap goggles and the blue cross hairclips: their textures exist on
   cloth1, but no mesh does. Options:
   - simple generated meshes in the build, UV-mapped onto the cloth1 regions;
   - a better source model (owner decision, free only).
4. **Hair.**
   - The angel ring is subtle, because the cap covers the crown.
   - The two shoulder curtains are not on chains (arm-weighted, a bit stiff).
   - In the `calling` crouch (no state plays it), the back chain's first tail sits inside the neck
     capsule. A collider radius of about 0.075 in `HAIR_CHAINS` would help.
   - Check whether the inner-hair "bob" edge at 54° head turns still shows now that the chains
     exist.
5. **Cloth.**
   - The diagonal sling is a little lighter than the reference's charcoal (texture value).
   - From 30° or more above, you can see into the hollow jacket between the neckline and the
     collar.
   - The bodysuit is flat black with no outline; the rim gives it a sheen. A subtle spec could
     help.
6. **Motion extras (not done).**
   - Eye bones for real eye darts (R3 #11).
   - Pointer parallax.
   - `calling`/`kneel`/`stand` are unused.
   - An ankle strap stretches in `sleep`.
7. **Normal maps** are not shipped. R2 advises against them for toon; use them only to bend the
   rim, if anything.

## Tools

| What | Command (from the repo root unless noted) |
|---|---|
| 12-angle sheet | `python3 tools/avatar/debug/views.py OUT [/assets/build/dev/x.glb]` |
| Iris visibility under the brim, every clip | `python3 tools/avatar/debug/eyes.py …` |
| Motion strips with spring clearance | `python3 tools/avatar/debug/motion.py OUT --glb … --seq idle:2,happy:3 --clips nervous:0.3:1.2` |
| Life layers / variant grids / GLB summary | `debug/life.py`, `debug/grid.py`, `debug/glbinfo.mjs` |
| Real stage layout | `tools/avatar/test-page/stage.html`; lead harness `.claude/look-work/lead/shoot.py` (`PAGE=stage-deployed.html` renders the deployed viewer) |
| Phone/desktop shots, perf | `python3 tools/avatar/shot.py --out …`, `python3 tools/avatar/perf.py` |
| Tests | `make test-js` (54 stage + 83 avatar); `cd tools/avatar && npm test` |
| Live e2e | `python3 scripts/e2e_live.py` (11 checks, user `claude`) |

**Viewer debug hooks** (`opts.debug`): `probe()`, `seek(t)`, `poseAt(clip,t)`, `run(s)`,
`playClip`, `tuneSprings`, `setOutlines`. `inspect3d.html` accepts `set=` for live tuning.

**Build switches:**
- `AVATAR_FEATURES=openJacket,hairCopies,hairChains`; all three are on by default.
- `AVATAR_CONFIG=variant.mjs` for experiments.
- `--debug` logs ambiguous atlas islands.

## Deploy

1. **Web:** `scripts/deploy-web.sh`. Then compare the shas of `/app/companion/*` (served from
   `localhost:8300`) against the repo.
2. **GLB:** build to `assets/build/dev/…`, check the renders, then copy to a temp file in
   `assets/build/` and `mv` it over `assets/build/klukai.glb`.
   - Verify with `docker exec companion-core sha256sum /avatar/klukai.glb`.
   - Commit the build config so that a plain build reproduces the live GLB byte for byte.
3. Run `python3 scripts/e2e_live.py`, then commit locally (commit + git-sync).

## Reading list

- `.claude/research-3d-look-20261011/` (on amarillo only):
  - `R1-rendering.md`, `R2-assets.md`, `R3-presentation.md`;
  - references `r2/dl1/whole_prev.jpg` (official art) and `r2/dl2/ws_12.jpg` (in-game rip).
- `.claude/look-work/{i1,i2}/REPORT.md`: what the two implementers did, with values and evidence.
- `tools/avatar/README.md`: the build and viewer reference.
