import { STATES, parseTint } from './state.js';
import { normalizeFraming, renderSettings } from './camera.js';
import { DEG, clamp } from './common.js';
import { SPRING } from './physics.js';

// Initialize api at its original point in the lifecycle.
export function createPublicApi(ctx) {
  ctx.api = {
    get ready() { return ctx.ready; },
    get state() { return ctx.stateName; },
    /** Load timings (ms since create) + live render stats, for logging/diagnostics. */
    get stats() {
      const i = ctx.renderer.info;
      return {
        ...ctx.stats, t0: undefined, pixelRatio: ctx.renderer.getPixelRatio(), drawCalls: i.render.calls, triangles: i.render.triangles,
        programs: i.programs?.length ?? 0, textures: i.memory.textures, fps: Math.round(ctx.fpsAvg), outlines: ctx.outlinesOn, headJoint: ctx.headJoint,
        springMs: ctx.springs ? +ctx.springs.ms.toFixed(3) : null,
      };
    },
    /** Set a state directly (thinking, talking, greet, …): always immediate. */
    setState(name) {
      if (ctx.disposed) return;
      ctx.sm.setState(name);
    },
    /** Follow one of her moods. Mood-to-mood changes hold for 4 s (strong moods
     *  excepted); a state set with setState() is always left at once. */
    setMood(mood) {
      if (ctx.disposed) return;
      ctx.sm.setMood(mood);
    },
    setSpeaking(level) {
      if (ctx.disposed) return;
      const v = Number(level);
      ctx.speakingTarget = Number.isFinite(v) ? Math.min(1, Math.max(0, v)) : 0;
      if (ctx.speakingTarget > 0.04) {
        ctx.reactions.finish();
        if (ctx.passives.active) ctx.passives.cancel();
      }
    },
    /** The user-view limits stay those of the framing it was created with. */
    setFraming(f) { ctx.framing = normalizeFraming(f); if (ctx.root) ctx.frame(); },
    /** Distance multiplier: below 1 zooms in, above 1 zooms out. */
    zoomBy(factor) { ctx.changeView(() => ctx.orbit.zoomBy(factor)); },
    /** Horizontal camera angle in radians, relative to the current view. */
    rotateBy(radians) { ctx.changeView(() => ctx.orbit.rotateBy(radians)); },
    /** Positive metres moves her up on screen; negative moves her down. */
    moveBy(metres) { ctx.changeView(() => ctx.orbit.moveBy(metres)); },
    getView() { if (!ctx.disposed) ctx.absorbCameraInput(); return ctx.orbit.state; },
    setView(view) { ctx.changeView(() => ctx.orbit.set(view)); },
    /** Back to the automatic view; `animate` eases there (~0.4 s) when she is on screen. */
    resetView({ animate = false } = {}) {
      if (animate && ctx.animateView(null)) return;
      ctx.changeView(() => ctx.orbit.reset());
    },
    react: ctx.react,
    getReactions() { return ctx.disposed ? [] : ctx.reactions.choices; },
    getMotionStatus() { return ctx.motionLibraryStatus; },
    /** Mood rim colour: 0xRRGGBB, '#rrggbb' (or a RIM_TINTS key); eases over ~1.2 s. */
    setTint(color) {
      const c = parseTint(color);
      if (c === null || ctx.disposed) return;
      ctx.rimGoal.setHex(c);
      if (ctx.paused) { ctx.shared.uRimColor.value.copy(ctx.rimGoal); ctx.hemi.color.copy(ctx.skyBase).lerp(ctx.rimGoal, 0.08); }
    },
    /** CSS px of the canvas covered by the topbar (top) and the dock (bottom). */
    setInsets({ top = 0, bottom = 0 } = {}) {
      if (ctx.disposed) return;
      const ok = (v) => (Number.isFinite(Number(v)) ? Math.max(0, Number(v)) : 0);
      const next = { top: ok(top), bottom: ok(bottom) };
      if (next.top === ctx.insetsPx.top && next.bottom === ctx.insetsPx.bottom) return; // resize events repeat it
      ctx.insetsPx = next;
      if (ctx.root) { ctx.frame({ snap: true }); if (ctx.paused) ctx.renderer.render(ctx.scene, ctx.camera); }
    },
    resize() {
      if (ctx.disposed) return;
      ({ w: ctx.w, h: ctx.h } = ctx.size());
      const s = renderSettings({ devicePixelRatio: globalThis.devicePixelRatio, width: ctx.w, height: ctx.h, isIOS: ctx.isIOS, iosMajor: ctx.iosMajor });
      ctx.rs = s; // new cap (orientation/size change); keep any adaptive step below it
      ctx.renderer.setPixelRatio(Math.min(ctx.renderer.getPixelRatio(), s.pixelRatio));
      ctx.renderer.setSize(ctx.w, ctx.h, false);
      ctx.syncOutlineRes();
      ctx.frame({ snap: true });
      if (ctx.paused) ctx.renderer.render(ctx.scene, ctx.camera);
    },
    /** Park the view while another one shows: no render loop until setPaused(false). */
    setPaused(on) {
      ctx.userPaused = Boolean(on);
      if (on && !ctx.disposed) { ctx.shared.uRimColor.value.copy(ctx.rimGoal); ctx.updateTint(0); } // parked: no easing to wait for
      if (!ctx.disposed && ctx.ready) ctx.syncPaused();
    },
    dispose() { if (!ctx.disposed) ctx.teardown(); },
  };

  if (ctx.opts.debug) {
    // Test/inspection hook only (tools/avatar): direct access to the scene graph.
    ctx.api.debug = {
      THREE: ctx.THREE, scene: ctx.scene, camera: ctx.camera, renderer: ctx.renderer, get root() { return ctx.root; }, controls: ctx.controls, frame: ctx.frame, mixer: ctx.mixer, shared: ctx.shared, outlineU: ctx.outlineU, hemi: ctx.hemi, key: ctx.key,
      get outlines() { return ctx.outlineMeshes; }, setOutlines: ctx.setOutlines, look: ctx.look, get headJoint() { return ctx.headJoint; },
      materials() { const out = []; ctx.root?.traverse((o) => { if (o.isMesh) out.push(...[].concat(o.material)); }); return out; },
      /** Snapshot of the procedural layers (tools/avatar/debug/life.py). */
      probe() {
        const q = new ctx.THREE.Quaternion(); const f = new ctx.THREE.Vector3();
        if (ctx.faceBone) { ctx.faceBone.getWorldQuaternion(q); f.copy(ctx.restFwd).applyQuaternion(q); }
        return {
          state: ctx.sm.state, source: ctx.sm.source, pending: ctx.sm.pending, mood: ctx.sm.mood, clip: ctx.current?.action?.getClip().name, fidget: Boolean(ctx.current?.fidget),
          lookW: ctx.look.w, lookYaw: ctx.look.yaw, lookPitch: ctx.look.pitch, faceYaw: Math.atan2(f.x, f.z), facePitch: Math.asin(Math.max(-1, Math.min(1, f.y))),
          armW: ctx.armW, pushIn: ctx.pushIn, talkPresence: ctx.talkPresence, talkWeight: ctx.talkWeight, mouthOpen: ctx.mouthOpen, mouthMode: ctx.mouthMode, reaction: ctx.reactions.active, reactionCount: ctx.reactions.count,
          passive: { active: ctx.passives.active, last: ctx.passives.last, count: ctx.passives.count, remainingMs: ctx.passives.remainingMs,
            available: [...ctx.fidgetActions.keys()] },
          motionLibraryStatus: ctx.motionLibraryStatus, motionClips: ctx.motionCatalog.map((entry) => entry.clip),
          nods: ctx.nods.length, blink: ctx.blinkIdx >= 0 ? ctx.skinned.find((m) => m.morphTargetInfluences)?.morphTargetInfluences[ctx.blinkIdx] ?? 0 : null,
          lid: ctx.lid, drift: ctx.drift.toArray(), swayAmp: ctx.shared.uSwayAmp.value, sway: ctx.shared.uSway.value.toArray(), shadowA: ctx.shadowA, shadowVisible: Boolean(ctx.shadowMesh?.visible),
          fov: ctx.camera.fov, cam: ctx.camera.position.toArray(), target: ctx.viewTarget.toArray(), reducedMotion: ctx.reducedMotion, outlines: ctx.outlinesOn, headJoint: ctx.headJoint,
          orbit: { ...ctx.orbit.state, dragging: ctx.orbitDragging }, follow: ctx.follow,
          eyeElevDeg: ctx.guard.e0 / DEG, eyeElevAfterDeg: ctx.guard.e1 / DEG, guardLiftDeg: ctx.guard.lift / DEG, guardW: ctx.guard.w,
          springs: ctx.springs ? { mode: ctx.springs.mode, ms: ctx.springs.ms, chains: ctx.springs.chains.map((c) => c.name), clearance: ctx.springClearance(),
            tips: ctx.springs.chains.map((c) => c.joints[c.joints.length - 1].cur.slice()) } : null,
          rim: ctx.shared.uRimColor.value.getHexString(),
        };
      },
      /** Hold clip `name` at `t` s (alone, full weight) as state `state`, let the procedural
       *  layers settle for `frames` steps, render once, and return probe() plus the screen
       *  position of her eyes (CSS px) — eyes open. For tools/avatar/debug/eyes.py. */
      poseAt(name, t, { state = 'idle', frames = 60, guard: g = true } = {}) {
        const a = ctx.actions.get(name);
        if (!a) return null;
        ctx.userPaused = true; ctx.syncPaused();
        if (STATES.includes(state)) { ctx.stateName = state; ctx.sm.settle(state); ctx.frame({ snap: true }); }
        for (const x of [...ctx.actions.values(), ...ctx.fidgetActions.values()]) { x.stop(); x.setEffectiveWeight(0); }
        a.reset(); a.setLoop(ctx.THREE.LoopRepeat, Infinity); a.setEffectiveWeight(1); a.play(); a.time = Number(t) || 0;
        ctx.current = { state, action: a, once: false };
        ctx.speakingTarget = 0; ctx.speaking = 0; ctx.talkWeight = state === 'talking' ? 1 : 0;
        ctx.resetSprings();
        ctx.GUARD.on = g !== false;
        const t0 = performance.now();
        for (let i = 0; i < frames; i++) { ctx.blinkT = -1; ctx.nextBlinkAt = Infinity; ctx.step(1 / 30, t0 + i * 33.3, 0); }
        for (const m of ctx.skinned) if (m.morphTargetInfluences && ctx.blinkIdx >= 0) m.morphTargetInfluences[ctx.blinkIdx] = 0;
        ctx.camera.updateMatrixWorld();
        ctx.renderer.render(ctx.scene, ctx.camera);
        const bone = ctx.faceBone || ctx.head;
        const eye = ctx.eyeLocal.clone().applyMatrix4(bone.matrixWorld);
        const top = eye.clone().add(new ctx.THREE.Vector3(0, 0.12, 0));
        const px = (v) => { const n = v.clone().project(ctx.camera); return [(n.x + 1) / 2 * ctx.w, (1 - n.y) / 2 * ctx.h]; };
        ctx.GUARD.on = true;
        return { ...ctx.api.debug.probe(), eyePx: px(eye), headTopPx: px(top), clipDur: a.getClip().duration };
      },
      guard: ctx.GUARD,
      /** Start a passive gesture now, with the same priority guards as its timer. */
      fidget() { ctx.passives.trigger(ctx.passiveContext()); return ctx.passives.active; },
      /** Pose the current clip at `t` seconds, alone at full weight (comparable stills). */
      seek(t) {
        if (!ctx.current?.action) return;
        for (const a of ctx.actions.values()) if (a !== ctx.current.action) a.setEffectiveWeight(0);
        ctx.current.action.enabled = true; ctx.current.action.setEffectiveWeight(1); ctx.current.action.time = Number(t) || 0;
        ctx.mixer.update(0);
        ctx.resetSprings(); ctx.updateSprings(0, performance.now()); // hang the hair for the still
      },
      /** Advance clips and every procedural layer by `seconds` at a fixed `dt` without
       *  real time (deterministic motion tests), then render once. `speaking` is a level
       *  or (t) => level. The render loop is parked while it runs. */
      run(seconds, { dt = 1 / 30, speaking = null } = {}) {
        ctx.userPaused = true; ctx.syncPaused({ simulate: true });
        ctx.simClock = Math.max(ctx.simClock, performance.now() / 1000); // blinks/fidgets/saccades are scheduled on real time
        const n = Math.max(1, Math.round((Number(seconds) || 0) / dt));
        for (let i = 0; i < n; i++) {
          ctx.simClock += dt;
          if (speaking !== null) ctx.speakingTarget = typeof speaking === 'function' ? clamp(Number(speaking(ctx.simClock)) || 0, 0, 1) : clamp(Number(speaking) || 0, 0, 1);
          ctx.sm.tick();
          ctx.step(dt, ctx.simClock * 1000, dt, true);
        }
        ctx.camera.updateMatrixWorld();
        ctx.renderer.render(ctx.scene, ctx.camera);
        return ctx.api.debug.probe();
      },
      resetSprings: ctx.resetSprings,
      /** Live spring tuning: { long|lock: { stiffness:[root, tip], gravity, drag, hitRadius } }. */
      tuneSprings(set = {}) {
        if (!ctx.springs) return false;
        for (const ch of ctx.springs.chains) {
          const kind = /side|lock|front/i.test(ch.name || '') ? 'lock' : 'long';
          const p = { ...SPRING[kind], ...(set[kind] || {}) };
          ch.joints.forEach((j, i) => {
            const f = ch.joints.length > 1 ? i / (ch.joints.length - 1) : 0;
            j.stiffness = p.stiffness[0] + (p.stiffness[1] - p.stiffness[0]) * f; j.gravity = p.gravity; j.drag = p.drag; j.hit = p.hitRadius;
            j.maxAngle = (p.maxAngle || 0) * DEG;
          });
        }
        ctx.resetSprings();
        return true;
      },
      /** Start clip `name` at `t` s as state `state` (no settle; follow with run()). */
      playClip(name, t = 0, { state = 'idle' } = {}) {
        const a = ctx.actions.get(name);
        if (!a) return false;
        ctx.userPaused = true; ctx.syncPaused();
        if (STATES.includes(state)) { ctx.stateName = state; ctx.sm.settle(state); }
        for (const x of [...ctx.actions.values(), ...ctx.fidgetActions.values()]) { if (x !== a) { x.stop(); x.setEffectiveWeight(0); } }
        a.reset(); a.setLoop(ctx.THREE.LoopRepeat, Infinity); a.setEffectiveWeight(1); a.play(); a.time = Number(t) || 0;
        ctx.current = { state, action: a, once: false };
        return true;
      },
    };
  }

}
