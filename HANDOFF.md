# Project handoff

## Current approved release

The owner approved the eye-corrected appearances on 2026-10-12 as the new release.
Pinned tag: `avatar-eyes-v2026.10.12`, application commit `9b4ee8793c78e1603e1e83c58a9ffeb845685503`.
This is the current recovery baseline: the original Blazing Star plus five Garf
appearances with green eyes and working blinking, with Retro 64 removed.
See [the verification, checksums, and private restore bundle](docs/handoff/2026-10-12-avatar-eye-fix.md).
Preserve this release when making future appearance changes.

## Earlier approved avatar checkpoint

The 3D neck and stomach repair is deployed and owner-approved as of 2026-10-12.
Release tag: `avatar-v2026.10.12`. See
[the repair, verification, and restore notes](docs/handoff/2026-10-12-approved-avatar-release.md).
This completes the model-finish objective recorded in the older bus snapshot below;
preserve the approved appearance as the baseline for future work.

## Personal appearances

Five additional looks are deployed through the companion’s **Look** button, with
an explicit personal/noncommercial notice. The approved default above remains
unchanged. VRoid is still pending its Pixiv sign-in/download; it is disabled in
the picker. See [sources, conversion, verification and recovery](docs/handoff/2026-10-12-personal-avatar-appearances.md).

## Eye correction and Retro 64 removal

The five Garf appearances now preserve their green irises and catchlights.
The opaque conversion of two transparent eye layers was corrected; the original
approved Blazing Star and its viewer remain byte-identical. Retro 64 is removed,
and saved Retro preferences migrate to the original default in 3D mode.
See [the eye-fix verification and release notes](docs/handoff/2026-10-12-avatar-eye-fix.md).

<!-- bus-handoff:start -->
Project: amarillo/klukai
Checkout: /home/jalsarraf/git/klukai
Active task: 51f4e055c682 [pending]
Objective: Finish Klukai's 3D model: the owner says it is almost there. Read docs/handoff/2026-10-11-3d-model-finish.md first (in ~/git/klukai on amarillo; the assets and research exist only on amarillo). Step 0: ask the owner what still looks off (a phone screenshot if possible) and check a real iPhone/Brave via av.stats before tuning anything. Known gaps, ranked: real-device perf and look; face (SDF face shadow; no visemes, so no lip-sync); missing cap goggles and hairclips; hair (subtle angel ring, stiff shoulder curtains, calling-crouch collider); cloth (sling tone, hollow jacket seen from above); eye bones and pointer parallax. Hard rules: node build.mjs without AVATAR_OUT is a LIVE deploy; a GLB with hair chains needs the spring viewer, so roll back both together; serialise renders with flock .claude/look-work/render.lock (70 C limit); never commit assets/; commit locally (commit + git-sync), push to GitHub only on request; no spending.
Owner: unclaimed
Branch: main
Base commit: 2e82ef1
Next action: not recorded
Blockers: none recorded
Thread: 51f4e055c682; source message: 51f4e055c682
Unfinished tasks: 1
- 51f4e055c682 [pending] Finish Klukai's 3D model: the owner says it is almost there. Read docs/handoff/2026-10-11-3d-model-finish.md first (in ~/git/klukai on amarillo; the assets and research exist only on amarillo). Step 0
<!-- bus-handoff:end -->
