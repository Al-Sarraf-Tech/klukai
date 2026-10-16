import { SPRING, SPRING_H, closestOnSegment, poseJumped, springHang, springStep, springSubsteps } from './physics.js';
import { DEG, clamp, wrapPi } from './common.js';

// Springs operations on the single avatar runtime owner.
export function installSprings(ctx) {
// A chain the springs can run: every bone present, every segment (and the tail) non-zero.
  ctx.chainUsable = function chainUsable(ch) {
    const list = (Array.isArray(ch?.bones) ? ch.bones : []).map((n) => ctx.root.getObjectByName(n));
    if (!list.length || list.some((b) => !b)) return false;
    const tail = Array.isArray(ch.tail) ? ch.tail : [0, -0.1, 0];
    return list.every((b, i) => (i < list.length - 1 ? list[i + 1].position.length() : Math.hypot(...tail)) > 1e-4);
  };

  ctx.buildSprings = function buildSprings(ud) {
    const chains = [];
    for (const ch of ud.springChains || []) {
      if (!ctx.chainUsable(ch)) continue;
      const list = ch.bones.map((n) => ctx.root.getObjectByName(n));
      const set = /side|lock|front/i.test(ch.name || '') ? SPRING.lock : SPRING.long;
      const joints = list.map((bone, i) => {
        const tail = i < list.length - 1 ? list[i + 1].position.clone() : new ctx.THREE.Vector3().fromArray(ch.tail || [0, -0.1, 0]);
        const f = list.length > 1 ? i / (list.length - 1) : 0;
        const j = {
          bone, initQ: bone.quaternion.clone(), axis: tail.clone().normalize(), len: tail.length(),
          stiffness: set.stiffness[0] + (set.stiffness[1] - set.stiffness[0]) * f, drag: set.drag, gravity: set.gravity, hit: set.hitRadius,
          maxAngle: (set.maxAngle || 0) * DEG, cur: [0, 0, 0], prev: [0, 0, 0], head: [0, 0, 0], rest: [0, 0, 0],
        };
        // one params object per joint, reused every step (no per-frame allocation)
        j.p = { cur: j.cur, prev: j.prev, head: j.head, len: j.len, restDir: j.rest, stiffness: 0, drag: 0, gravity: 0, gravityDir: [0, -1, 0], dt: SPRING_H, colliders: null, hitRadius: 0, maxAngle: 0 };
        return j;
      });
      if (joints.every((j) => j.len > 1e-4)) chains.push({ name: ch.name, joints, lock: set === SPRING.lock });
    }
    if (!chains.length) return null;
    const colliders = [];
    for (const c of ud.springColliders || []) {
      const bone = ctx.root.getObjectByName(c?.bone);
      const to = c?.to ? ctx.root.getObjectByName(c.to.bone) : null;
      if (!bone || !(c.radius > 0) || (c.to && !to)) continue;
      colliders.push({
        bone, off: new ctx.THREE.Vector3().fromArray(c.offset || [0, 0, 0]), to, toOff: to ? new ctx.THREE.Vector3().fromArray(c.to.offset || [0, 0, 0]) : null,
        shape: to ? { a: [0, 0, 0], b: [0, 0, 0], r: c.radius } : { c: [0, 0, 0], r: c.radius },
      });
    }
    // + the floor (she lies down in `sleep`; crouches): hair rests on it instead of sinking
    const shapes = [...colliders.map((c) => c.shape), { floorY: 0 }];
    // side locks hang in front: only head, neck, chest and shoulders can reach them
    const near = shapes.filter((sh, i) => i >= colliders.length || /Head|Neck|Spine2|Arm$/.test(colliders[i].bone.name));
    for (const ch of chains) for (const j of ch.joints) j.p.colliders = ch.lock ? near : shapes;
    // bones whose world matrices the springs read, parents first (not the whole scene graph)
    const need = new Set();
    const addUp = (b) => { for (let x = b; x && x !== ctx.root; x = x.parent) need.add(x); };
    if (ctx.head) addUp(ctx.head);
    for (const c of colliders) { addUp(c.bone); if (c.to) addUp(c.to); }
    for (const ch of chains) addUp(ch.joints[0].bone.parent);
    const order = [];
    ctx.root.traverse((o) => { if (need.has(o)) order.push(o); }); // traverse = parents before children
    return { chains, colliders, shapes, order, acc: 0, hold: 0, primed: false, lastPos: new ctx.THREE.Vector3(), lastQ: new ctx.THREE.Quaternion(), ms: 0, mode: 'hang' };
  };

  ctx.toCentre = function toCentre(v, out) { v.applyMatrix4(ctx._rootInv); out[0] = v.x; out[1] = v.y; out[2] = v.z; };

// one joint: rest direction from the (already updated) parent, then sim / hang / aim, then rotate the bone at its tail
  ctx.solveJoint = function solveJoint(j, mode) {
    const parent = j.bone.parent;
    ctx._sq.setFromRotationMatrix(parent.matrixWorld).multiply(j.initQ); // bone's rest frame, world
    ctx._sh.copy(j.bone.position).applyMatrix4(parent.matrixWorld); // joint origin, world
    ctx.toCentre(ctx._sv.copy(ctx._sh), j.head);
    ctx._sv.copy(j.axis).applyQuaternion(ctx._sq).applyQuaternion(ctx._rootQ); // rest direction, centre space
    j.rest[0] = ctx._sv.x; j.rest[1] = ctx._sv.y; j.rest[2] = ctx._sv.z;
    const p = j.p;
    p.stiffness = j.stiffness; p.drag = j.drag; p.gravity = j.gravity; p.hitRadius = j.hit; p.maxAngle = j.maxAngle;
    if (mode === 'sim') {
      springStep(ctx._tmp, p);
      j.prev[0] = j.cur[0]; j.prev[1] = j.cur[1]; j.prev[2] = j.cur[2];
      j.cur[0] = ctx._tmp[0]; j.cur[1] = ctx._tmp[1]; j.cur[2] = ctx._tmp[2];
    } else if (mode === 'hang') {
      springHang(j.cur, p);
      j.prev[0] = j.cur[0]; j.prev[1] = j.cur[1]; j.prev[2] = j.cur[2];
    }
    // aim: tail direction in the bone's rest frame → rotation from the bind axis
    ctx._sv.set(j.cur[0], j.cur[1], j.cur[2]).applyMatrix4(ctx.root.matrixWorld).sub(ctx._sh).applyQuaternion(ctx._sq.invert());
    if (ctx._sv.lengthSq() < 1e-12) return;
    ctx._sqa.setFromUnitVectors(j.axis, ctx._sv.normalize());
    j.bone.quaternion.copy(j.initQ).multiply(ctx._sqa);
    j.bone.updateMatrix();
    j.bone.matrixWorld.multiplyMatrices(parent.matrixWorld, j.bone.matrix);
  };

  ctx.updateSprings = function updateSprings(dt, now) {
    const S = ctx.springs;
    if (!S) return;
    const t0 = performance.now();
    // the procedural layers moved bones: refresh just the matrices the springs read
    ctx.root.updateWorldMatrix(true, false);
    for (const b of S.order) { b.updateMatrix(); b.matrixWorld.multiplyMatrices(b.parent.matrixWorld, b.matrix); }
    ctx._rootInv.copy(ctx.root.matrixWorld).invert();
    ctx._rootQ.setFromRotationMatrix(ctx._rootInv);
    for (const c of S.colliders) {
      if (c.to) {
        ctx.toCentre(ctx._sv.copy(c.off).applyMatrix4(c.bone.matrixWorld), c.shape.a);
        ctx.toCentre(ctx._sv.copy(c.toOff).applyMatrix4(c.to.matrixWorld), c.shape.b);
      } else ctx.toCentre(ctx._sv.copy(c.off).applyMatrix4(c.bone.matrixWorld), c.shape.c);
    }
    let jumped = !S.primed;
    if (ctx.head) {
      ctx.head.matrixWorld.decompose(ctx._sv, ctx._sq, ctx._sScale); // refreshed above (head is in S.order)
      if (S.primed) jumped = poseJumped({ moved: ctx._sv.distanceTo(S.lastPos), turned: S.lastQ.angleTo(ctx._sq), dt });
      S.lastPos.copy(ctx._sv); S.lastQ.copy(ctx._sq);
    }
    S.primed = true;
    S.hold = Math.max(0, S.hold - Math.max(0, dt)); // seconds left to hang (clock-free: works for debug.run too)
    const hang = jumped || !ctx.life || ctx.reducedMotion || S.hold > 0;
    if (hang) {
      S.acc = 0; S.mode = 'hang';
      for (const ch of S.chains) for (const j of ch.joints) ctx.solveJoint(j, 'hang');
    } else {
      const st = springSubsteps(S.acc, dt, SPRING_H);
      S.acc = st.acc; S.mode = 'sim';
      if (!st.n) for (const ch of S.chains) for (const j of ch.joints) ctx.solveJoint(j, 'aim'); // keep tails world-steady
      for (let i = 0; i < st.n; i++) for (const ch of S.chains) for (const j of ch.joints) ctx.solveJoint(j, 'sim');
    }
    S.ms = S.ms ? S.ms * 0.95 + (performance.now() - t0) * 0.05 : performance.now() - t0;
  };

// smallest gap between any spring tail and any collider surface (debug; < 0 = inside)
  ctx.springClearance = function springClearance() {
    if (!ctx.springs) return null;
    let min = Infinity; let at = '';
    const nameOf = new Map(ctx.springs.colliders.map((c) => [c.shape, c.bone.name]));
    for (const ch of ctx.springs.chains) for (const j of ch.joints) for (const c of j.p.colliders) {
      let g;
      if (c.floorY !== undefined) g = j.cur[1] - c.floorY;
      else {
        const q = c.b ? closestOnSegment([0, 0, 0], j.cur, c.a, c.b) : c.c;
        g = Math.hypot(j.cur[0] - q[0], j.cur[1] - q[1], j.cur[2] - q[2]) - c.r;
      }
      if (g < min) { min = g; at = `${ch.name}→${nameOf.get(c) || 'floor'}`; }
    }
    return { min, at };
  };

  ctx.updateSway = function updateSway(dt, t) {
    ctx.shared.uTime.value = t;
    const on = ctx.life && !ctx.reducedMotion && !ctx.hasChains;
    ctx.shared.uSwayAmp.value = on ? 0.004 * ctx.unitsPerMetre : 0;
    const sp = ctx.bones.Spine2;
    if (!on || !sp || dt <= 0) { ctx.sway.fresh = true; ctx.shared.uSway.value.set(0, 0); return; }
    sp.getWorldPosition(ctx._v1);
    ctx.bones.Hips?.getWorldQuaternion(ctx._q1);
    const bf = ctx._v2.copy(ctx.hipsFwd).applyQuaternion(ctx._q1);
    const yaw = Math.atan2(bf.x, bf.z);
    if (ctx.sway.fresh) { ctx.sway.prev.copy(ctx._v1); ctx.sway.prevYaw = yaw; ctx.sway.fresh = false; return; }
    const vx = (ctx._v1.x - ctx.sway.prev.x) / dt; const vz = (ctx._v1.z - ctx.sway.prev.z) / dt;
    const yr = wrapPi(yaw - ctx.sway.prevYaw) / dt;
    ctx.sway.prev.copy(ctx._v1); ctx.sway.prevYaw = yaw;
    const tx = clamp(-vx * 0.08 - yr * 0.01, -0.03, 0.03);
    const tzz = clamp(-vz * 0.08, -0.03, 0.03);
    const w0 = 7; const zeta = 0.35; const h2 = Math.min(dt, 1 / 30);
    ctx.sway.vx += (w0 * w0 * (tx - ctx.sway.x) - 2 * zeta * w0 * ctx.sway.vx) * h2; ctx.sway.x = clamp(ctx.sway.x + ctx.sway.vx * h2, -0.03, 0.03);
    ctx.sway.vz += (w0 * w0 * (tzz - ctx.sway.z) - 2 * zeta * w0 * ctx.sway.vz) * h2; ctx.sway.z = clamp(ctx.sway.z + ctx.sway.vz * h2, -0.03, 0.03);
    ctx.shared.uSway.value.set(ctx.sway.x * ctx.unitsPerMetre, ctx.sway.z * ctx.unitsPerMetre);
  };
}

