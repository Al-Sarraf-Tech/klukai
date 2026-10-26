// Klukai 3D avatar window: her rigged model idling and reacting while you chat.
//
// ES module, no bundler. three.js is vendored as ./vendor/three-avatar.min.js (no CDN — privacy,
// Brave shields, offline). The GLB is personal-use, game-derived content: it is
// never committed and only reaches the page through `loadModel()` (the app's
// authenticated API), never a public URL.
//
//   import { createAvatar3D, moodToState } from './avatar3d.js';
//   const av = await createAvatar3D(el, { loadModel: () => api.avatarGlb(), onReady, onError });
//   av.setMood('quietly_pleased'); av.setSpeaking(level); av.setState('greet');
//   av.setTint('#ffb3c8');            // mood rim light colour (eases over ~1.2 s)
//   av.setInsets({ top, bottom });    // CSS px of the canvas hidden under the topbar / dock
//   av.zoomBy(0.85); av.rotateBy(Math.PI / 12); av.moveBy(0.12); av.resetView();
//   av.react('salute'); // temporary; returns to her current mood/thinking state
//   av.getView(); av.setView(saved); // opts.view / opts.onViewChange persist inspection
//   av.setPaused(true); // parked behind another view: no render loop until setPaused(false)
//
// Look: two-band tinted toon (per-role shadow colours, baked AO from the GLB's
// occlusion map), face normals blended toward a sphere, a key-side rim plus a
// mood-tinted back rim, a hair "angel ring", and an inverted-hull outline drawn
// by a second SkinnedMesh per part (shared geometry, skeleton and blink morph).
// Life: look-at (camera, pointer on desktop), breathing, idle arm relax,
// speech-driven nods, blinks on gaze shifts and phrase ends, idle fidgets,
// mood hysteresis, eased camera, a contact shadow in full framing.
//
// The pure parts (moodToState, STATE_CLIPS, cameraFraming, lookSplit, …)
// import nothing heavy, so they run under `node --test` (tools/avatar/test).
// three.js is loaded lazily.

// Stable public entrypoint; pure policies stay lightweight and Three remains lazy.
export { STATES, STATE_CLIPS, moodToState, mappedMoods, moodCoverage, createReactionController, RIM_TINTS, parseTint, STRONG_MOODS, MOOD_HOLD_MS, moodGate, createStateMachine, pickFidget, createPassiveController } from './avatar/state.js?v=7697bf55d563';
export { renderSettings, iosMajorVersion, cameraFraming, CAMERA_ZOOM_RANGE, cameraFarPlane, createCameraView, createAvatarGesture, detectIOS, nextPixelRatio, nextQuality } from './avatar/camera.js';
export { speechMouthDelta, speechMouthLevel, LOOK_WEIGHT, lookSplit, EYE_GUARD, softLimit } from './avatar/presence.js';
export { stepEnvelope, nodCurve, blinkCurve, closestOnSegment, collidePoint, coneClamp, springStep, springHang, springSubsteps, poseJumped, SPRING } from './avatar/physics.js';
export { warmAvatar3D, forgetAvatarModels } from './avatar/loader.js';
export { createAvatar3D } from './avatar/runtime.js?v=6f540f7da5c2';
