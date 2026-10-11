// Mixamo's auto-rig gave `mixamorigHead` no skin weights: the face, cap and
// bangs ride on the Neck, so every head rotation in the clips (8–55°) and the
// viewer's speaking tilt were invisible. These helpers make Head a real skin
// joint and move the Neck's influence onto it above the jaw line.
//
// Pure functions (typed arrays / matrices passed in, no three.js import) so they
// are unit-tested without the source assets.

/** Hermite smoothstep, 0 below e0, 1 above e1. */
export function smoothstep(e0, e1, x) {
  if (e1 === e0) return x < e0 ? 0 : 1;
  const t = Math.min(1, Math.max(0, (x - e0) / (e1 - e0)));
  return t * t * (3 - 2 * t);
}

/**
 * How much weight moves to the Head at a bind-pose position:
 *   { from: share of the Neck weight, other: share of every other influence }.
 * - `blend` [y0, y1]: the jaw-line band for the face, neck, cap and side locks
 *   (Neck weight only).
 * - `behind` { z: [zStart, zFull], blend: [y0, y1] }: hair hanging behind the
 *   head (the high ponytail). Its root must follow the head, but below the
 *   nape it carries no Neck weight, so a Neck-only transfer shears the tail
 *   there. Behind the head a share of *all* influences moves instead, fading
 *   down the tail over the long band: the tail bends like a rope. Mixed in
 *   smoothly by how far behind the vertex is (zStart → zFull, zFull < zStart);
 *   only for vertices where `hairLike` is true.
 */
export function headShareFn({ blend, behind = null }) {
  return (x, y, z, hairLike = true) => {
    const near = smoothstep(blend[0], blend[1], y);
    if (!behind || !hairLike) return { from: near, other: 0 };
    const b = smoothstep(-behind.z[0], -behind.z[1], -z);
    if (b <= 0) return { from: near, other: 0 };
    const far = smoothstep(behind.blend[0], behind.blend[1], y);
    return { from: (1 - b) * near + b * Math.max(near, far), other: b * far };
  };
}

/**
 * Moves weight onto the `to` joint, in place on the interleaved arrays:
 * share(x, y, z, v) → { from, other } (or a number = Neck-only share) gives the
 * fraction of the `from` joint's weight and of every other influence that goes
 * to `to`. Keeps at most `maxInfluences` per vertex (drops the smallest, then
 * renormalises).
 *
 * @param skinIndex  joint indices, `maxInfluences` per vertex
 * @param skinWeight weights, same layout
 * @param positions  xyz per vertex (bind pose)
 * @param from / to  joint indices (e.g. Neck → Head)
 * @param blend      [y0, y1]: default share = smoothstep over y, Neck only
 * @param otherJoints optional per-joint booleans: only these may give the `other` share
 *                   (e.g. the spine chain; arm-weighted hair keeps draping over the shoulders)
 * @returns { touched, full, dropped } vertex counts (any weight moved / all of `from` moved / an influence dropped)
 */
export function reweightToChild({ skinIndex, skinWeight, positions, from, to, blend, share = null, maxInfluences = 4, otherJoints = null }) {
  const shareAt = share || ((x, y) => smoothstep(blend[0], blend[1], y));
  const n = positions.length / 3;
  let touched = 0; let full = 0; let dropped = 0;
  const idx = new Array(maxInfluences + 1);
  const wts = new Array(maxInfluences + 1);
  const clamp = (s) => Math.min(1, Math.max(0, s || 0));
  for (let v = 0; v < n; v++) {
    const o = v * maxInfluences;
    let hasFrom = false;
    for (let k = 0; k < maxInfluences; k++) if (skinIndex[o + k] === from && skinWeight[o + k] > 0) { hasFrom = true; break; }
    const s = shareAt(positions[v * 3], positions[v * 3 + 1], positions[v * 3 + 2], v);
    const sFrom = clamp(typeof s === 'number' ? s : s.from);
    const sOther = clamp(typeof s === 'number' ? 0 : s.other);
    if (!(hasFrom && sFrom > 0) && !(sOther > 0)) continue;
    // Split every influence: the moved part accumulates on `to`.
    let m = 0;
    let moved = 0;
    let toAt = -1;
    for (let k = 0; k < maxInfluences; k++) {
      const j = skinIndex[o + k]; const x = skinWeight[o + k];
      if (!(x > 0)) continue;
      if (j === to) { toAt = m; idx[m] = j; wts[m] = x; m++; continue; }
      const sh = j === from ? sFrom : (otherJoints && !otherJoints[j] ? 0 : sOther);
      moved += sh * x;
      idx[m] = j; wts[m] = (1 - sh) * x; m++;
    }
    if (!(moved > 0)) continue;
    touched++;
    if (hasFrom && sFrom >= 1) full++;
    if (toAt >= 0) wts[toAt] += moved;
    else { idx[m] = to; wts[m] = moved; m++; }
    // drop zero weights, then the smallest until it fits. The `to` joint is
    // always kept: dropping it would undo the transfer (and tear the blend).
    const order = [];
    for (let k = 0; k < m; k++) if (wts[k] > 1e-6) order.push(k);
    order.sort((a, b) => (idx[b] === to) - (idx[a] === to) || wts[b] - wts[a]);
    if (order.length > maxInfluences) dropped++;
    const keep = order.slice(0, maxInfluences);
    const sum = keep.reduce((acc, k) => acc + wts[k], 0) || 1;
    for (let k = 0; k < maxInfluences; k++) {
      if (k < keep.length) { skinIndex[o + k] = idx[keep[k]]; skinWeight[o + k] = wts[keep[k]] / sum; } else { skinIndex[o + k] = 0; skinWeight[o + k] = 0; }
    }
  }
  return { touched, full, dropped };
}

/**
 * Inverse bind matrix for a bone that was not a skin joint, consistent with its
 * parent's bind pose: bindWorld(child) = bindWorld(parent) · restWorld(parent)⁻¹ · restWorld(child).
 * (Equals restWorld(child)⁻¹ when the rest pose is the bind pose, as in Mixamo FBX.)
 * Takes THREE.Matrix4-like objects (clone/invert/multiply) so this file stays import-free.
 */
export function childBindInverse({ parentInverse, parentWorld, childWorld }) {
  return parentInverse.clone().invert()
    .multiply(parentWorld.clone().invert())
    .multiply(childWorld)
    .invert();
}

/** Sum of each joint's weight over all vertices (for logs and tests). */
export function jointWeightTotals(skinIndex, skinWeight, jointCount) {
  const t = new Float64Array(jointCount);
  for (let i = 0; i < skinIndex.length; i++) if (skinWeight[i] > 0) t[skinIndex[i]] += skinWeight[i];
  return t;
}
