import {
  BASE_FOV, cameraFarPlane, cameraFraming, cameraLimits, createAvatarGesture, createCameraView, createDoubleTap,
  createViewTween, zoomAim,
} from './camera.js';
import { wrapPi } from './common.js';

// Camera operations on the single avatar runtime owner.
export function installCamera(ctx) {
  ctx.frame = function frame({ snap = false } = {}) {
    if (!ctx.root) return;
    // Standing head height from the rest pose: the live pose would aim the camera at
    // the floor when she gets up from `sleep` (and jitter with the clip).
    const insets = { top: ctx.insetsPx.top / ctx.h, bottom: ctx.insetsPx.bottom / ctx.h };
    const f = cameraFraming({ framing: ctx.framing, state: ctx.stateName, aspect: ctx.w / ctx.h, headY: ctx.headRestY, insets });
    const tz = f.targetZ || 0;
    ctx.sideView = Boolean(f.side);
    ctx.camGoal.target.set(ctx.sideView ? 0 : ctx.follow, f.targetY, tz);
    const pos = f.side ? new ctx.THREE.Vector3(f.dist, f.camY, tz) : new ctx.THREE.Vector3(0, f.camY, f.dist);
    ctx.camGoal.sph.setFromVector3(pos.sub(ctx.camGoal.target));
    ctx.camGoal.fov = f.fov;
    ctx.camera.aspect = ctx.w / ctx.h;
    ctx.camera.updateProjectionMatrix();
    if (snap) ctx.camSnap = true;
    ctx.placeCamera(0);
  };

  ctx.absorbCameraInput = function absorbCameraInput() {
    if (ctx.controls) {
      ctx._sph2.setFromVector3(ctx._v1.subVectors(ctx.camera.position, ctx.controls.target));
      ctx.orbit.absorb(ctx._sph2);
    }
  };

  ctx.placeCamera = function placeCamera(dt, absorb = true) {
    // 1. Include pointer-handler and damping updates since our last placement,
    //    even when resize/insets snap the automatic framing this frame.
    if (absorb) ctx.absorbCameraInput();
    // 2. ease the framing (~0.8 s, frame-rate independent)
    const k = ctx.camSnap ? 1 : 1 - Math.exp(-dt * 3.0);
    ctx.camCur.target.lerp(ctx.camGoal.target, k);
    ctx.camCur.sph.radius += (ctx.camGoal.sph.radius - ctx.camCur.sph.radius) * k;
    ctx.camCur.sph.phi += (ctx.camGoal.sph.phi - ctx.camCur.sph.phi) * k;
    ctx.camCur.sph.theta += wrapPi(ctx.camGoal.sph.theta - ctx.camCur.sph.theta) * k;
    const fov = ctx.camCur.fov + (ctx.camGoal.fov - ctx.camCur.fov) * k;
    if (Math.abs(fov - ctx.camera.fov) > 1e-4) { ctx.camera.fov = fov; ctx.camera.updateProjectionMatrix(); }
    ctx.camCur.fov = fov;
    ctx.camSnap = false;
    // 3. Compose the persistent view with the automatic 3% speech push-in.
    const view = ctx.orbit.place(ctx.camCur.sph, ctx.pushIn);
    const { panY, zoom } = ctx.orbit.state;
    // The embedded (waist) framing aims up at her face as he pinches in and
    // down to her middle as he pinches out; the other framings never aim.
    const aim = zoomAim({ framing: ctx.framing, state: ctx.stateName, zoom, targetY: ctx.camCur.target.y, headY: ctx.headRestY });
    ctx.viewTarget.copy(ctx.camCur.target); ctx.viewTarget.y += panY + aim;
    ctx.camera.position.setFromSpherical(ctx._sph.set(view.radius, view.phi, view.theta)).add(ctx.viewTarget);
    if (ctx.controls) {
      ctx.controls.target.copy(ctx.viewTarget);
      ctx.controls.minDistance = view.minDistance; ctx.controls.maxDistance = view.maxDistance;
      ctx.controls.minAzimuthAngle = view.minAzimuthAngle; ctx.controls.maxAzimuthAngle = view.maxAzimuthAngle;
      ctx.controls.minPolarAngle = view.minPolarAngle; ctx.controls.maxPolarAngle = view.maxPolarAngle;
      ctx.controls.update();
    } else {
      ctx.camera.lookAt(ctx.viewTarget);
    }
    const far = cameraFarPlane(ctx.camera.position.length());
    if (ctx.camera.far !== far) { ctx.camera.far = far; ctx.camera.updateProjectionMatrix(); }
  };

  ctx.changeView = function changeView(change) {
    if (ctx.disposed || !ctx.root) return;
    ctx.viewTween = null; // a direct change replaces any easing in progress
    ctx.absorbCameraInput();
    // Drain the controls' residual rotation through their public API, then
    // replace that camera position. Otherwise a Reset during damping drifts.
    if (ctx.controls) {
      const damping = ctx.controls.enableDamping;
      ctx.controls.enableDamping = false;
      ctx.controls.update();
      ctx.controls.enableDamping = damping;
    }
    change();
    ctx.placeCamera(0, false);
    ctx.notifyView();
    if (ctx.paused) ctx.renderer.render(ctx.scene, ctx.camera);
  };

  /** Ease the user view to `to` (null = the automatic view) in the render
   *  loop. False when it can't animate (parked, reduced motion, not loaded):
   *  the caller then changes the view at once. */
  ctx.animateView = function animateView(to, ms = 380) {
    if (ctx.disposed || !ctx.root || ctx.paused || ctx.reducedMotion) return false;
    ctx.absorbCameraInput();
    const goal = createCameraView(to, ctx.orbit.limits).state;
    ctx.viewTween = createViewTween({ from: ctx.orbit.state, to: goal, at: performance.now(), ms });
    return true;
  };

  /** One frame of an animated view change (called before placeCamera). */
  ctx.stepViewTween = function stepViewTween(now) {
    if (!ctx.viewTween) return;
    const { view, done } = ctx.viewTween.value(now);
    ctx.orbit.set(view);
    if (done) { ctx.viewTween = null; ctx.notifyView(); }
  };

  ctx.notifyView = function notifyView() {
    const value = ctx.orbit.state;
    // Round only the persisted snapshot: camera precision stays untouched and
    // sub-pixel drift cannot cause localStorage writes on automatic frames.
    for (const k of Object.keys(value)) value[k] = +value[k].toFixed(5);
    const next = JSON.stringify(value);
    if (next === ctx.lastView) return;
    ctx.lastView = next;
    try { ctx.opts.onViewChange?.(value); } catch { /* storage is caller-owned */ }
  };

  ctx.settleView = function settleView() {
    clearTimeout(ctx.viewNotifyTimer);
    ctx.viewNotifyTimer = setTimeout(() => { if (!ctx.disposed) { ctx.absorbCameraInput(); ctx.notifyView(); } }, 140);
  };
}

