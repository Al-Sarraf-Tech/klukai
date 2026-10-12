# Klukai 3D outfit integration research

Research date: 2026-10-12. Requested by the owner, with a Luna research agent.

Klukai has five named alternate GFL2 outfits beyond the approved Blazing Star appearance, plus dorm wear. Complete models exist for several of them. The companion can support multiple outfits, but the existing texture sets and misleading OBJ group names are insufficient for a complete swap. The implementation followup below records the privately converted models and the remaining VRoid sign-in requirement.

## Outfit sources

The [Klukai outfit gallery](https://www.iopwiki.com/wiki/Klukai) lists the six named outfits. The [official art portal](https://gf2exilium.sunborngame.com/main/art), inspected in a real browser, supplies MMD archive entries for the default, dorm wear, Speed Star, Astral Luminous, Indigo Oath, and Immaculate Service. All six public CDN archive URLs returned HTTP 200 to HEAD requests without login. Archive contents were not downloaded or inspected; an available archive does not yet establish a working converted model.

| Outfit | Appearance | Model evidence |
| --- | --- | --- |
| Blazing Star | Current purple and grey tactical jacket | Approved local GLB; official Default archive also exists |
| Speed Star | White racing outfit | Official archive, 22,263,445 bytes |
| Astral Luminous | Alternate tactical outfit | Official archive, 30,355,817 bytes |
| Cerulean Breaker | Swimsuit | [Community MMD listing](https://www.aplaybox.com/details/model/JKkaOgZkQBHi); no matching entry found in the five pages of the global official portal |
| Indigo Oath | Bridal outfit | Official archive, 146,927,485 bytes |
| Immaculate Service | Maid outfit | Official archive, 38,237,462 bytes |
| Dorm wear | Casual dorm clothing | Official archive, 74,677,484 bytes; separate from the six named costumes |

These archive sizes describe source packages, not estimated mobile GLB sizes. These official archives were not downloaded. No purchases or new accounts were made.

The official portal's **Usage Guidelines**, section 2.7, limit the supplied models to noncommercial fan videos and explicitly exclude interactive media and software. This corrects the earlier README's uncertainty about the restrictions. The global portal's downloads do not require the AplayBox login used by the separate Chinese distribution site. Community uploader terms and the rights to the underlying assets require separate review; a free download alone does not answer that question.

An additional [Open3DLab model by Garf](https://open3dlab.com/project/9ed8198a-df22-46db-81dd-676d0d6da019/) lists a 206.07 MB `Klukai_V1.blend`, five skins, and rigged weapons. It specifies Goo Engine 4.1. Subsequent inspection opened it successfully in ordinary Blender 4.1.1 with Python autoexec disabled. Its five outfits are Blazing Star, Dorm, Speed Star, Astral Luminous, and Cerulean Breaker; it contains neither the bridal nor maid release. The uploader selects CC BY-NC-ND 4.0. See the private-conversion details below.

## Personal use followup

The owner clarified that the companion is for personal use. Permissions must be evaluated for that scope, separately from publishing a converted model or offering a public app.

The strongest explicit creator permission found is [HK416 by こな麦茶 on VRoid Hub](https://hub.vroid.com/en/characters/7421423711420001864/models/4935140573537488161). Its current settings allow other users, avatar use, alterations, and noncommercial individual use; they prohibit redistribution, corporate use, sexual acts, and violent acts. The creator describes it as an HK416 fan creation and also asks users to follow Girls' Frontline guidelines. This is a creator-made rendition, not a verified alternate outfit for our existing GFL2 mesh. The download flow requires Pixiv sign-in. No VRM was obtained; the owner was asked whether an existing account is available. It has affirmative creator permission for avatar use, not documented Sunborn approval of this particular companion.

[Player Legend's Girls' Frontline 64](https://mods.sm64coopdx.com/mods/girls-frontline-64.842/) also includes an original HK416 model and expressly licenses its models/code under CC BY-SA 4.0. It is a low-poly interpretation, and the listing separately reserves the character and sound rights to MICA/Sunborn. It is a useful creator-licensed alternative if that different appearance is acceptable.

The Open3DLab listing's CC BY-NC-ND 4.0 label should **not** be treated as a ban on all private conversions. The [license text](https://creativecommons.org/licenses/by-nc-nd/4.0/legalcode.en) section 2(a)(1)(B) permits producing and reproducing adaptations for noncommercial purposes without sharing them; section 2(a)(4) permits necessary technical format changes. This makes the pack a candidate for private adaptation within the rights its uploader holds. It does not establish the uploader's authority over underlying game assets. The later technical inspection establishes the five included outfits and viewer compatibility, separately from permissions.

The official Global MMD files remain subject to their separate video-only/software exclusion, which states no personal-use exception. No publisher-approved route for the exact GFL2 alternate skins in a private interactive companion was verified. An absent software prohibition on another creator's page is not an explicit app-use grant.

The detailed followup is saved in `.claude/look-work/skin-research-20261012/personal-use-permissions.md`. Source acquisition and implementation followed this review; their current status is recorded below.

## Local asset findings

At the start of the review, `assets/source-models/` contained one combined OBJ and 15 Mixamo FBX clips. The builder selectively removes alternate and duplicate parts. Texture filenames reference several other costumes, but there are no complete, separately validated alternate outfit builds.

The group `Cloth_1_(Speed_Star)` contains only 800 triangles, all between heights 1.357 and 1.4343 metres: a head-area part, not a racing outfit. In `Klukai_Cloth_1_(Astral_Luminous)`, 92.57% of vertices are within 2 mm of the default cloth, with median nearest distance 0.600 mm. This confirms the prior visual finding that its label cannot be trusted as evidence of a complete Astral Luminous costume. The remaining groups also need visual and topology inspection before reuse.

The six canonical outfit IDs already exist in `config/personality.yaml`. The wardrobe API, daily choice, and Flutter picker already use them. Before this implementation, `docker/core/app/routes_portrait.py` served one `AVATAR_MODEL_PATH`, and the stage's `refreshDay()` only changes the outfit label. It never replaces the loaded 3D model. The viewer also had a single unkeyed `warmModel` promise; adding model URLs alone would risk reusing the previous outfit's bytes.

## Implemented personal appearances

The owner requested all three source candidates, which include fan renditions as well as outfits. A separate **Look** selector therefore leaves canonical wardrobe/unlock semantics intact. The pinned `avatar-v2026.10.12` GLB remains the default. Both the stage and picker explicitly say **“For personal, noncommercial use only.”** This describes intended application use; it does not replace the source licenses or claim publisher approval.

- **Garf:** the public free mirror supplied `Klukai_V1.blend`, 216,079,752 bytes, SHA256 `a9f6609313c0a32269aaf5cd2002d34267610a2e6645eece6e8f512a8d091473`. Five separate GLBs preserve their complete clothing, skin and face. A clean deform rig maps to canonical names; the pinned model's 15 clips are retargeted using source/target rest rotations. Four eyelid shapes combine into blink. Source scripts are disabled; custom Goo shaders become local toon-compatible albedo materials. Output sizes are 1.69–2.82 MB. No adapted binaries are committed or publicly attached.
- **Player Legend:** source commit `148c3b7bcf2b7dac751e267b428b4afa389208a8`. The converter rebuilds Fast3D materials from source textures, removes unrelated wing-cap geometry, rebinds/renames the rig, and retains the creator's idle clip. The 137,060-byte Retro 64 rendition uses full-body framing for its large head. It has one authored animation, not the 15 full-size clips.
- **VRoid:** catalogued but unavailable until an authenticated Pixiv download supplies the source. No sign-in or new account was fabricated. The picker disables this entry.

Authenticated `/api/avatar/models` reports fixed IDs, availability, credits, source and license. `/api/avatar/model?model=<id>` uses an allowlist; missing/unknown models return 404. Files stay outside the static web root. ETags include model identity. The browser persists selection locally, keys prefetches by model, cancels obsolete work, and retains the working avatar if a replacement fails.

Conversion scripts are `tools/avatar/import-garf.py`, `import-gfl64.py`, and `retarget-variant.mjs`. The checksum-verified official Blender 4.1.1 tool is local to the ignored evidence directory. No added runtime dependency is required.

Verification evidence lives in `.claude/look-work/skin-integration/`: source inspection, output manifest, 12-view rotations, motion sheets, blink close-ups, mobile/desktop switching, and test logs. All five full-size models were sampled in all 15 clips. Testing uses Brave/SwiftShader; actual phone GPU performance and physical Safari remain unmeasured. Skirts/hair have no newly authored cloth dynamics; extreme crouch/sleep poses can intersect clothing. The original body-core patch is never transplanted to the other models.

The old README suggestion to use Three.js's bundled `MMDLoader` is stale. The pinned Three.js 0.186.1 installation does not include it; the upstream [migration guide](https://github.com/mrdoob/three.js/wiki/Migration-Guide#169--170) directs users away from the deprecated MMD modules. An offline Blender/MMD importer or separately vetted converter is required. [MMD Tools](https://github.com/MMD-Blender/blender_mmd_tools) is a candidate to evaluate, not an installed or tested dependency in this project.

## Evidence and limits

The detailed Luna report and raw checks are in `.claude/look-work/skin-research-20261012/`: `luna-research.md`, `official-vue-data.json`, `official-downloads.json`, `usage-guidelines.txt`, `local-geometry.json`, and `local-overlap.json`. Official archive requests were intercepted and aborted; subsequent HEAD requests confirmed availability without downloading their contents.

Research used native web search, source pages, local Brave/Playwright, and direct public HTTP metadata. Brave Search MCP, search-mcp, and Playwright MCP were not connected. Grok/X was not invoked because its billing could not be established under the owner's no-spending rule. After those backends failed on the Open3DLab page, one permitted prepaid 2Captcha fallback fetched public mirror metadata (reported cost zero, zero captcha solves). The later direct mirror download and browser renders are recorded above. No Grok billing or new spending was incurred.
