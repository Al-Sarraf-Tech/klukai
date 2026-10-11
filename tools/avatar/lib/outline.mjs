// Data for the viewer's inverted-hull outline, shipped as the vertex attribute
// `_OUTLINE` (three: `_outline`, vec4, normalized int8):
//   xyz  smoothed normal: the angle-weighted average of the face normals around
//        every vertex sharing a position, across ALL primitives of both meshes.
//        The source normals are hard-split at UV seams and between parts, so a
//        hull pushed along them cracks open. Where the sum cancels (two-sided
//        cards, back-to-back layers) the vertex keeps its own normal.
//   w    width mask 0..1: 0 on the iris/highlight discs and in the eye, brow and
//        mouth areas of the face (no dark ticks in the creases), ~0.6 on the
//        hands, 1 elsewhere.
// Same space as `normal` (object/bind space). Pure functions: no three.js, no assets.

/** Position → hash key on a `quantum` grid (metres). */
export function positionKey(x, y, z, quantum) {
  return `${Math.round(x / quantum)},${Math.round(y / quantum)},${Math.round(z / quantum)}`;
}

function cornerAngle(ax, ay, az, bx, by, bz) {
  const la = Math.hypot(ax, ay, az); const lb = Math.hypot(bx, by, bz);
  if (la < 1e-12 || lb < 1e-12) return 0;
  return Math.acos(Math.max(-1, Math.min(1, (ax * bx + ay * by + az * bz) / (la * lb))));
}

/**
 * @param prims [{ positions: Float32Array xyz, normals: Float32Array xyz, indices: Uint*Array | null }]
 * @param quantum   grid for "same position" (metres)
 * @param minLength keep the vertex's own normal when |Σ wᵢnᵢ| / Σ wᵢ is below this
 * @returns Float32Array xyz (unit) per primitive
 */
export function smoothOutlineNormals(prims, { quantum = 1e-5, minLength = 0.3 } = {}) {
  const acc = new Map(); // key → [sx, sy, sz, sumAngle]
  const keysOf = prims.map(({ positions: P }) => {
    const keys = new Array(P.length / 3);
    for (let i = 0; i < keys.length; i++) keys[i] = positionKey(P[i * 3], P[i * 3 + 1], P[i * 3 + 2], quantum);
    return keys;
  });
  prims.forEach(({ positions: P, normals: N, indices: I }, pi) => {
    const keys = keysOf[pi];
    const tris = I ? I.length / 3 : P.length / 9;
    for (let t = 0; t < tris; t++) {
      const a = I ? I[t * 3] : t * 3; const b = I ? I[t * 3 + 1] : t * 3 + 1; const c = I ? I[t * 3 + 2] : t * 3 + 2;
      const abx = P[b * 3] - P[a * 3]; const aby = P[b * 3 + 1] - P[a * 3 + 1]; const abz = P[b * 3 + 2] - P[a * 3 + 2];
      const acx = P[c * 3] - P[a * 3]; const acy = P[c * 3 + 1] - P[a * 3 + 1]; const acz = P[c * 3 + 2] - P[a * 3 + 2];
      let nx = aby * acz - abz * acy; let ny = abz * acx - abx * acz; let nz = abx * acy - aby * acx;
      const len = Math.hypot(nx, ny, nz);
      if (len < 1e-14) continue; // degenerate
      nx /= len; ny /= len; nz /= len;
      // Orient the face normal like the authored vertex normals (robust to
      // flipped winding); back-to-back copies then still cancel.
      if (N) {
        const sx = N[a * 3] + N[b * 3] + N[c * 3]; const sy = N[a * 3 + 1] + N[b * 3 + 1] + N[c * 3 + 1]; const sz = N[a * 3 + 2] + N[b * 3 + 2] + N[c * 3 + 2];
        if (sx * nx + sy * ny + sz * nz < 0) { nx = -nx; ny = -ny; nz = -nz; }
      }
      const angA = cornerAngle(abx, aby, abz, acx, acy, acz);
      const bcx = P[c * 3] - P[b * 3]; const bcy = P[c * 3 + 1] - P[b * 3 + 1]; const bcz = P[c * 3 + 2] - P[b * 3 + 2];
      const angB = cornerAngle(-abx, -aby, -abz, bcx, bcy, bcz);
      const angC = Math.max(0, Math.PI - angA - angB);
      for (const [v, w] of [[a, angA], [b, angB], [c, angC]]) {
        if (w <= 0) continue;
        const k = keys[v];
        const s = acc.get(k);
        if (s) { s[0] += nx * w; s[1] += ny * w; s[2] += nz * w; s[3] += w; } else acc.set(k, [nx * w, ny * w, nz * w, w]);
      }
    }
  });
  return prims.map(({ positions: P, normals: N }, pi) => {
    const keys = keysOf[pi];
    const out = new Float32Array(P.length);
    for (let i = 0; i < keys.length; i++) {
      const s = acc.get(keys[i]);
      const l = s ? Math.hypot(s[0], s[1], s[2]) : 0;
      let x; let y; let z;
      if (s && s[3] > 0 && l / s[3] >= minLength) { x = s[0] / l; y = s[1] / l; z = s[2] / l; } else if (N) {
        x = N[i * 3]; y = N[i * 3 + 1]; z = N[i * 3 + 2];
        const ln = Math.hypot(x, y, z) || 1; x /= ln; y /= ln; z /= ln;
      } else { x = 0; y = 0; z = 0; }
      out[i * 3] = x; out[i * 3 + 1] = y; out[i * 3 + 2] = z;
    }
    return out;
  });
}

