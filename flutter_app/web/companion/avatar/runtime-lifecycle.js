import { followGoal, nextQuality } from './camera.js';
import { DEG } from './common.js';

// Lifecycle operations on the single avatar runtime owner.
export function installLifecycle(ctx) {
  ctx.syncOutlineRes = function syncOutlineRes() {
    ctx.renderer.getDrawingBufferSize(ctx.outlineU.uRes.value);
    ctx.outlineU.uDpr.value = ctx.renderer.getPixelRatio();
  };

  ctx.adapt = function adapt(now) {
    if (ctx.lastNow) { ctx.frameAcc += now - ctx.lastNow; ctx.frameN++; }
    ctx.lastNow = now;
    if (ctx.frameAcc < (ctx.adaptWindows < 3 ? 1000 : 2000)) return; // react quickly right after start
    ctx.adaptWindows++;
    const avg = ctx.frameAcc / ctx.frameN;
    ctx.fpsAvg = 1000 / avg;
    ctx.frameAcc = 0; ctx.frameN = 0;
    if (!ctx.adaptive) return;
    const cur = ctx.renderer.getPixelRatio();
    const q = nextQuality({ avgFrameMs: avg, ratio: cur, cap: ctx.rs.pixelRatio, outlines: ctx.outlinesOn, hasOutlines: ctx.outlineMeshes.length > 0 && ctx.opts.outlines !== false });
    if (q.ratio !== cur) { ctx.renderer.setPixelRatio(q.ratio); ctx.renderer.setSize(ctx.w, ctx.h, false); ctx.syncOutlineRes(); }
    if (q.outlines !== ctx.outlinesOn) ctx.setOutlines(q.outlines);
  };

  ctx.tick = function tick() {
    ctx.rafId = 0;
    if (ctx.disposed || ctx.paused) return;
    ctx.timer.update();
    const dt = Math.min(ctx.timer.getDelta(), 0.1);
    const now = performance.now();
    const t = now / 1000;
    ctx.adapt(now);
    if (!ctx.disposed) ctx.sm.tick(); // a mood held by the hysteresis
    ctx.step(dt, now, dt);
    if (ctx.life && !ctx.reducedMotion) { // slow figure-eight drift, this frame only
      ctx.drift.set(Math.sin(t * 0.43) * 0.004, Math.sin(t * 0.29) * 0.003, 0);
      ctx.camera.position.add(ctx.drift);
    } else ctx.drift.set(0, 0, 0);
    ctx.renderer.render(ctx.scene, ctx.camera);
    ctx.camera.position.sub(ctx.drift);
    ctx.rafId = requestAnimationFrame(ctx.tick);
  };

// One frame of animation + procedural layers + camera (no render). `mixerDt` 0
  // holds the clip still while the layers settle (debug.poseAt).
  ctx.step = function step(dt, now, mixerDt, simulate = false) {
    const t = now / 1000;
    // speaking: smooth the level, layer the talking clip over the base state
    ctx.speaking += (ctx.speakingTarget - ctx.speaking) * Math.min(1, dt * 12);
    ctx.passives.tick(mixerDt, ctx.passiveContext(simulate));
    if (ctx.talkAction && ctx.stateName !== 'talking' && ctx.stateName !== 'sleep') {
      const goal = !ctx.reducedMotion && !ctx.reactions?.active && ctx.speaking > 0.04 ? Math.min(0.95, 0.4 + ctx.speaking) : 0;
      ctx.talkWeight += (goal - ctx.talkWeight) * Math.min(1, dt * 4);
      if (goal === 0 && ctx.talkWeight < 0.001) ctx.talkWeight = 0;
      if (ctx.talkWeight > 0) { ctx.talkAction.enabled = true; ctx.talkAction.play(); }
      ctx.talkAction.setEffectiveWeight(ctx.talkWeight);
    } else if (ctx.talkAction) {
      ctx.talkWeight = 0;
      if (ctx.stateName !== 'talking') ctx.talkAction.setEffectiveWeight(0);
    }
    // three's PropertyMixer only writes a bone whose mixed value CHANGED since the last
    // update, so on a flat stretch of a track (or a held pose) the bone would keep last
    // frame's procedural rotation and every layer below would pile up on it. Put the
    // clean clip pose back first; the mixer then overwrites it whenever it has news.
    for (const c of ctx.clean) c.bone.quaternion.copy(c.q);
    ctx.mixer.update(mixerDt);
    for (const c of ctx.clean) c.q.copy(c.bone.quaternion);
    // procedural layers on top of the clip pose: arms → breathing → look-at → nods → eye guard → blink
    ctx.applyArms(dt, t);
    ctx.applyBreath(dt);
    ctx.applyLookAt(dt, now);
    ctx.applyTalk(dt, now);
    ctx.applyMouth(dt);
    ctx.applyEyeGuard(dt);
    ctx.applyBlink(dt, now);
    ctx.updateSprings(dt, now);
    ctx.updateSway(dt, t);
    ctx.updateShadow(dt);

    if (ctx.viewTween) ctx.stepViewTween(performance.now());
    if (ctx.bones.Hips && !ctx.sideView) { // keep her centred on a narrow phone without chasing every sway
      const head = ctx.head || ctx.neck;
      const goal = followGoal({ framing: ctx.framing, state: ctx.stateName, zoom: ctx.orbit.state.zoom,
        hipsX: ctx.bones.Hips.getWorldPosition(ctx._v1).x, headX: head ? head.getWorldPosition(ctx._v2).x : Number.NaN });
      ctx.follow += (goal.x - ctx.follow) * (1 - Math.exp(-dt * goal.rate));
      ctx.camGoal.target.x = ctx.follow;
    }
    ctx.placeCamera(dt);
    ctx.updateTint(dt);
  };

  ctx.start = function start() { if (!ctx.rafId && !ctx.disposed && !ctx.paused) { ctx.timer.reset(); ctx.sway.fresh = true; ctx.resetSprings(); ctx.rafId = requestAnimationFrame(ctx.tick); } };

  ctx.stop = function stop() { if (ctx.rafId) cancelAnimationFrame(ctx.rafId); ctx.rafId = 0; ctx.lastNow = 0; ctx.frameAcc = 0; ctx.frameN = 0; };

// ── lifecycle ────────────────────────────────────────────────────────────
  // One rule for the render loop: it runs only while the page is visible, the
  // GL context is live, and the caller hasn't parked the view (setPaused).
  ctx.syncPaused = function syncPaused({ simulate = false } = {}) {
    const wasPaused = ctx.paused;
    ctx.paused = (typeof document !== 'undefined' && document.hidden) || ctx.contextLost || ctx.userPaused;
    if (!simulate && wasPaused !== ctx.paused) ctx.passives?.cancel();
    if (ctx.controls) ctx.controls.enabled = !ctx.paused;
    if (ctx.paused && !simulate) {
      if (ctx.ready && ctx.orbitDragging) { ctx.absorbCameraInput(); ctx.notifyView(); }
      ctx.gesture.cancel(); ctx.orbitDragging = false;
    }
    if (ctx.paused) ctx.stop(); else ctx.start();
  };

  ctx.disposeObject = function disposeObject(obj) {
    obj?.traverse?.((o) => {
      if (o.geometry) o.geometry.dispose();
      const mats = o.material ? (Array.isArray(o.material) ? o.material : [o.material]) : [];
      for (const m of mats) {
        for (const k of Object.keys(m)) { const v = m[k]; if (v && v.isTexture) v.dispose(); }
        m.dispose();
      }
    });
  };

  ctx.teardown = function teardown() {
    if (ctx.disposed) return;
    if (ctx.ready) { ctx.absorbCameraInput(); ctx.notifyView(); }
    ctx.disposed = true;
    ctx.opts.signal?.removeEventListener('abort', ctx.onAbort);
    ctx.stop();
    clearTimeout(ctx.viewNotifyTimer);
    ctx.reactions?.cancel();
    ctx.passives?.cancel({ restore: false });
    if (ctx.onVisibility) document.removeEventListener('visibilitychange', ctx.onVisibility);
    if (ctx.onLost) ctx.canvas.removeEventListener('webglcontextlost', ctx.onLost);
    if (ctx.onRestored) ctx.canvas.removeEventListener('webglcontextrestored', ctx.onRestored);
    if (ctx.onPointer) ctx.canvas.removeEventListener('pointermove', ctx.onPointer);
    if (ctx.onPointerLeave) ctx.canvas.removeEventListener('pointerleave', ctx.onPointerLeave);
    if (ctx.onViewPointerDown) ctx.canvas.removeEventListener('pointerdown', ctx.onViewPointerDown);
    if (ctx.onViewPointerMove) ctx.canvas.removeEventListener('pointermove', ctx.onViewPointerMove);
    if (ctx.onViewPointerUp) ctx.canvas.removeEventListener('pointerup', ctx.onViewPointerUp);
    if (ctx.onViewPointerCancel) ctx.canvas.removeEventListener('pointercancel', ctx.onViewPointerCancel);
    if (ctx.onViewPointerCancel) ctx.canvas.removeEventListener('lostpointercapture', ctx.onViewPointerCancel);
    if (ctx.onMotionPref) ctx.motionMq?.removeEventListener?.('change', ctx.onMotionPref);
    ctx.ro?.disconnect();
    if (ctx.controls) {
      if (ctx.onControlStart) ctx.controls.removeEventListener('start', ctx.onControlStart);
      if (ctx.onControlEnd) ctx.controls.removeEventListener('end', ctx.onControlEnd);
      ctx.controls.dispose();
    }
    ctx.timer.dispose?.();
    if (ctx.mixer) {
      ctx.mixer.removeEventListener('finished', ctx.onFinished);
      ctx.mixer.stopAllAction(); ctx.mixer.uncacheRoot(ctx.root);
    }
    ctx.disposeObject(ctx.root); // outline meshes live in the model tree (shared geometry: a second dispose is a no-op)
    ctx.disposeObject(ctx.shadowMesh);
    ctx.shadowMesh?.removeFromParent();
    for (const t of ctx.ownedTextures) t.dispose();
    ctx.renderer.dispose();
    try { ctx.renderer.forceContextLoss(); } catch { /* already lost */ }
    ctx.canvas.remove();
    ctx.actions.clear(); ctx.fidgetActions.clear(); ctx.skinned = []; ctx.outlineMeshes = []; ctx.root = null; ctx.mixer = null;
  };
}

