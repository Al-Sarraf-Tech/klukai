import { FIDGETS, REACTION_CATALOG, STATE_CLIPS, createPassiveController, createReactionController, createStateMachine } from './state.js?v=7697bf55d563';
import { DEG } from './common.js';

// Motion operations on the single avatar runtime owner.
export function installMotion(ctx) {
// ── rest-pose references for the procedural layers ───────────────────────
  // faceBone: the bone that actually carries the face — Head when it is a skin
  // joint, else Neck (the head mesh rides on it; the clips' Head rotation is
  // then invisible and must not count as "where she looks").
  ctx.captureRest = function captureRest() {
    ctx.faceBone = ctx.headJoint ? ctx.head : ctx.neck;
    ctx.clean = [ctx.neck, ctx.head, ...['Spine1', 'Spine2', 'LeftShoulder', 'RightShoulder', 'LeftArm', 'RightArm', 'LeftForeArm', 'RightForeArm', 'LeftHand', 'RightHand'].map((n) => ctx.bones[n])]
      .filter(Boolean).map((bone) => ({ bone, q: bone.quaternion.clone() }));
    ctx.root.updateMatrixWorld(true);
    if (ctx.head) ctx.headRestY = ctx.head.getWorldPosition(new ctx.THREE.Vector3()).y;
    const framingHeadY = ctx.root.userData?.framingHeadY;
    if (Number.isFinite(framingHeadY) && framingHeadY >= 0.5 && framingHeadY <= 3) ctx.headRestY = framingHeadY;
    ctx.forwardOf(ctx.faceBone, ctx.restFwd);
    ctx.forwardOf(ctx.bones.Hips, ctx.hipsFwd);
    ctx.forwardOf(ctx.faceBone, ctx.restUp, ctx.AX.y);
    // eyes ≈ the head-sphere centre, moved 2 cm forward; into the face bone's local space
    const sk = ctx.skinned[0]?.skeleton;
    const i = sk && ctx.faceBone ? sk.bones.indexOf(ctx.faceBone) : -1;
    if (i >= 0 && ctx.hasHeadCenter) {
      const mesh = ctx.shared.uHeadCenter.value.clone().applyMatrix4(ctx.skinned[0].bindMatrix);
      mesh.z += 0.02 * ctx.unitsPerMetre;
      ctx.eyeLocal.copy(mesh).applyMatrix4(sk.boneInverses[i]);
    }
  };

// Bone-local direction of "forward" (+z: she is modelled facing +z) in the
  // bind pose, from the skin's inverse bind matrix (the node rest pose may differ).
  ctx.forwardOf = function forwardOf(bone, out, dir = ctx.AX.z) {
    if (!bone) return;
    const sk = ctx.skinned[0]?.skeleton;
    const i = sk ? sk.bones.indexOf(bone) : -1;
    const q = new ctx.THREE.Quaternion();
    if (i >= 0) sk.boneInverses[i].clone().invert().decompose(new ctx.THREE.Vector3(), q, new ctx.THREE.Vector3());
    else bone.getWorldQuaternion(q);
    out.copy(dir).applyQuaternion(q.invert());
  };

  ctx.pickClip = function pickClip(state) {
    const spec = STATE_CLIPS[state] || STATE_CLIPS.idle;
    const avail = spec.clips.filter((c) => ctx.actions.has(c));
    if (!avail.length) return null;
    return avail[Math.floor(Math.random() * avail.length)];
  };

  ctx.play = function play(state, { fade = ctx.FADE } = {}) {
    const spec = STATE_CLIPS[state] || STATE_CLIPS.idle;
    const clipName = ctx.pickClip(state) || ctx.pickClip('idle');
    const next = clipName ? ctx.actions.get(clipName) : null;
    if (!next) return;
    if (ctx.current?.action === next && next.isRunning()) {
      // Already on screen. If it was playing as a one-shot fidget (thankful is
      // both a fidget and a happy clip), adopt it in place with this state's
      // loop mode: no restart, and its end is no longer taken for a fidget's.
      if (ctx.current.fidget || ctx.current.reaction || ctx.current.once !== !!spec.once) {
        next.setLoop(spec.loop ? ctx.THREE.LoopRepeat : ctx.THREE.LoopOnce, spec.loop ? Infinity : 1);
        next.clampWhenFinished = !spec.loop;
        ctx.current = { state, action: next, once: !!spec.once };
      } else ctx.current.state = state;
      return;
    }
    next.reset();
    next.setLoop(spec.loop ? ctx.THREE.LoopRepeat : ctx.THREE.LoopOnce, spec.loop ? Infinity : 1);
    next.clampWhenFinished = !spec.loop;
    next.setEffectiveTimeScale(1);
    next.setEffectiveWeight(1);
    if (clipName === 'idle') { // never start the idle from the same frame
      next.time = Math.random() * next.getClip().duration;
    }
    next.play();
    if (ctx.current?.action && ctx.current.action !== next) {
      ctx.current.action.crossFadeTo(next, fade, false);
    } else {
      next.fadeIn(fade);
    }
    ctx.current = { state, action: next, once: !!spec.once };
  };

  ctx.buildFidgets = function buildFidgets(clips) {
    const byName = new Map(clips.map((c) => [c.name, c]));
    for (const f of FIDGETS) {
      try {
        const src = byName.get(f.from || f.name);
        if (!src) continue;
        const clip = f.from ? ctx.THREE.AnimationUtils.subclip(src, f.name, Math.round(f.start * 30), Math.round(Math.min(f.end, src.duration) * 30), 30) : src;
        if (!clip.tracks.length || clip.duration < 0.5) continue;
        ctx.fidgetActions.set(f.name, f.from ? ctx.mixer.clipAction(clip) : ctx.actions.get(f.name));
      } catch { /* a missing or odd clip only means one fidget fewer */ }
    }
  };

  ctx.playFidget = function playFidget(name) {
    const next = name && ctx.fidgetActions.get(name);
    if (!next || !ctx.current?.action || ctx.current.action === next) return;
    next.reset();
    next.setLoop(ctx.THREE.LoopOnce, 1);
    next.clampWhenFinished = true;
    next.setEffectiveTimeScale(1);
    next.setEffectiveWeight(1);
    next.play();
    ctx.current.action.crossFadeTo(next, 0.6, false);
    ctx.current = { state: ctx.stateName, action: next, once: true, fidget: true };
  };

  ctx.onFinished = function onFinished(e) {
    if (!ctx.current || e.action !== ctx.current.action || !ctx.current.once) return;
    if (ctx.current.reaction) { ctx.reactions.finish(); return; }
    if (ctx.current.fidget) { ctx.passives.finish(); return; }
    if (ctx.stateName === 'greet') { ctx.sm.settle('idle'); ctx.stateName = 'idle'; }
    ctx.play(ctx.stateName);
  };

  ctx.passiveContext = function passiveContext(simulate = false) {
    return { state: ctx.stateName, speaking: Math.max(ctx.speakingTarget, ctx.speaking), reacting: Boolean(ctx.reactions?.active),
      dragging: ctx.orbitDragging || ctx.gesture.active, paused: ctx.paused && !simulate, reducedMotion: ctx.reducedMotion, enabled: ctx.life };
  };

  ctx.react = function react(kind = 'auto') {
    if (ctx.disposed || ctx.paused || ctx.stateName === 'sleep' || ctx.speakingTarget > 0.04 || ctx.orbitDragging || ctx.gesture.active) return false;
    return ctx.reactions.trigger(kind);
  };

// ── procedural layers ────────────────────────────────────────────────────
  // Rotate a bone about a WORLD axis (whatever its local axes are):
  // q_local' = parentInv · R · parent · q_local
  ctx.rotWorld = function rotWorld(bone, axis, angle) {
    if (!bone || !angle) return;
    bone.parent.getWorldQuaternion(ctx._q1);
    ctx._q2.setFromAxisAngle(axis, angle);
    ctx._q3.copy(ctx._q1).invert().multiply(ctx._q2).multiply(ctx._q1);
    bone.quaternion.premultiply(ctx._q3);
  };

  ctx.applyArms = function applyArms(dt, t) {
    const idleClip = ctx.current?.action === ctx.actions.get('idle') && !ctx.current.fidget;
    const goal = ctx.life && idleClip && ctx.stateName === 'idle' ? 1 - Math.min(1, ctx.talkWeight) : 0;
    ctx.armW += (goal - ctx.armW) * (1 - Math.exp(-dt / 0.4));
    if (ctx.armW < 1e-3) return;
    const swing = (ctx.reducedMotion ? 0 : 1.5 * DEG) * Math.sin(t * 2 * Math.PI * 0.11);
    ctx.rotWorld(ctx.bones.LeftArm, ctx.AX.z, 0.10 * ctx.armW);
    ctx.rotWorld(ctx.bones.RightArm, ctx.AX.z, -0.10 * ctx.armW);
    ctx.rotWorld(ctx.bones.LeftArm, ctx.AX.x, -swing * ctx.armW);
    ctx.rotWorld(ctx.bones.RightArm, ctx.AX.x, swing * ctx.armW);
    ctx.rotWorld(ctx.bones.LeftForeArm, ctx.AX.x, -0.17 * ctx.armW);
    ctx.rotWorld(ctx.bones.RightForeArm, ctx.AX.x, -0.17 * ctx.armW);
  };

  ctx.applyBreath = function applyBreath(dt) {
    if (!ctx.life || ctx.reducedMotion) return;
    const sleep = ctx.stateName === 'sleep';
    ctx.breathPhase += (dt / (sleep ? 5.5 : 4.2)) * 2 * Math.PI;
    const amp = ctx.stateName === 'excited' || ctx.stateName === 'flustered' ? 1.3 : 1;
    const s = Math.sin(ctx.breathPhase);
    ctx.rotWorld(ctx.bones.Spine1, ctx.AX.x, -0.6 * DEG * amp * s * 0.5);
    ctx.rotWorld(ctx.bones.Spine2, ctx.AX.x, -0.6 * DEG * amp * s * 0.5);
    if (!sleep) {
      ctx.rotWorld(ctx.bones.LeftShoulder, ctx.AX.z, 0.4 * DEG * amp * s);
      ctx.rotWorld(ctx.bones.RightShoulder, ctx.AX.z, -0.4 * DEG * amp * s);
    }
  };
}

