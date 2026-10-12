# Approved avatar release

The owner approved the deployed neck and stomach repair on 2026-10-12 and requested that this version be saved and pinned. The annotated Git tag is `avatar-v2026.10.12`. Preserve this appearance as the baseline for future changes.

## What changed

The source model contained the head and arms but omitted the connecting neck and central torso under the jacket. Hair and clothing hid these gaps in the initial full-body inspection. The owner's screenshots exposed them; removing the outer layers in the 3D viewer confirmed the missing geometry. The earlier `core-check-20261012` conclusion is superseded.

`tools/avatar/lib/body-core.mjs` generates a closed inner bodysuit from hips to jaw. Fourteen fitted rings, outward-facing triangles, smooth normals, and interpolated spine-to-head skin weights let it move with the existing skeleton. The builder merges its 896 triangles into the existing opaque black bodysuit material. The model retains eight material primitives, 67 bones, and 15 animation clips, with no added textures or draw calls. Ring dimensions live in `BODY_CORE` in `avatar.config.mjs`.

## Verification and deployment

The neck and stomach regression tests failed against the old model, then passed against the repaired model. Each casts rays at 30-degree intervals against the bodysuit alone, preventing hair or the jacket from masking a gap. The mesh unit test checks closed edges, outward winding, normals, and skin weights.

All 140 JavaScript tests passed with zero failures or skips against the deployed GLB: 54 stage tests and 86 avatar tests. Visual checks covered full rotations of the neck, stomach, and exposed inner core; 18 animation poses; and 12 full-body views including above and below. Authenticated test-account checks on the local and public site verified loading, 2D/3D toggling, reload, and absence of browser errors. Browser rendering used Brave with SwiftShader; physical iPhone Safari was not independently tested.

The model was built to `assets/build/dev/core-fix.glb`, verified, then atomically copied to `assets/build/klukai.glb`. The container and authenticated local/public downloads matched the deployed checksum. Reload the app to load the repaired model.

- GLB SHA256: `f5df44194eed02574341d4ec792a668b1e09284a99d4ec2867400bc3125ce03a` (2,074,572 bytes).
- Viewer SHA256: `5b99f2305bc8cb28f1c206b6276c10669e46a5aa89de3715fb5952fe751e5bb1` (`web-build/companion/avatar3d.js`).
- Evidence: `.claude/look-work/neck-core-fix/`, including contact sheets, before/after screenshots, live screenshots, verification scripts, and reports.
- Previous model: `assets/build/rollback/pre-neck-core-20261012-093142.glb` (contains the gaps).

## Saved release and restoration

The private restore bundle is on Amarillo at `/home/jalsarraf/.local/share/klukai/releases/avatar-v2026.10.12/`. `restore.tar.gz` preserves the approved GLB, deployed `web-build`, source models, textures, verification evidence, and test log. `SHA256SUMS` identifies the archive; `files.sha256` verifies its contents. `source.bundle` preserves the tagged Git history. These personal-use model assets remain outside Git and GitHub release attachments.

To recover this exact appearance, verify `SHA256SUMS`, extract the archive into a staging directory, and verify `files.sha256` there. Restore its paired `web-build` and `assets/build/klukai.glb` through the normal deployment paths; atomically replace the model after validation. The spring viewer and hair-chain model must stay together. The Git tag restores the builder and viewer source; the archive restores the exact deployed bytes.

For future development, build with `AVATAR_OUT=assets/build/dev/<name>.glb`. A plain build overwrites the live model. Serialize rendering/build work with `flock .claude/look-work/render.lock`, respect the 70 C limit, and keep this approved checkpoint intact. Repeat the neck/stomach tests and rotations before replacing it.

```sh
AVATAR_TEST_GLB=/home/jalsarraf/git/klukai/assets/build/klukai.glb \
AVATAR_TEST_W2_GLB=/home/jalsarraf/git/klukai/assets/build/klukai.glb \
make test-js
```