// Initialize loop at its original point in the lifecycle.
export function initializeLoop(ctx) {
  // ── loop ─────────────────────────────────────────────────────────────────
  ctx.scheduleBlink(performance.now());

  // Adaptive quality: every ~2 s compare the average frame time against the
  // budget and step the pixel ratio (between 1 and the device cap); at the
  // floor, drop the outlines rather than the frame rate.
  ctx.adaptive = ctx.opts.adaptiveResolution !== false;
  ctx.adaptWindows = 0;

}

// Initialize lifecycle at its original point in the lifecycle.
export function initializeLifecycle(ctx) {

  ctx.onVisibility = ctx.syncPaused;
  ctx.onLost = (e) => { e.preventDefault(); ctx.contextLost = true; ctx.syncPaused(); };
  ctx.onRestored = () => { ctx.contextLost = false; ctx.resetSprings(); ctx.frame({ snap: true }); ctx.syncPaused(); };
  ctx.onPointer = (e) => {
    if (e.pointerType !== 'mouse') return; // touch: she looks at the camera
    const r = ctx.canvas.getBoundingClientRect();
    if (!r.width || !r.height) return;
    ctx.pointer.x = ((e.clientX - r.left) / r.width) * 2 - 1;
    ctx.pointer.y = -((e.clientY - r.top) / r.height) * 2 + 1;
    ctx.pointer.until = performance.now() + 2500;
  };
  ctx.onPointerLeave = () => { ctx.pointer.until = 0; };
  ctx.onViewPointerDown = (e) => {
    if (ctx.disposed || ctx.paused || (e.pointerType === 'mouse' && e.button !== 0)) return;
    ctx.gesture.down(e.pointerId, e.clientX, e.clientY, performance.now());
    ctx.viewTween = null; // his finger takes over from an easing reset, where it is now
    ctx.canvas.setPointerCapture?.(e.pointerId);
    ctx.orbitDragging = true;
    ctx.passives.cancel();
  };
  ctx.onViewPointerMove = (e) => {
    if (ctx.disposed || ctx.paused) return;
    const delta = ctx.gesture.move(e.pointerId, e.clientX, e.clientY);
    if (!delta) return;
    ctx.doubleTap.reset(); // a drag between two taps is not a double tap
    const height = Math.max(1, ctx.canvas.clientHeight);
    const distance = ctx.camera.position.distanceTo(ctx.viewTarget);
    const metresPerPixel = 2 * distance * Math.tan(ctx.camera.fov * DEG / 2) / height;
    ctx.absorbCameraInput();
    ctx.orbit.rotateBy(-delta.dx * 2 * Math.PI / height * (ctx.controls?.rotateSpeed || 0.5));
    ctx.orbit.moveBy(-delta.dy * metresPerPixel);
    ctx.placeCamera(0, false);
  };
  ctx.onViewPointerUp = (e) => {
    const tap = ctx.gesture.up(e.pointerId, e.clientX, e.clientY, performance.now());
    ctx.orbitDragging = ctx.gesture.active;
    if (!ctx.disposed) { ctx.absorbCameraInput(); ctx.notifyView(); }
    if (!tap || ctx.disposed || ctx.paused) return;
    const rect = ctx.canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    ctx.root.updateMatrixWorld(true);
    ctx.camera.updateMatrixWorld(true);
    // Bounds change with the skinned pose; recalculate before Three's coarse
    // sphere test so a hand outside the previous animation frame is hittable.
    for (const mesh of ctx.skinned) mesh.computeBoundingSphere();
    ctx.raycaster.setFromCamera({ x: 2 * (tap.x - rect.left) / rect.width - 1, y: 1 - 2 * (tap.y - rect.top) / rect.height }, ctx.camera);
    if (ctx.raycaster.intersectObjects(ctx.skinned, false).length) { ctx.doubleTap.reset(); ctx.react(); return; }
    // Embedded view: a double tap on empty space eases back to the default view.
    if (ctx.opts.doubleTapReset && ctx.doubleTap.tap(tap.x, tap.y, tap.downAt, tap.upAt)) ctx.api.resetView({ animate: true });
  };
  ctx.onViewPointerCancel = (e) => { ctx.gesture.cancel(e.pointerId); ctx.orbitDragging = ctx.gesture.active; ctx.settleView(); };
  ctx.onMotionPref = () => {
    ctx.reducedMotion = ctx.opts.reducedMotion === true || Boolean(ctx.motionMq?.matches);
    ctx.passives.cancel();
    if (ctx.reducedMotion) ctx.reactions?.finish();
  };
  document.addEventListener('visibilitychange', ctx.onVisibility);
  ctx.canvas.addEventListener('webglcontextlost', ctx.onLost, false);
  ctx.canvas.addEventListener('webglcontextrestored', ctx.onRestored, false);
  ctx.canvas.addEventListener('pointermove', ctx.onPointer, { passive: true });
  ctx.canvas.addEventListener('pointerleave', ctx.onPointerLeave, { passive: true });
  if (ctx.OrbitControls) {
    ctx.canvas.addEventListener('pointerdown', ctx.onViewPointerDown);
    ctx.canvas.addEventListener('pointermove', ctx.onViewPointerMove);
    ctx.canvas.addEventListener('pointerup', ctx.onViewPointerUp);
    ctx.canvas.addEventListener('pointercancel', ctx.onViewPointerCancel);
    ctx.canvas.addEventListener('lostpointercapture', ctx.onViewPointerCancel);
  }
  ctx.motionMq?.addEventListener?.('change', ctx.onMotionPref);
  if (typeof ResizeObserver === 'function') { ctx.ro = new ResizeObserver(() => ctx.api.resize()); ctx.ro.observe(ctx.container); }
  ctx.syncOutlineRes();

}