// Initialize camera at its original point in the lifecycle.
export function initializeCamera(ctx) {
  // ── camera framing (eased) + persistent user zoom/orbit ──────────────────
  ctx.camGoal = { target: new ctx.THREE.Vector3(), sph: new ctx.THREE.Spherical(2, Math.PI / 2, 0), fov: BASE_FOV };
  ctx.camCur = { target: new ctx.THREE.Vector3(), sph: new ctx.THREE.Spherical(2, Math.PI / 2, 0), fov: BASE_FOV };
  ctx.orbit = createCameraView(ctx.opts.view, cameraLimits(ctx.framing));
  ctx.viewTween = null;    // animated reset (double tap in the embedded view)
  ctx.doubleTap = createDoubleTap();
  ctx.viewTarget = new ctx.THREE.Vector3();
  ctx.gesture = createAvatarGesture();
  ctx.raycaster = new ctx.THREE.Raycaster();
  ctx.lastView = JSON.stringify(ctx.orbit.state);
  ctx.orbitDragging = false;
  ctx.camSnap = true;
  ctx.drift = new ctx.THREE.Vector3();
  ctx.pushIn = 0;
  ctx.follow = 0; // slow horizontal follow of her hips (the idle clip drifts ~10 cm sideways)
  ctx.sideView = false;

  if (ctx.OrbitControls) {
    ctx.controls = new ctx.OrbitControls(ctx.camera, ctx.canvas);
    ctx.controls.enablePan = false;
    ctx.controls.enableRotate = false; // one pointer: our yaw + vertical-framing gesture
    // An unsupported ONE action resets OrbitControls to NONE after a pinch.
    // Disabled ROTATE returns early and can leave its two-pointer state alive.
    ctx.controls.touches.ONE = null;
    ctx.controls.enableZoom = true;
    ctx.controls.enableDamping = false; // exact release position, with no hidden residual input
    ctx.controls.dampingFactor = 0.08;
    ctx.controls.rotateSpeed = ctx.isIOS || globalThis.matchMedia?.('(pointer: coarse)').matches ? 0.7 : 0.5;
    ctx.onControlStart = () => { ctx.orbitDragging = true; ctx.viewTween = null; ctx.passives?.cancel(); };
    ctx.onControlEnd = () => { ctx.orbitDragging = ctx.gesture.active; ctx.settleView(); };
    ctx.controls.addEventListener('start', ctx.onControlStart);
    ctx.controls.addEventListener('end', ctx.onControlEnd);
  }

}
