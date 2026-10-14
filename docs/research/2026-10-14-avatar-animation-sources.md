# More animations for Klukai: sources and adoption path

Research date: 2026-10-14. The owner requested more zoom and animations, then chose **everyday and playful** movement. This report covers animation sourcing and wiring. Camera limits and production acceptance belong to the implementation record.

## Recommendation

Use selected clips from **Mesh2Motion's human add-on library**, retargeted locally to Klukai's existing skeleton. It offers ordinary gestures, short dances, and quiet idle movement through direct public downloads. The project declares its art assets CC0 and its application code MIT; it provides Blender source files. No account, model upload, payment, or generative API is needed for this route. [Official project](https://mesh2motion.org/), [pinned license declaration](https://github.com/Mesh2Motion/mesh2motion-app/blob/653a9698f5f315523072f1c3ff3496100401317b/README.md#licenses).

The implemented selection is Greeting, Nod, Confused, Bow, Charleston, Groove, Cheer, and Listening. The two dances require explicit selection; Listening also joins the passive scheduler. The menu offers these eight alongside eight existing reactions. Source clip names were checked against rendered poses on the actual default and GARF models. See the [implementation and deployment record](../handoff/2026-10-14-zoom-motion-library.md) for verification and publication status.

## Acquired evidence

The following downloads came directly from the official `Mesh2Motion/mesh2motion-app` repository at commit [`653a9698f5f315523072f1c3ff3496100401317b`](https://github.com/Mesh2Motion/mesh2motion-app/commit/653a9698f5f315523072f1c3ff3496100401317b), whose committer timestamp is **2026-10-06T21:48:37Z**. The commit date is a repository snapshot date, not a claim about when every animation was created.

| Artifact | Bytes | SHA256 |
| --- | ---: | --- |
| [human-addon-animations.glb](https://raw.githubusercontent.com/Mesh2Motion/mesh2motion-app/653a9698f5f315523072f1c3ff3496100401317b/static/animations/human-addon-animations.glb) | 5,292,804 | `a0d64d555e0d492026b72d58bf8e16c5e86779295f9093e376dcc001915c2c95` |
| [human-base-animations.glb](https://raw.githubusercontent.com/Mesh2Motion/mesh2motion-app/653a9698f5f315523072f1c3ff3496100401317b/static/animations/human-base-animations.glb) | 5,656,648 | `406eb0a8dc4ab366e623b79b6e3005a4951392e1bda78ae39c1099d31147733c` |

Both files were saved under `/tmp/klukai-animation-sources/`. The sibling `.glb.json` files record their source URL, hash, node names, clip names, and durations. `README.md` and `LICENSE-CC0.MD` were fetched from the same pinned commit. These temporary paths are research evidence, not production asset locations.

Direct inspection of the GLB JSON found **75 add-on clips and 87 base clips**, ordinary glTF 2.0, and no declared extensions. Each file has one 66-joint skin and 68 nodes. Bone names follow the same UE-style scheme: `pelvis`, `spine_01` through `spine_03`, `neck_01`, `head`, `clavicle_l`, `upperarm_l`, `lowerarm_l`, `hand_l`, `thigh_l`, `calf_l`, `foot_l`, and `ball_l`, with right-side and finger counterparts. This is file inspection, not a promise that the clips already match Klukai's Mixamo rig.

### Exact clips versus proposed labels

Durations below are the maximum animation input accessor times in the downloaded GLBs. They should be checked again after baking and trimming.

| File | Exact source clip | Seconds | Proposed use; requires rendered acceptance |
| --- | --- | ---: | --- |
| Add-on | `Greeting` | 4.833 | Friendly greeting; call it a wave only if the rendered gesture supports that label |
| Add-on | `Head Nod` | 0.875 | Brief acknowledgement |
| Add-on | `Confused` | 2.917 | Puzzled response; do not assume it is a shrug |
| Add-on | `Bow` | 3.750 | Polite acknowledgement |
| Add-on | `Dance Charleston` | 2.333 | Short playful dance candidate |
| Add-on | `Dance Reach Hip` | 2.542 | Alternative playful dance candidate |
| Add-on | `Dance Body Roll` | 6.500 | Longer explicit interaction, if it suits her character |
| Add-on | `Victory Fist Pump` | 2.250 | Celebration |
| Add-on | `Idle Listening` | 1.708 | Quiet passive candidate |
| Add-on | `Idle_Subtle` | 2.583 | Quiet passive candidate |
| Base | `Dance_Simple` | 1.250 | Compact alternative dance |
| Base | `Idle_FoldArms` | 2.500 | Posture variation, after checking arm/clothing intersections |
| Base | `Idle_ShakeOff` | 2.500 | Occasional fidget |

No clap clip was verified in these two files. Do not advertise clap, wave, or shrug merely because those were useful search categories.

## License and provenance distinctions

The selected add-on source is covered by the pinned project's CC0 declaration and [CC0 license file](https://github.com/Mesh2Motion/mesh2motion-app/blob/653a9698f5f315523072f1c3ff3496100401317b/LICENSE-CC0.MD). The separate source-art repository, inspected at commit `c5b0b6c821dbf2fa1dfe49590a6f431fd9f707ce`, contains corresponding files such as `rigs/human/animation-human-greeting.blend`, `animation-human-head-nod.blend`, `animation-human-confused.blend`, `animation-human-idle-listening.blend`, and `animation-human-idle-subtle.blend`. [Pinned source-art repository](https://github.com/Mesh2Motion/mesh2motion-assets/tree/c5b0b6c821dbf2fa1dfe49590a6f431fd9f707ce).

Some Mesh2Motion human animations originate with Quaternius. Its source-art repository retains a separate [Quaternius CC0 notice](https://github.com/Mesh2Motion/mesh2motion-assets/blob/c5b0b6c821dbf2fa1dfe49590a6f431fd9f707ce/CC0-packs/Quaternius/License.txt). Keep original creator/source attribution in the asset manifest even where attribution is optional under CC0. CC0 permits modification and commercial reuse without permission; it does not warrant unrelated rights. [Creative Commons' CC0 explanation](https://creativecommons.org/publicdomain/zero/1.0/).

**Do not extend the general CC0 wording to every unrelated file in the repository.** The app also has a `static/animations/CarnegieMellonAnimations` directory whose readme points to a third-party CMU conversion. CMU permits use in products but prohibits selling its motion data itself, including converted data. The selected add-on does not require that directory or the separate `human-mocap-animations.glb` bundle. Avoid both for this release unless their provenance is assessed separately. [Pinned directory readme](https://github.com/Mesh2Motion/mesh2motion-app/blob/653a9698f5f315523072f1c3ff3496100401317b/static/animations/CarnegieMellonAnimations/readme.txt), [CMU's published terms](https://mocap.cs.cmu.edu/).

## Concrete wiring plan

The following adoption criteria informed the implementation. Publication evidence is recorded separately in the linked handoff.

1. Extract only the selected clips and necessary source skeleton into a small motion asset. Keep the source URL, commit, hash, original clip names, creator, and license in a manifest. Do not replace Klukai's mesh or textures.
2. Retarget to the actual loaded Klukai skeleton using an explicit target-to-source bone map. Correct rest-pose orientation and hip scale; verify left/right limbs and finger orientation. Matching names alone is insufficient evidence of correct motion.
3. Prefer a bake to Klukai's rig when that avoids repeated retargeting work. If retargeting in the browser is required for variant rigs, perform it once per rig/pack and cache the resulting clips. Bound optional loading time and preserve existing animations when the pack fails.
4. For standing gestures, constrain horizontal root travel without flattening useful vertical hip motion. Check feet against the ground, limb reach, clothing collisions, and the return to idle. Do not turn a moving dance into a stationary one without checking the resulting foot sliding.
5. Register accepted one-shot clips with the existing reaction controller, using finite playback and crossfades. Add only accepted quiet clips to passive selection. Preserve speech/thinking priority, interaction cooldowns, camera-drag guards, visibility pause, and reduced-motion behavior.
6. Verify on the actual installed models and desktop/phone layouts: visible pose change, bounded root displacement, stable transitions, no stuck pose, no speech interruption, and no automatic reaction caused by pinch/drag. Keep the original embedded clips as the failure path if loading or retargeting fails.

Three.js documents `SkeletonUtils.retargetClip`, including target-to-source name mapping, hip selection/influence, scale, trimming, and local offsets. Its `AnimationAction` API supplies one-shot playback and crossfades. These APIs establish an available mechanism; they do not prove a particular pair of rigs retargets correctly. Use the repository's pinned Three.js version and inspect its actual behavior. [Retargeting API](https://threejs.org/docs/pages/module-SkeletonUtils.html), [action playback and blending](https://threejs.org/docs/pages/AnimationAction.html).

## Alternatives

| Source | Verified access and terms | Fit for Klukai |
| --- | --- | --- |
| [Quaternius Universal Animation Library](https://quaternius.itch.io/universal-animation-library) and [Library 2](https://quaternius.itch.io/universal-animation-library-2) | CC0; free Standard archives; GLB/FBX exports; root-motion and stationary variants. The creator lists Mixamo compatibility. UAL2's latest listed update is 2026-09-28. | Strong independent source for further gestures. Retarget and verify locally. The advertised 120+/130+ totals include paid versions, so they are not verified free-Standard counts. UAL1 lists Pro at $9.99 and Source at $14.99; UAL2 currently lists Source at $14.99. No paid versions were acquired. |
| [Adobe Mixamo](https://helpx.adobe.com/creative-cloud/faq/mixamo-faq.html) | Adobe says it is free with an Adobe ID and permits royalty-free use in personal/commercial projects. Help pages remain dated 2021-09-14. | Large practical selection, but account-gated. Adobe permits starting from a stock character, so animation acquisition need not upload Klukai. Apply and download on that rig, then retarget locally. [Official workflow](https://helpx.adobe.com/creative-cloud/help/animate-characters-mixamo.html). Do not treat it as CC0 or a raw animation redistribution license. |
| [100STYLE — Ian Mason](https://www.ianxmason.com/100style/) | CC BY 4.0; individual BVH downloads; 60 fps data on a 28-bone skeleton, with trim-frame metadata. Creative/commercial work needs attribution. | Useful for distinctive passive body language and locomotion later. More cleanup than a curated gesture pack; preserve the required credit. |
| [CMU Graphics Lab mocap](https://mocap.cs.cmu.edu/) | Free use and inclusion in products; raw/converted motion resale prohibited. CMU warns that hands/toes can be noisy and fingers were not captured. | Broad motion variety but a weaker first choice for close-up everyday gestures; requires retargeting, cleanup, and provenance care. |
| [Kenney Animated Characters Protagonists](https://kenney.nl/assets/animated-characters-protagonists) | CC0 character pack; author provides a [character/animation importing guide](https://kenney.nl/knowledge-base/game-assets-3d/importing-characters-and-animations). | Useful stylized baseline. Not selected over the verified Mesh2Motion add-on for this request; exact desired gesture coverage was not verified. |

Mixamo's separate [Additional Terms, effective 2021-06-23](https://wwwimages2.adobe.com/content/dam/cc/en/legal/servicetou/Mixamo-Addl-Terms-en_US-20210623.pdf), contain an AI/ML-use restriction. An [Adobe-hosted community FAQ from 2022-09-29](https://community.adobe.com/questions-696/mixamo-faq-licensing-royalties-ownership-eula-and-tos-589400?lang=en) distinguishes finished creative projects from raw-file distribution. The latter is community guidance rather than a replacement for the terms. These differences reinforce using the selected CC0 files for new reusable motion assets.

## Coverage and caveats

This research branch used native web search/fetch, official creator and project pages, official GitHub metadata and pinned files, plus direct GLB inspection. It did not create accounts, upload Klukai, make purchases, run paid generation, or download full source repositories. Acquisitions were the two approximately 5 MB runtime GLBs and small license/readme files.

Native results were cross-checked against creator documentation and actual assets, rather than treating search snippets or marketing clip totals as implementation evidence. The Quaternius viewer is a Godot application and its exact Standard clip inventory was not extracted. No visual quality claim for the retargeted Klukai motions follows from the metadata inspection alone.

Brave/search-mcp/context7 were not directly callable in this branch. Grok/X was not called under the no-provider-call research constraint; social announcements were unnecessary to establish the selected files' current contents and stated licenses.

On 2026-10-14, the root agent independently queried both Brave and SearXNG through Klukai's existing scoped MCP configuration for “Mesh2Motion CC0 humanoid animations wave dance retarget Mixamo”. Both returned the official Mesh2Motion website; Brave additionally surfaced project documentation and third-party demonstrations. SearXNG also returned an unrelated Lisbon tourism result, which was discarded. These searches corroborate discovery; the licensing and exact clip claims above come from the official sources and inspected files, not third-party snippets. The complete nonsecret responses are preserved in `/home/jalsarraf/Documents/Codex/2026-10-14/klukai-zoom-motion-library/research-backends.json`. No material source disagreement changed the selected add-on pack.