// Initialize springs at its original point in the lifecycle.
export function initializeSprings(ctx) {
  // Hair springs (when the GLB has chains): three-vrm's VRMSpringBoneJoint on plain
  // bones. Tails live in centre space (the character root), so root or camera moves
  // don't throw the hair; fixed 1/60 s sub-steps; roots before children; after the
  // mixer, look-at and eye guard. "Hang" mode (the step's fixed point, no inertia)
  // covers resets, sleep transitions, big pose jumps and prefers-reduced-motion.
  ctx._rootInv = new ctx.THREE.Matrix4();
  ctx._rootQ = new ctx.THREE.Quaternion();
  ctx._sv = new ctx.THREE.Vector3(); ctx._sh = new ctx.THREE.Vector3();
  ctx._sq = new ctx.THREE.Quaternion(); ctx._sqa = new ctx.THREE.Quaternion(); ctx._sScale = new ctx.THREE.Vector3();
  ctx._tmp = [0, 0, 0];

  ctx.resetSprings = (holdMs = 0) => {
    if (!ctx.springs) return;
    ctx.springs.primed = false;
    ctx.springs.hold = Math.max(ctx.springs.hold, (Number(holdMs) || 0) / 1000);
  };

  // Hair: a damped spring on the torso's motion drives a vertex-shader sway
  // (interim, and the fallback for GLBs without hair chains), plus a faint breeze.
  ctx.sway = { x: 0, vx: 0, z: 0, vz: 0, prev: new ctx.THREE.Vector3(), prevYaw: 0, fresh: true };

  // Contact shadow: one soft blob quad (hips + chest when lying + feet), only in
  // full framing and asleep. No shadow map, no extra skinning pass.

}
