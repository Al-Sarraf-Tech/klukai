import { DEG, clamp, smoothstep, wrapPi } from './common.js';
import { EYE_GUARD, LOOK_WEIGHT, lookSplit, softLimit } from './presence.js';
import { blinkCurve, nodCurve, stepEnvelope } from './physics.js';
import { nextBlink } from '../logic.js';

// Presence operations on the single avatar runtime owner.
export function installPresence(ctx) {
  ctx.lookTarget = function lookTarget(out, headPos) {
    out.copy(ctx.camera.position);
    if (performance.now() < ctx.pointer.until) {
      ctx._vRay.set(ctx.pointer.x, ctx.pointer.y, 0.5).unproject(ctx.camera).sub(ctx.camera.position).normalize();
      out.addScaledVector(ctx._vRay, ctx.camera.position.distanceTo(headPos));
    }
    return out;
  };

  ctx.applyLookAt = function applyLookAt(dt, now) {
    const bone = ctx.faceBone;
    if (!bone || !ctx.neck) return;
    bone.getWorldPosition(ctx._v1);
    bone.getWorldQuaternion(ctx._q1);
    const fwd = ctx._v2.copy(ctx.restFwd).applyQuaternion(ctx._q1);
    const dir = ctx.lookTarget(ctx._v3, ctx._v1).sub(ctx._v1).normalize();
    let dy = wrapPi(Math.atan2(dir.x, dir.z) - Math.atan2(fwd.x, fwd.z));
    let dp = Math.asin(clamp(dir.y, -1, 1)) - Math.asin(clamp(fwd.y, -1, 1));
    // micro-saccades: small target jumps, at most every 0.5 s (~5 % per 60 Hz frame)
    if (!ctx.reducedMotion && now >= ctx.look.nextSacc && Math.random() < 1 - Math.pow(0.95, dt * 60)) {
      ctx.look.sYaw = (Math.random() * 2 - 1) * 0.8 * DEG; ctx.look.sPitch = (Math.random() * 2 - 1) * 0.6 * DEG;
      ctx.look.nextSacc = now + 500;
    }
    dy += ctx.look.sYaw; dp += ctx.look.sPitch;
    // the clip's own head angle (vs the body) lowers the weight: big gestures win
    let clipAngle = 0;
    if (ctx.bones.Hips) { ctx.bones.Hips.getWorldQuaternion(ctx._q2); clipAngle = fwd.angleTo(ctx._v1.copy(ctx.hipsFwd).applyQuaternion(ctx._q2)); }
    // a target behind her (orbit, inspection cameras) would clamp to ±max and flip sides as she
    // turns, snapping the head: let go of it instead
    const behind = 1 - smoothstep(90 * DEG, 130 * DEG, Math.abs(dy));
    // Let temporary gestures keep their authored glances instead of pulling
    // a thoughtful or bashful reaction straight back toward the camera.
    const shownClip = ctx.current?.action?.getClip().name;
    const gestureState = { thankful: 'happy', salute: 'greet', dismiss: 'idle' }[shownClip] || shownClip;
    const lookWeight = ctx.current?.reaction && shownClip?.startsWith('motion_') ? 0.15
      : ctx.current?.fidget ? 0.45 : LOOK_WEIGHT[ctx.current?.reaction ? gestureState : ctx.stateName] ?? 0.6;
    const wGoal = ctx.life ? lookWeight * (1 - smoothstep(30 * DEG, 90 * DEG, clipAngle)) * behind : 0;
    ctx.look.w += (wGoal - ctx.look.w) * (1 - Math.exp(-dt * 2.5));
    const lim = lookSplit({ yaw: dy, pitch: dp, headJoint: ctx.headJoint }); // clamp first …
    const cy = (lim.neck.yaw + lim.head.yaw) * ctx.look.w;
    const cp = (lim.neck.pitch + lim.head.pitch) * ctx.look.w;
    const k = 1 - Math.exp(-dt * 4); // … then damp (ChatVRM smoothFactor 4)
    ctx.look.yaw += (cy - ctx.look.yaw) * k;
    ctx.look.pitch += (cp - ctx.look.pitch) * k;
    // a gaze shift over 15° comes with a blink
    if (now - ctx.look.gazeAt > 250) {
      if (Math.hypot(cy - ctx.look.gYaw, cp - ctx.look.gPitch) > 15 * DEG) ctx.triggerBlink(now);
      ctx.look.gYaw = cy; ctx.look.gPitch = cp; ctx.look.gazeAt = now;
    }
    if (ctx.look.w < 1e-3 && Math.abs(ctx.look.yaw) < 1e-4 && Math.abs(ctx.look.pitch) < 1e-4) return;
    const split = lookSplit({ yaw: ctx.look.yaw, pitch: ctx.look.pitch, headJoint: ctx.headJoint });
    const pitchAxis = ctx._v1.crossVectors(fwd, ctx.AX.y);
    if (pitchAxis.lengthSq() < 1e-6) return;
    pitchAxis.normalize();
    ctx.rotWorld(ctx.neck, ctx.AX.y, split.neck.yaw);
    ctx.rotWorld(ctx.neck, pitchAxis, split.neck.pitch);
    if (ctx.headJoint) { ctx.rotWorld(ctx.head, ctx.AX.y, split.head.yaw); ctx.rotWorld(ctx.head, pitchAxis, split.head.pitch); }
  };

  ctx.applyTalk = function applyTalk(dt, now) {
    ctx.env = stepEnvelope(ctx.env, ctx.speakingTarget, dt);
    if (ctx.env.onset && ctx.life) {
      ctx.nods.push({ t0: now, amp: (3 + 2 * Math.min(1, ctx.speakingTarget)) * DEG });
      if (ctx.nods.length > 3) ctx.nods.shift();
    }
    if (ctx.speakingTarget > 0.08) { ctx.lastLoudAt = now; ctx.inPhrase = true; } else if (ctx.inPhrase && now - ctx.lastLoudAt > 350) {
      ctx.inPhrase = false; ctx.rollAt = now; ctx.triggerBlink(now); // phrase end
    }
    ctx.talkPresence += ((ctx.inPhrase ? 1 : 0) - ctx.talkPresence) * (1 - Math.exp(-dt * 2.2));
    ctx.pushIn = ctx.life && !ctx.reducedMotion ? ctx.talkPresence : 0;
    if (!ctx.life || ctx.reducedMotion) return;
    let nod = 0;
    for (const n of ctx.nods) nod += nodCurve((now - n.t0) / 1000, n.amp);
    while (ctx.nods.length && now - ctx.nods[0].t0 > 1500) ctx.nods.shift();
    const roll = nodCurve((now - ctx.rollAt) / 1000, 1 * DEG, 0.25);
    const yaw = 1.5 * DEG * ctx.talkPresence * Math.sin((now / 1000) * 2 * Math.PI * 0.23);
    if (!nod && !roll && Math.abs(yaw) < 1e-5) return;
    const bone = ctx.faceBone;
    if (!bone) return;
    bone.getWorldQuaternion(ctx._q1);
    const fwd = ctx._v2.copy(ctx.restFwd).applyQuaternion(ctx._q1);
    const pitchAxis = ctx._v1.crossVectors(fwd, ctx.AX.y);
    if (pitchAxis.lengthSq() < 1e-6) return;
    pitchAxis.normalize();
    ctx.rotWorld(bone, pitchAxis, -nod);
    ctx.rotWorld(bone, ctx.AX.y, yaw);
    ctx.rotWorld(bone, fwd.normalize(), roll);
    // Short emphases in the existing conversational clip: wrists turn with
    // syllables, one forearm leads each phrase. All offsets are rebuilt from
    // the clean animation pose every frame, so silence leaves no residual pose.
    const emphasis = ctx.env.fast * ctx.talkPresence;
    ctx.rotWorld(ctx.bones.RightForeArm, ctx.AX.x, -0.09 * emphasis);
    ctx.rotWorld(ctx.bones.RightHand, ctx.AX.y, 0.08 * emphasis * Math.sin(now / 430));
    ctx.rotWorld(ctx.bones.LeftHand, ctx.AX.z, -0.05 * emphasis * Math.sin(now / 600));
  };

  ctx.eyeElevation = function eyeElevation(bone) {
    bone.getWorldQuaternion(ctx._q1);
    const fwd = ctx._v2.copy(ctx.restFwd).applyQuaternion(ctx._q1);
    const up = ctx._v3.copy(ctx.restUp).applyQuaternion(ctx._q1);
    const eye = ctx._f1.copy(ctx.eyeLocal).applyMatrix4(bone.matrixWorld);
    const c = ctx._f2.subVectors(ctx.camera.position, eye).normalize();
    // camera azimuth around the face (0 = straight in front): from the side the brim
    // covers the near eye sooner, so the limit comes down as she turns away
    const right = ctx._f1.crossVectors(fwd, up);
    ctx.guard.az = Math.atan2(Math.abs(c.dot(right)), c.dot(fwd));
    return Math.asin(clamp(c.dot(up), -1, 1)); // + = camera above the face's plane (brim side)
  };

  ctx.applyEyeGuard = function applyEyeGuard(dt) {
    const bone = ctx.faceBone;
    ctx.guard.w += ((ctx.stateName === 'sleep' ? 0.2 : 1) - ctx.guard.w) * (1 - Math.exp(-dt * 3));
    ctx.guard.lift = 0;
    if (!bone || !ctx.life) return;
    const e = ctx.eyeElevation(bone);
    ctx.guard.e0 = e; ctx.guard.e1 = e;
    const side = ctx.GUARD.side * smoothstep(20 * DEG, 70 * DEG, ctx.guard.az);
    if (!ctx.GUARD.on || e <= ctx.GUARD.start - side) return;
    const goal = e - ctx.guard.w * (e - softLimit(e, ctx.GUARD.start - side, ctx.GUARD.max - side));
    for (let i = 0; i < 2; i++) { // second pass corrects for yaw (the lift then acts partly sideways)
      const cur = i ? ctx.eyeElevation(bone) : e;
      const lift = cur - goal;
      if (lift <= 1e-4) break;
      const axis = ctx._v1.crossVectors(ctx._v2, ctx._v3); // fwd × up (both set by eyeElevation): + lifts the face
      if (axis.lengthSq() < 1e-8) break;
      ctx.rotWorld(bone, axis.normalize(), lift);
      ctx.guard.lift += lift;
    }
    ctx.guard.e1 = ctx.eyeElevation(bone);
  };

  ctx.scheduleBlink = function scheduleBlink(now) {
    const b = nextBlink();
    ctx.nextBlinkAt = now + b.delay;
    ctx.pendingDouble = b.double;
  };

  ctx.triggerBlink = function triggerBlink(now) {
    if (ctx.blinkIdx < 0 || ctx.blinkT >= 0 || ctx.stateName === 'sleep' || now - ctx.lastBlinkAt < 700) return;
    ctx.blinkT = 0; ctx.blinkSlow = ctx.SLOW_BLINK_MOODS.has(ctx.sm.mood); ctx.pendingDouble = false; ctx.lastBlinkAt = now;
  };

  ctx.applyBlink = function applyBlink(dt, now) {
    if (ctx.blinkIdx < 0) return;
    ctx.lid += ((ctx.stateName === 'sad' ? 0.18 : 0) - ctx.lid) * (1 - Math.exp(-dt * 2));
    let v = 0;
    if (ctx.stateName === 'sleep') v = 1;
    else if (ctx.blinkT >= 0) {
      ctx.blinkT += dt * 1000;
      v = blinkCurve(ctx.blinkT, ctx.blinkSlow);
      if (v < 0) {
        v = 0;
        if (ctx.pendingDouble) { ctx.pendingDouble = false; ctx.blinkT = -120; } else { ctx.blinkT = -1; ctx.scheduleBlink(now); }
      }
    } else if (ctx.blinkT < -1) { // gap before the second of a double blink
      ctx.blinkT += dt * 1000;
      if (ctx.blinkT >= -1) ctx.blinkT = 0;
    } else if (now >= ctx.nextBlinkAt) { ctx.blinkT = 0; ctx.blinkSlow = ctx.SLOW_BLINK_MOODS.has(ctx.sm.mood); ctx.lastBlinkAt = now; }
    if (ctx.stateName !== 'sleep') v = ctx.lid + (1 - ctx.lid) * v;
    for (const m of ctx.skinned) if (m.morphTargetInfluences) m.morphTargetInfluences[ctx.blinkIdx] = v;
  };
}

