# Everyday and playful motions

`everyday-playful.json` contains only the selected source skeleton and motion
channels from Mesh2Motion's CC0 human add-on pack. It contains no Klukai model,
mesh, texture, target bind pose, or private source data. The viewer retargets it
to the authenticated appearance locally and keeps the resulting clips in memory.

- Project/creator: [Mesh2Motion](https://mesh2motion.org/), with its published
  [Blender animation sources](https://github.com/Mesh2Motion/mesh2motion-assets/tree/c5b0b6c821dbf2fa1dfe49590a6f431fd9f707ce/rigs/human).
- Source commit: `653a9698f5f315523072f1c3ff3496100401317b`.
- [Original GLB](https://raw.githubusercontent.com/Mesh2Motion/mesh2motion-app/653a9698f5f315523072f1c3ff3496100401317b/static/animations/human-addon-animations.glb),
  SHA256 `a0d64d555e0d492026b72d58bf8e16c5e86779295f9093e376dcc001915c2c95`.
- License: CC0-1.0; the original notice is included as `LICENSE-CC0.txt`.
  The source URL, digest and license URL are also embedded in the JSON.
- Selected source clips: `Greeting`, `Head Nod`, `Confused`, `Bow`,
  `Dance Charleston`, `Dance Reach Hip`, `Victory Fist Pump`, `Idle Listening`.

Rebuild from the reviewed original download, using the repository's installed,
pinned avatar tooling:

```sh
node tools/avatar/build-motion-pack.mjs /path/to/human-addon-animations.glb \
  flutter_app/web/companion/motions/everyday-playful.json
node tools/avatar/vendor.mjs
AVATAR_TEST_GLB=/path/to/assets/build/klukai.glb make test-js
python3 tools/avatar/library_e2e.py --viewport all --out /tmp/klukai-motion-check
```

The builder verifies the input SHA256, extracts the required source rig, reduces
redundant keys, preserves STEP channels, and rounds JSON numbers to six decimal
places. No external files or target models are read. The output has 67 source
nodes and eight clips. Do not write into the live `assets/build` directory.

To add more motions, review their original source and license, add accepted
entries to `MOTION_CATALOG` in `motion-library.js`, and rebuild. The current
builder accepts only the pinned add-on; another source needs an explicit new
provenance check and bone mapping. Retargeting applies world-space rotation
deltas relative to both rigs' rest poses, scales hip motion, limits horizontal
travel, and corrects foot contact for each appearance's proportions. This is
not full-body collision solving or foot IK: visually inspect limbs, clothing,
feet, and transitions on each supported appearance before accepting a clip.

Only `Idle Listening` is added to passive selection. Both dances have
`auto: false`, so they require explicit selection. Speech, state priority,
reduced motion and existing cooldowns remain authoritative. If fetching or
retargeting fails, the original embedded reactions remain available.

After changing source assets, refresh the cache hashes in dependency order:
motion JSON → motion module; vendor and motion module → avatar module;
avatar → app; app and CSS → HTML. Publish dependencies before HTML.

See `docs/research/2026-10-14-avatar-animation-sources.md` for the comparison
with Quaternius, Mixamo, 100STYLE, CMU, and Kenney.
