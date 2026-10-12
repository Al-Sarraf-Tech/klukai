# Avatar eye correction and Retro 64 removal

The owner requested removal of Retro 64 and corrected eyes on the five added Garf appearances, excluding the first approved Blazing Star. The five corrected models are deployed. The original default GLB and shared 3D viewer are byte-identical to the preceding approved release.

## Root cause and correction

The Blender source’s `Klukai Eyeblend` is an eye-highlight layer. The first converter tagged it as opaque cloth, covering the authored green iris texture. A second material, `Eye Shadow`, is fully transparent in the source shader, but conversion replaced it with an opaque Principled surface, creating white bars across the eyes. Isolating the two materials independently reproduced both defects in the deployed Dorm model.

`import-garf.py` now assigns `overlay` to the highlights and `face` to the irises, matching the original avatar’s existing viewer contract. It deletes only faces using the transparent helper material, preserving the mesh vertices and shape-key arrays. All five variants were rebuilt from the same checked source and retargeted offline. The 15 motion clips remain intact. No viewer shader changes were needed.

Retro 64 is absent from the allowlisted catalog and its endpoint returns 404 even if an old file exists. The previous live binary was moved outside the served asset directory into private rollback evidence. At startup, a saved Retro 64 preference migrates to `default` before prefetch or wake; the saved 3D mode, music and voice settings remain intact.

## Verification

- Eye-asset regressions failed on all five old models, then passed on all five rebuilt files.
- The route regression first served retired bytes (200), then correctly returned 404 after removal; all 25 route tests pass.
- All 63 companion and 91 avatar tests pass, with no skips: 179 tests in total. Ruff and diff checks pass.
- Fresh browser close-ups verify visible green irises/catchlights and full blinking at -35°, 0° and +35° on every corrected model. Pixel checks detect the irises when open and their disappearance when closed; image review confirms the result.
- Sol 6.1 reviewed the fix and caught the saved-preference startup race. After migration, actual app wiring handles both delayed and failed catalogs without requesting Retro 64 or falling back to portrait. Decoded blink POSITION arrays match the old models exactly: 6,768 components and zero maximum delta for every outfit.
- Authenticated `claude` test-account checks passed on localhost at phone dimensions and the public desktop site: all six installed model hashes match, Retro 64 is absent/404, saved Retro preferences become default in 3D, selection/toggling/reload work, and no page errors occurred. Test tokens are revoked afterward.

Evidence: `.claude/look-work/eye-fix/`. Renders use Brave/SwiftShader; physical mobile GPU performance is not newly tested. The personal/noncommercial notice and source attribution remain visible.

## Deployed files

| ID | Bytes | SHA256 |
| --- | ---: | --- |
| `klukai_garf` | 2,011,220 | `40b301ffe5bf2205fac1d0b9ed30781e939876481ac8a575bcf39f398bf4c23e` |
| `garf_dorm` | 1,681,316 | `68c56e279b49b6f6176e77fbbf9f5ed9537d5a40edc22f0a74d152f082fb40aa` |
| `garf_speed_star` | 2,141,568 | `dd0f3c6ac345b577c0f57b705991d70848661a4704eb2382b14de895f7d572e7` |
| `garf_astral_luminous` | 2,700,480 | `19b6458af5ff3dfca9dc5bb5a115c43237e3a75681d4a90d3ad499a6d75f58dd` |
| `garf_cerulean_breaker` | 2,813,960 | `ad35f3faf015d4c06511e2e931791c4da4b85a96b8b720ca812fd27319a32dc5` |

Preserved default SHA256: `f5df44194eed02574341d4ec792a668b1e09284a99d4ec2867400bc3125ce03a`.
Preserved viewer SHA256: `87965b98d19da6c374c60720b2cc2407f64b9cf6fdbc640f246d27c6b1961077`.

## Recovery

Release tag: `avatar-eyes-v2026.10.12`. Private backup: `/home/jalsarraf/.local/share/klukai/releases/avatar-eyes-v2026.10.12/`. It contains the deployed default/variant bytes, web build, verification evidence and Git source bundle, with checksums. Source model acquisition is unchanged and remains in the earlier `avatar-looks-v2026.10.12` bundle. Restore the tagged backend and web code with matching model files. The original `avatar-v2026.10.12` and preceding appearances tags remain intact.

Fresh owner screenshots are on Nobara at `/home/jalsarraf/Pictures/Screenshots/Klukai-eyes-fixed-2026-10-12-132331/`: 33 images including `00-EYES-before-after.jpg`, six-model comparison, full rotations, phone/desktop screenshots and open/closed eye sheets. The delivered files are verified by full SFTP readback hashes. Backend image: `sha256:0eb5870424a6dc1da597116762336b004513f4ed87c98cb5d6e57e562dbe6831`.
