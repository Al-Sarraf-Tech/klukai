# Avatar eyes and Retro 64 removal

Owner goal: remove Retro 64 and fix the eyes on every added full-size appearance, preserving the first approved Blazing Star.

Execute inline. Keep the deployed viewer and default GLB byte-identical. The fix belongs in `tools/avatar/import-garf.py`; remove the retired model from `docker/core/app/avatar_models.py`. Builds and screenshots use the render lock and stay below 70 C. Private model assets never enter Git. Retain the personal/noncommercial notice.

- [x] Reproduce on the deployed Dorm model and isolate its eye materials. The opaque Eyeblend blocks the green iris; the fully transparent source Eye Shadow becomes a white bar. The working default uses additive `overlay` and `face` roles and omits the shadow helper.
- [x] Add failing asset regressions for all five Garf models and a route regression denying Retro 64 even if an old binary remains.
- [x] Correct conversion, rebuild all five into explicit dev paths, verify green irises/catchlights and blink from multiple angles.
- [x] Verify a saved Retro 64 preference recovers to the original default when the catalog no longer lists it.
- [x] Deploy verified variant files and backend registry, archive the retired live binary, verify live model bytes and appearance picker using the test account.
- [x] Save fresh screenshots to the owner's Nobara Screenshots folder, document evidence, commit/sync, and preserve original release pins.

Review focus: deleting the transparent helper must preserve eyelid shape keys; highlights must not become opaque; every Garf model shares the correction; the original model/viewer and noncommercial notice must stay unchanged; stale Retro preferences must recover without a stuck load or lost avatar.

Execution evidence: all five rebuilt assets pass the regression and multi-angle blink checks; the Sol 6.1 review confirms identical decoded face blink arrays. Fixed the saved Retro startup race identified by the review. Live local/public test-account checks pass for the six remaining installed models, retired model404 and preference migration. Default and viewer SHA256s remain unchanged. All179 tests pass. Release and screenshot evidence is recorded in `docs/handoff/2026-10-12-avatar-eye-fix.md`.
