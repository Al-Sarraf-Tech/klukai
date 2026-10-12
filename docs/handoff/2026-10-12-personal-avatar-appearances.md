# Personal appearance release

This release adds six private appearances through the companion’s **Look** button. The stage and selector display **“For personal, noncommercial use only.”** Original third-party terms remain applicable. This release does not assert publisher approval.

## Available appearances

| ID | Creator/source | Bytes | Clips | SHA256 |
| --- | --- | ---: | ---: | --- |
| `hk416_gfl64` | Player Legend / GFL64 | 137,060 | 1 | `ac7ac1ce08e99672189785eea84334d042cc795496444c96f81e5aaa814e22b3` |
| `klukai_garf` | Garf / Open3DLab | 2,015,440 | 15 | `3f6465c2b6d6291f1b4f423b0e53b85234d4bda1c0aa2c934a3c421b4824061d` |
| `garf_dorm` | Garf / Open3DLab | 1,685,528 | 15 | `42fdfbbf8ed9322b990e400e5ee543bc80f0120eb5ee4a467de8964b4168724a` |
| `garf_speed_star` | Garf / Open3DLab | 2,145,784 | 15 | `8ff9118821ba23d17c441d53b8fb59ac410a06118ba23f1a2256a8e2fa819092` |
| `garf_astral_luminous` | Garf / Open3DLab | 2,704,452 | 15 | `1b8829c5fa045f90ed3d88d550c35f59a9e08d52d8b8b45e44d780b5df96642d` |
| `garf_cerulean_breaker` | Garf / Open3DLab | 2,817,900 | 15 | `f59625a56713bf816c78153c8da7f0d45ede9e1136556b2c293377843387ab93` |

VRoid HK416 by こな麦茶 is listed but disabled until its Pixiv-gated download is available. The owner was asked about an existing Pixiv account. It has not been converted or installed. Neither Indigo Oath nor Immaculate Service was in the acquired Garf source.

## Source and conversion

[Research and permissions](../research/2026-10-12-3d-outfit-integration.md) records the source links and terms. Garf’s private source file SHA256 is `a9f6609313c0a32269aaf5cd2002d34267610a2e6645eece6e8f512a8d091473`; GFL64 source commit is `148c3b7bcf2b7dac751e267b428b4afa389208a8`. Garf uses uploader-selected CC BY-NC-ND 4.0; private adaptations stay private. Player Legend’s original model is CC BY-SA 4.0. No model files enter Git or public release attachments.

`import-garf.py` exports five complete clothing sets with canonical joints and blink. `retarget-variant.mjs` adapts the default’s 15 animations by rest-pose rotation. The source’s Astral Luminous magazines, zipper and belt fitting referenced absent bones: these now bind to the thigh/spine attachments, and export rejects unweighted vertices. `import-gfl64.py` keeps its creator’s idle, converts Fast3D materials, removes wing-cap geometry and supplies full-body camera metadata.

## Verification

All 24 route tests, 63 companion tests and 86 pinned-avatar tests pass. Ruff and diff checks pass. Sol 6.1 reviewed the code; the resulting cancellation/toggle fixes passed real browser checks with delayed GLTF parsing and shader compilation. No important review findings remain.

Offline Brave/SwiftShader renders cover 12 views per model, every one of the 15 clips for the full-size models, and blink/neck close-ups. Astral was rendered again after the attachment fix. Desktop and phone framing was tested; physical iPhone Safari and real mobile GPU performance remain untested. Cloth and hair can intersect in deep crouch/sleep poses; no new cloth physics or lipsync is promised. Evidence and scripts: `.claude/look-work/skin-integration/`.

## Default and restoration

The approved default remains byte-identical: SHA256 `f5df44194eed02574341d4ec792a668b1e09284a99d4ec2867400bc3125ce03a`. Its `avatar-v2026.10.12` tag and private restore bundle remain intact. New files live under `assets/build/variants/` with fixed names from `avatar_models.py`, served only by authenticated model routes. Choosing the first Look entry returns to the approved model.

The new code retains the working avatar on failed replacements, disposes superseded loads and remembers the selected appearance locally. Missing files return 404 and remain disabled in the catalog.

## Live release and backup

Deployed on 2026-10-12 to `https://klukai.appnest.cc/app/companion.html`. The companion-core image is `sha256:7fa293931a82d5559b0a52bf9147774cf20a0bfbd83e36dc506f67c79253806f`. Its health check passed. The web build passed its third-party-origin audit; only the six changed companion web files were atomically installed from that build.

Authenticated live checks used the `claude` test account, with its temporary token revoked afterward. Local phone-sized and public desktop views both verified all seven available models against local SHA256s, unauthenticated rejection, VRoid 404, every selection, one active canvas, 2D/3D toggling, and selection persistence after reload. Neither view reported a page error. Live evidence is in `.claude/look-work/skin-integration/live/`.

Release tag: `avatar-looks-v2026.10.12`. Private backup directory: `/home/jalsarraf/.local/share/klukai/releases/avatar-looks-v2026.10.12/`. It holds `restore.tar.gz`, the variant manifest, SHA256 checksums and a Git source bundle. Restore the verified web build, default GLB and variant directory from this archive together with the tagged companion-core code. Source files and conversion evidence are included; the large Blender executable is excluded and can be reacquired by its recorded official checksum. The older `avatar-v2026.10.12` checkpoint remains a separate fallback.
