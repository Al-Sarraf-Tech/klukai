import { clamp } from './common.js';

export function stepEnvelope(env, level, dt) {
  const prev = env || { fast: 0, slow: 0, since: 1, armed: true };
  const l = clamp(Number(level) || 0, 0, 1);
  const k = (tc) => 1 - Math.exp(-Math.max(0, dt) / tc);
  const fast = prev.fast + (l - prev.fast) * k(l > prev.fast ? 0.03 : 0.12);
  const slow = prev.slow + (l - prev.slow) * k(0.45);
  const since = prev.since + Math.max(0, dt);
  const rise = fast - slow;
  const onset = prev.armed !== false && since > 0.28 && fast > 0.12 && rise > 0.08;
  const armed = onset ? false : prev.armed !== false || rise < 0.03;
  return { fast, slow, since: onset ? 0 : since, onset, armed };
}

/** Pure: nod impulse — rises to `amp` at t = tau (s), then falls off fast
 *  (~17 % at 3·tau, gone by ~5·tau). */
export function nodCurve(t, amp, tau = 0.1) {
  if (!(t >= 0)) return 0;
  const x = t / tau;
  return x > 8 ? 0 : amp * x * x * Math.exp(2 * (1 - x));
}

/** Pure: eyelid 0..1 over a blink, t in ms (fast close, slower open); -1 when done.
 *  `slow` is the ~0.3 s tender blink. */
export function blinkCurve(t, slow = false) {
  const k = slow ? 2.2 : 1;
  const close = 55 * k; const hold = 25 * k; const open = 90 * k;
  if (t < 0) return 0;
  if (t < close) return t / close;
  if (t < close + hold) return 1;
  if (t < close + hold + open) return 1 - (t - close - hold) / open;
  return -1;
}

// ── spring-bone hair (three-vrm VRMSpringBoneJoint on plain bones) ─────────
// Vectors are [x, y, z] arrays in centre space (the character root), metres.

/** Pure: closest point to `p` on segment a–b, written into `out`. */
export function closestOnSegment(out, p, a, b) {
  const ab0 = b[0] - a[0]; const ab1 = b[1] - a[1]; const ab2 = b[2] - a[2];
  const l2 = ab0 * ab0 + ab1 * ab1 + ab2 * ab2;
  let t = l2 > 1e-12 ? ((p[0] - a[0]) * ab0 + (p[1] - a[1]) * ab1 + (p[2] - a[2]) * ab2) / l2 : 0;
  t = t < 0 ? 0 : t > 1 ? 1 : t;
  out[0] = a[0] + ab0 * t; out[1] = a[1] + ab1 * t; out[2] = a[2] + ab2 * t;
  return out;
}

export const _cp = [0, 0, 0];
/** Pure: push point `p` (in place) out of a sphere { c, r }, capsule { a, b, r } or
 *  floor { floorY }, plus `hit` (the hair's own thickness). Returns true when it moved. */
export function collidePoint(p, col, hit = 0) {
  if (col.floorY !== undefined) {
    if (p[1] >= col.floorY + hit) return false;
    p[1] = col.floorY + hit;
    return true;
  }
  const c = col.b ? closestOnSegment(_cp, p, col.a, col.b) : (col.c || col.a);
  const d0 = p[0] - c[0]; const d1 = p[1] - c[1]; const d2 = p[2] - c[2];
  const r = col.r + hit;
  const dist = Math.hypot(d0, d1, d2);
  if (dist >= r || dist < 1e-9) return false;
  const k = r / dist;
  p[0] = c[0] + d0 * k; p[1] = c[1] + d1 * k; p[2] = c[2] + d2 * k;
  return true;
}

/** Pure: keep a tail within `maxAngle` (rad) of the rest direction, on the bone
 *  length (in place). Long hair can't fold back or twist over on a fast turn. */
export function coneClamp(p, head, len, restDir, maxAngle) {
  if (!(maxAngle > 0) || maxAngle >= Math.PI) return false;
  let d0 = p[0] - head[0]; let d1 = p[1] - head[1]; let d2 = p[2] - head[2];
  const l = Math.hypot(d0, d1, d2);
  if (l < 1e-9) return false;
  d0 /= l; d1 /= l; d2 /= l;
  const c = d0 * restDir[0] + d1 * restDir[1] + d2 * restDir[2];
  const cMax = Math.cos(maxAngle);
  if (c >= cMax) return false;
  // component of d orthogonal to rest, normalised; then rest·cos + ortho·sin
  let o0 = d0 - restDir[0] * c; let o1 = d1 - restDir[1] * c; let o2 = d2 - restDir[2] * c;
  let ol = Math.hypot(o0, o1, o2);
  if (ol < 1e-9) { // straight back: any side, as long as it is orthogonal to rest
    const ax = Math.abs(restDir[1]) < 0.9 ? [0, 1, 0] : [1, 0, 0];
    o0 = restDir[1] * ax[2] - restDir[2] * ax[1]; o1 = restDir[2] * ax[0] - restDir[0] * ax[2]; o2 = restDir[0] * ax[1] - restDir[1] * ax[0];
    ol = Math.hypot(o0, o1, o2);
  }
  const sMax = Math.sin(maxAngle);
  p[0] = head[0] + (restDir[0] * cMax + (o0 / ol) * sMax) * len;
  p[1] = head[1] + (restDir[1] * cMax + (o1 / ol) * sMax) * len;
  p[2] = head[2] + (restDir[2] * cMax + (o2 / ol) * sMax) * len;
  return true;
}