// Initialize presence at its original point in the lifecycle.
export function initializePresence(ctx) {

  // Idle arm relax: the idle clip holds the arms tight to the body.
  ctx.armW = 0;

  // Breathing: chest ±0.6°, shoulders ±0.4°, 4.2 s (the CSS `breathe` period).
  ctx.breathPhase = 0;

  // Look-at: toward the camera (and the pointer on desktop), damped, clamped,
  // weighted by state and by how far the clip already turns her head.
  ctx.look = { yaw: 0, pitch: 0, w: 0, sYaw: 0, sPitch: 0, nextSacc: 0, gazeAt: 0, gYaw: 0, gPitch: 0 };
  ctx.pointer = { x: 0, y: 0, until: 0 };

  // Talk layer: nods on speech onsets, a slow yaw drift, a small roll at phrase ends.
  ctx.env = { fast: 0, slow: 0, since: 1, armed: true };
  ctx.nods = [];
  ctx.talkPresence = 0;
  ctx.lastLoudAt = -Infinity;
  ctx.inPhrase = false;
  ctx.rollAt = -Infinity;

  // Eye guard: she never hides her eyes from him under the cap brim. Measure how far
  // the camera sits above the face's own horizontal plane (head space, from the
  // eyes) and, past GUARD.start, lift the head-carrying bone about its own right
  // axis so that elevation eases toward GUARD.max (soft limit) instead of growing.
  // Pitch only: yaw, roll and the rest of the clip's character stay.
  ctx.GUARD = { start: EYE_GUARD.start * DEG, max: EYE_GUARD.max * DEG, side: EYE_GUARD.side * DEG, on: true };
  ctx.guard = { w: 1, e0: 0, e1: 0, lift: 0, az: 0 };

  // Blinks: random gaps (logic.js nextBlink) + on gaze shifts and phrase ends;
  // slow in tender moods, a little lid droop when she's sad.
  ctx.SLOW_BLINK_MOODS = new Set(['tender', 'affectionate', 'drowsy']);
  ctx.lid = 0;

}
