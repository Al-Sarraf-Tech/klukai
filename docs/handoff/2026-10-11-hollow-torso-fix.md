# Hollow torso fix

The open-jacket build previously dropped the lower-front and lower-back bodysuit islands to avoid animation poke-through. From oblique/side camera angles that left the waist shell physically open and could look hollow.

This branch keeps those two islands as the flat black bodysuit and recesses them 6 mm along averaged normals beneath the jacket. `tools/avatar/lib/inset-faces.mjs` performs the seam-safe inset; `tools/avatar/test/hollow-torso.test.mjs` covers both the geometry helper and the open-jacket configuration.

Before merge:

1. GitHub Avatar Tests must pass (`cd tools/avatar && npm test`).
2. On the host with the private source assets, build to a non-live output first:
   `AVATAR_OUT=assets/build/dev/klukai.glb node tools/avatar/build.mjs`
3. Rotate/render the dev GLB from front, rear, both sides, and ±45° views and confirm the waist is closed without jacket poke-through.
4. Only then rebuild/deploy the live `assets/build/klukai.glb`.
