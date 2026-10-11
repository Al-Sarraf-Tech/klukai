// Build-time hair bone chains for the viewer's spring physics. The rip has no
// hair bones: the hair was skinned to the spine, arms and thighs. The build fits
// a polyline through each hair mass (ponytail, back hair, side locks), adds a
// bone per segment under the Head, and reweights the hair to the chain:
//   head share h = max(root blend along the chain, closeness to the head), the
//   rest is interpolated between bone segment midpoints (two bones at most).
// Arm, thigh and spine influences are dropped. Unanimated, the chains follow
// the head rigidly; the viewer's springs swing them.
// Pure functions on plain arrays: unit-tested without assets.

/** Per-island geometry summary: { faces, top, bottom, centroid, lo, hi } (bind metres). */
export function islandStats(positions, faceV, island) {
  let top = null; let bottom = null;
  const lo = [Infinity, Infinity, Infinity]; const hi = [-Infinity, -Infinity, -Infinity]; const c = [0, 0, 0];
  for (const f of island) {
    for (let k = 0; k < 3; k++) {
      const v = faceV[f * 3 + k];
      const p = [positions[v * 3], positions[v * 3 + 1], positions[v * 3 + 2]];
      if (!top || p[1] > top[1]) top = p;
      if (!bottom || p[1] < bottom[1]) bottom = p;
      for (let i = 0; i < 3; i++) { lo[i] = Math.min(lo[i], p[i]); hi[i] = Math.max(hi[i], p[i]); c[i] += p[i] / (island.length * 3); }
    }
  }
  return { faces: island.length, top, bottom, centroid: c, lo, hi };
}

/**
 * Seed rule: the island's top point (its root) and bottom decide membership.
 * sel: { topMinY, topMaxY, topMinZ, topMaxZ, bottomMaxY, side (+1: x > 0, −1: x < 0) }
 */
export function matchesSeed(st, sel) {
  const t = st.top;
  if (sel.topMinY !== undefined && t[1] < sel.topMinY) return false;
  if (sel.topMaxY !== undefined && t[1] > sel.topMaxY) return false;
  if (sel.topMinZ !== undefined && t[2] < sel.topMinZ) return false;
  if (sel.topMaxZ !== undefined && t[2] > sel.topMaxZ) return false;
  if (sel.bottomMaxY !== undefined && st.bottom[1] > sel.bottomMaxY) return false;
  if (sel.side && Math.sign(st.centroid[0]) !== Math.sign(sel.side)) return false;
  return true;
}

/** Is a point inside box [x0,y0,z0,x1,y1,z1]? */
export function inBox3(p, b) {
  return p[0] >= b[0] && p[1] >= b[1] && p[2] >= b[2] && p[0] <= b[3] && p[1] <= b[4] && p[2] <= b[5];
}

/**
 * Polyline through a hair mass: `bones + 1` points from `yTop` down to `yBottom`,
 * each the centroid (x, z) of the points within ±slice/2 of that height (falls
 * back to the nearest non-empty level, then to the overall centroid).
 * @param points [[x,y,z], …]
 */
export function fitPolyline(points, { bones, yTop, yBottom, slice = null }) {
  const n = bones + 1;
  const step = (yTop - yBottom) / bones;
  const half = (slice ?? Math.abs(step)) / 2;
  const all = points.reduce((a, p) => [a[0] + p[0] / points.length, a[1], a[2] + p[2] / points.length], [0, 0, 0]);
  const level = [];
  for (let k = 0; k < n; k++) {
    const y = yTop - k * step;
    let sx = 0; let sz = 0; let m = 0;
    for (const p of points) if (Math.abs(p[1] - y) <= half) { sx += p[0]; sz += p[2]; m++; }
    level.push(m ? [sx / m, y, sz / m] : null);
  }
  return level.map((p, k) => {
    if (p) return p;
    for (let d = 1; d < n; d++) for (const j of [k - d, k + d]) if (level[j]) return [level[j][0], yTop - k * step, level[j][2]];
    return [all[0], yTop - k * step, all[2]];
  });
}

/** Nearest point on a polyline: { s (arc length from the first point), seg, t, dist }. */
export function projectOnPolyline(poly, p) {
  let best = null; let acc = 0;
  for (let k = 0; k < poly.length - 1; k++) {
    const a = poly[k]; const b = poly[k + 1];
    const ab = [b[0] - a[0], b[1] - a[1], b[2] - a[2]];
    const len2 = ab[0] ** 2 + ab[1] ** 2 + ab[2] ** 2;
    const len = Math.sqrt(len2);
    let t = len2 > 0 ? ((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1] + (p[2] - a[2]) * ab[2]) / len2 : 0;
    t = Math.min(1, Math.max(k === 0 ? -Infinity : 0, t)); // before the root: negative s
    const q = [a[0] + ab[0] * t, a[1] + ab[1] * t, a[2] + ab[2] * t];
    const dist = Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]);
    if (!best || dist < best.dist) best = { s: acc + t * len, seg: k, t, dist };
    acc += len;
  }
  return best;
}

/** Arc length of each polyline point from the first. */
export function arcLengths(poly) {
  const out = [0];
  for (let k = 1; k < poly.length; k++) out.push(out[k - 1] + Math.hypot(poly[k][0] - poly[k - 1][0], poly[k][1] - poly[k - 1][1], poly[k][2] - poly[k - 1][2]));
  return out;
}

/**
 * Bone weights at arc length s on a chain of `poly.length − 1` bones (bone k
 * spans poly[k] → poly[k+1]). Interpolates between segment midpoints, so a
 * joint is shared 50/50 and each segment's middle is its own bone.
 * @returns [{ bone, w }] (1 or 2 entries, sum 1)
 */
export function chainBoneWeights(arc, s) {
  const nb = arc.length - 1;
  const mids = []; for (let k = 0; k < nb; k++) mids.push((arc[k] + arc[k + 1]) / 2);
  if (s <= mids[0]) return [{ bone: 0, w: 1 }];
  if (s >= mids[nb - 1]) return [{ bone: nb - 1, w: 1 }];
  for (let k = 0; k < nb - 1; k++) {
    if (s <= mids[k + 1]) {
      const t = (s - mids[k]) / (mids[k + 1] - mids[k]);
      return [{ bone: k, w: 1 - t }, { bone: k + 1, w: t }].filter((x) => x.w > 0);
    }
  }
  return [{ bone: nb - 1, w: 1 }];
}

function smooth01(e0, e1, x) {
  const t = Math.min(1, Math.max(0, (x - e0) / (e1 - e0)));
  return t * t * (3 - 2 * t);
}

/**
 * Full weights for one hair vertex on a chain.
 * @param head { center, radius: [rRigid, rFree] } — fully head inside rRigid, free beyond rFree
 * @param rootBlend arc length over which the chain takes over from the head
 * @returns { head: share, bones: [{ bone, w }] } (head + Σ bones = 1)
 */
export function hairVertexWeights({ poly, arc, p, head, rootBlend }) {
  const pr = projectOnPolyline(poly, p);
  const dHead = Math.hypot(p[0] - head.center[0], p[1] - head.center[1], p[2] - head.center[2]);
  const h = Math.max(1 - smooth01(0, rootBlend, pr.s), 1 - smooth01(head.radius[0], head.radius[1], dHead));
  const bones = h >= 1 ? [] : chainBoneWeights(arc, Math.max(0, pr.s)).map((x) => ({ bone: x.bone, w: x.w * (1 - h) }));
  return { head: h, bones, dist: pr.dist };
}