/** Ellipsoid distance: 1 on the surface of { center, radius }. */
export function ellipsoidDistance(x, y, z, { center, radius }) {
  return Math.hypot((x - center[0]) / radius[0], (y - center[1]) / radius[1], (z - center[2]) / radius[2]);
}

function smooth01(e0, e1, x) {
  const t = Math.min(1, Math.max(0, (x - e0) / (e1 - e0)));
  return t * t * (3 - 2 * t);
}

/**
 * Width mask per vertex.
 * @param positions  xyz (bind metres)
 * @param zero       whole primitive gets 0 (iris discs, additive highlight)
 * @param masked     apply `masks` (face role)
 * @param masks      [{ center, radius, feather }]: 0 inside, ramps to 1 at (1 + feather)·radius
 * @param handShare  optional Float32Array per vertex: summed weight of hand + finger joints
 * @param handWidth  width where the hand share is dominant
 */
export function outlineWidths({ positions, zero = false, masked = false, masks = [], handShare = null, handWidth = 0.6 }) {
  const n = positions.length / 3;
  const w = new Float32Array(n);
  if (zero) return w;
  for (let i = 0; i < n; i++) {
    let v = 1;
    if (masked) {
      for (const m of masks) {
        const d = ellipsoidDistance(positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2], m);
        v = Math.min(v, smooth01(1, 1 + (m.feather ?? 0.5), d));
      }
    }
    if (handShare) v = Math.min(v, 1 + (handWidth - 1) * smooth01(0.35, 0.65, handShare[i]));
    w[i] = v;
  }
  return w;
}

/** Summed weight of the selected joints per vertex (JOINTS_0 / WEIGHTS_0 layout, 4 per vertex). */
export function jointShare(joints, weights, selected) {
  const n = joints.length / 4;
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    let s = 0;
    for (let k = 0; k < 4; k++) if (selected[joints[i * 4 + k]]) s += weights[i * 4 + k];
    out[i] = s;
  }
  return out;
}

/** xyz (unit) + w (0..1) → normalized int8 vec4. */
export function packOutline(xyz, w) {
  const n = w.length;
  const out = new Int8Array(n * 4);
  const q = (x) => Math.round(Math.max(-1, Math.min(1, x)) * 127);
  for (let i = 0; i < n; i++) {
    out[i * 4] = q(xyz[i * 3]); out[i * 4 + 1] = q(xyz[i * 3 + 1]); out[i * 4 + 2] = q(xyz[i * 3 + 2]);
    out[i * 4 + 3] = q(w[i]);
  }
  return out;
}