// Initialize motion at its original point in the lifecycle.
export function initializeMotion(ctx) {
  // ── animation control ────────────────────────────────────────────────────
  ctx.FADE = 0.45;

  ctx.sm = createStateMachine({
    now: () => performance.now(),
    enter(name, prev) {
      ctx.reactions?.cancel();
      ctx.passives?.cancel({ restore: false });
      if (prev === 'talking' && name !== 'talking') ctx.talkWeight = 1; // fade the layer out, no pop
      ctx.stateName = name;
      ctx.play(name);
      if ((prev === 'sleep') !== (name === 'sleep')) { ctx.frame(); ctx.resetSprings(700); } // lying down / getting up: hang through the crossfade
    },
  });

  ctx.passives = createPassiveController({
    available: [...ctx.fidgetActions.keys()], play: ctx.playFidget,
    restore() { ctx.play(ctx.stateName, { fade: 0.6 }); },
  });

  ctx.reactions = createReactionController({
    available: [...ctx.actions.keys()],
    catalog: [...REACTION_CATALOG, ...ctx.motionCatalog],
    play(reaction) {
      ctx.passives.cancel({ restore: false });
      if (ctx.reducedMotion) {
        ctx.triggerBlink(performance.now(), true);
        ctx.reactions.finish();
        return;
      }
      const next = ctx.actions.get(reaction.clip);
      const previous = ctx.current?.action;
      next.reset().setLoop(ctx.THREE.LoopOnce, 1);
      next.clampWhenFinished = true;
      next.setEffectiveTimeScale(1);
      next.setEffectiveWeight(1);
      next.play();
      if (previous && previous !== next) previous.crossFadeTo(next, 0.28, false);
      ctx.current = { state: ctx.stateName, action: next, once: true, reaction: true };
      ctx.triggerBlink(performance.now());
    },
    restore() { ctx.play(ctx.stateName, { fade: 0.4 }); },
  });

  ctx.play('idle', { fade: 0 });
  ctx.stateName = 'idle';
  ctx.frame({ snap: true });

}
