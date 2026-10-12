# Personal avatar variants implementation plan

> Execute inline using the existing outfit integration research as the design brief. Owner requested all three candidates and an explicit noncommercial notice. Source access remains a prerequisite for each candidate; unavailable assets must never be presented as installed.

**Goal:** Add privately hosted 3D appearances from the three researched sources, preserving the pinned default, with visible personal/noncommercial use text and source attribution.

**Architecture:** An authenticated allowlisted appearance catalog selects separate private GLBs. Browser-local appearance selection is distinct from her canonical wardrobe, since fan renditions are not canonical outfits. Key prefetches by appearance and replace the renderer only when a newer selection succeeds. Failed or superseded loads retain/dispose resources correctly.

**Spec:** `docs/research/2026-10-12-3d-outfit-integration.md` (personal-use followup and proposed integration).

**Constraints:** No new spending. No assets committed. No default GLB changes. Explicit text: “For personal, noncommercial use only.” Preserve original third-party licenses; the notice describes this application's intended use. Never imply publisher approval. Do not substitute the software-excluded official Global downloads. Builds/renders serialized with `.claude/look-work/render.lock`.

## Work

- [x] Acquired and inspected the Garf and GFL64 sources privately; recorded commit/checksum and terms.
- [ ] Obtain the VRoid source after owner Pixiv sign-in; this download remains unavailable.
- [x] Convert acquired sources offline to standalone GLBs in `assets/build/dev/variants/`. Give models standard bone names/rest orientation where possible, retarget suitable motion, preserve opaque coverage. Do not apply the Blazing Star body-core patch to other silhouettes. Check each through 360 degrees and motion poses before installing.
- [x] Add `docker/core/app/avatar_models.py` for fixed appearance definitions and availability, plus authenticated `/api/avatar/models` and allowlisted `?model=` in `routes_portrait.py`. Test unauthorized access, unknown/path traversal IDs, unavailable files, per-model ETags and private caching in `test_routes_portrait.py` before implementation.
- [x] Add appearance selection and credits in the companion stage, a latest-request-wins loader, and keyed prefetch in `avatar3d.js`. Persist the appearance locally. Test reverse-order completion, failed replacement, toggling/reload, cleanup, and default compatibility. Keep canonical wardrobe semantics intact.
- [x] Run focused server/JS tests, then existing avatar/stage tests. Inspect desktop/mobile using test account and each available model. Deploy only verified assets/code; save source provenance and concrete blockers. Do not claim all three installed while any remains unavailable.

## Review focus

Stale async loads must never replace the latest choice. A failed model must not remove the working avatar. Invalid identifiers must not read arbitrary files. Appearance selection must not spoof canonical outfit unlocks. The intended-use notice must not replace CC BY-SA with an NC license.

## Execution ledger

2026-10-12: Source acquisition started. GFL64 source commit `148c3b7bcf2b7dac751e267b428b4afa389208a8`; Blender 4.1.1 official binary checked against published SHA256 `ab2ea3fe991601a5e6bd2cda786ecaa919c0b39e0550e59978b5d40270c260d3`. VRoid requires Pixiv sign-in; owner asked about existing account. Open3DLab page blocked by WAF; Luna investigating public mirrors. Approved live GLB has not changed.

2026-10-12 implementation: Five Garf outfits and one GFL64 rendition converted and inspected through full rotations. Five full-size rigs retain all 15 clips and blinking; Retro 64 retains creator idle. Astral's missing accessory bone references were repaired after motion inspection; export now rejects unweighted vertices. Sol 6.1 reviewed the implementation and verified cancellation/toggle fixes in the actual viewer. All 24 route, 63 companion and 86 avatar tests pass. The default checksum and original release tag are unchanged. The visible noncommercial notice and creator/source attribution are implemented. See the new appearance handoff for deployed checksums and final live validation.

Live validation passed for all seven available appearances on localhost phone dimensions and the public desktop site; no page errors, correct authenticated bytes, toggling and persistence. Temporary test token revoked. Only VRoid acquisition/conversion remains pending.
