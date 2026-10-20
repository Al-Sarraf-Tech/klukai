# Avatar module ownership

`../avatar3d.js` is the existing public entrypoint. Its 43 named exports and the
returned avatar/window API remain compatible. It loads no remote code: Three
and its addons still share the lazily imported pinned
`../vendor/three-avatar.min.js` (Three 0.186.1; see `tools/avatar/package-lock.json`
and `../vendor/LICENSE.three.txt`). Personal GLB assets still enter only through
the caller's authenticated `loadModel` function.

Pure policies are separate from rendering: `state.js` owns mood/clip/reaction
selection, `camera.js` owns framing/gestures/quality, `presence.js` owns look and
speech constraints, and `physics.js` owns envelope/spring math. `common.js` holds
their shared numeric helpers. Shader source/constants live in `shaders.js`.
`loader.js` retains the one shared library promise and the model cache (one
download per model id per page, shared by the warm-up and every viewer).

`runtime.js` creates one context per avatar. Renderer, scene, model, animation,
camera and disposal state have that single owner. The `runtime-*` modules
install related operations on that context rather than creating extra owners:
materials, motion, presence, springs, camera and lifecycle. Their initializers
run at the original lifecycle points; `runtime-load.js` loads the model and
`runtime-api.js` constructs the existing public/debug interface. Internal
modules depend on pure policies, not on the public entrypoint or each other.

`../stage_appearance.js` owns the catalog and transactional appearance switcher;
`../app.js` keeps session, voice and main-page wiring. Explicit live accessors
retain the app's ownership of its current avatar and conversation state.

`tools/avatar/vendor.mjs` follows local static/dynamic module imports from
`avatar3d.js` and `motion-library.js`, deduplicates cycles and rejects path or
symlink escapes. It excludes the generated vendor bundle when deriving its own
required Three exports. Do not rebuild or replace that bundle for a source
extraction.

Flutter copies this whole directory into the PWA. Immutable release manifests
must include every `avatar/*.js` module, `stage_appearance.js`, and the Pretext
source/provenance/license files; copying only the public entrypoint is
incomplete. Relative paths and same-origin cache revalidation remain in force.

The browser regression uses an isolated synthetic animated rig and real
headless Chrome. It verifies material compilation, rendered frames, springs,
state/mood, persistent camera position, and WebGL/canvas disposal. The same
literal assertions also pass the original pre-extraction avatar implementation.