export function keepLength(p, head, len) {
  const d0 = p[0] - head[0]; const d1 = p[1] - head[1]; const d2 = p[2] - head[2];
  const l = Math.hypot(d0, d1, d2);
  if (l < 1e-9) return;
  const k = len / l;
  p[0] = head[0] + d0 * k; p[1] = head[1] + d1 * k; p[2] = head[2] + d2 * k;
}

/**
 * Pure: one fixed step of a spring bone's tail (three-vrm VRMSpringBoneJoint):
 * inertia (cur − prev)·(1 − drag) + stiffness·dt toward the rest direction +
 * gravity·dt, then the bone length is restored, the tail is kept within
 * `maxAngle` of the rest direction (0 = no limit) and pushed out of every
 * collider (length restored again). Writes and returns `out`.
 */
export function springStep(out, { cur, prev, head, len, restDir, stiffness, drag, gravity, gravityDir = [0, -1, 0], dt, colliders = [], hitRadius = 0, maxAngle = 0 }) {
  const keep = 1 - drag;
  for (let k = 0; k < 3; k++) {
    out[k] = cur[k] + (cur[k] - prev[k]) * keep + restDir[k] * stiffness * dt + gravityDir[k] * gravity * dt;
  }
  keepLength(out, head, len);
  coneClamp(out, head, len, restDir, maxAngle);
  for (const c of colliders) if (collidePoint(out, c, hitRadius)) keepLength(out, head, len);
  return out;
}

/** Pure: where the tail settles with no motion (the step's fixed point):
 *  along stiffness·restDir + gravity·down, out of the colliders. For resets and
 *  the no-physics fallback (reduced motion), so chains hang instead of sticking out. */
export function springHang(out, { head, len, restDir, stiffness, gravity, gravityDir = [0, -1, 0], colliders = [], hitRadius = 0, maxAngle = 0 }) {
  for (let k = 0; k < 3; k++) out[k] = head[k] + (restDir[k] * stiffness + gravityDir[k] * gravity);
  keepLength(out, head, len);
  if (Math.hypot(out[0] - head[0], out[1] - head[1], out[2] - head[2]) < 1e-9) for (let k = 0; k < 3; k++) out[k] = head[k] + restDir[k] * len;
  coneClamp(out, head, len, restDir, maxAngle); // same limit as springStep: no pop when the sim takes over
  for (const c of colliders) if (collidePoint(out, c, hitRadius)) keepLength(out, head, len);
  return out;
}

/** Pure: fixed 1/60 s sub-steps for a frame. dt is clamped to 0.1 s and at most
 *  `max` steps run; the remainder carries over in `acc`. */
export function springSubsteps(acc, dt, h = 1 / 60, max = 6) {
  let a = acc + Math.min(Math.max(Number(dt) || 0, 0), 0.1);
  let n = Math.floor(a / h + 1e-9);
  a -= n * h;
  if (n > max) { n = max; a = 0; }
  return { n, acc: a };
}

/** Pure: did the head jump (teleport, resumed tab, state snap) so far in one frame
 *  that the springs must be reset rather than simulated? The limits are per 60 Hz
 *  frame and scale with longer frames, so a slow phone's fast turn still simulates. */
export function poseJumped({ moved = 0, turned = 0, dt = 1 / 60 } = {}) {
  const f = Math.max(1, (Number(dt) || 0) * 60);
  return moved > 0.25 * f || turned > 1.0 * f;
}

export const SPRING_H = 1 / 60; // fixed spring sub-step (s)

/** Per-chain settings (stiffness root → tip; maxAngle in degrees from the rest
 *  direction). Ponytail and back hair overlap, so they share one set. The cone is per
 *  joint, so a 5-bone ponytail can still curl 5×; softer, less damped or wider (0.9→0.35
 *  stiffness, 0.42 drag, 35° tried) fans the whole back mass out on nervous' 560°/s turns. */
export const SPRING = Object.freeze({
  long: { stiffness: [3.0, 1.5], gravity: 0.5, drag: 0.5, hitRadius: 0.03, maxAngle: 15 }, // ponytail, back
  lock: { stiffness: [2.0, 1.2], gravity: 0.3, drag: 0.5, hitRadius: 0.015, maxAngle: 25 }, // side locks
});

/** Pure: next idle fidget, never the same one twice in a row. */
