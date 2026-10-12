# Project handoff

## Approved avatar checkpoint

The 3D neck and stomach repair is deployed and owner-approved as of 2026-10-12.
Release tag: `avatar-v2026.10.12`. See
[the repair, verification, and restore notes](docs/handoff/2026-10-12-approved-avatar-release.md).
This completes the model-finish objective recorded in the older bus snapshot below;
preserve the approved appearance as the baseline for future work.

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
