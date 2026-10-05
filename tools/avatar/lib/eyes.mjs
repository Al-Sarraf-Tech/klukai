// The source face is a baked wink: the model's right eye (−x) is open, the
// left (+x) lid is closed over its iris disc. Mixamo dropped the shape keys,
// so we rebuild both states from geometry:
//   open   = each closed-side eye vertex takes the MIRROR of its counterpart on
//            the open side (matched by mirrored or identical UV, per group)
//   closed = each open-side eye vertex takes the mirror of its closed-side match
// The open state becomes the base mesh; (closed − open) is a "blink" morph target.
// Iris/highlight discs (one shared full-square UV layout) are already symmetric
// and are left untouched; matching them by UV would scramble them.
import { uvIslands } from './atlas-assign.mjs';

/** Region around both eyes (metres, bind pose). */
export const EYE_BOX = Object.freeze({ y0: 1.35, y1: 1.43, ax0: 0.003, ax1: 0.085, z0: -0.02, z1: 0.2 });

function inEye(x, y, z, box = EYE_BOX) {
  const ax = Math.abs(x);
  return y >= box.y0 && y <= box.y1 && ax >= box.ax0 && ax <= box.ax1 && z >= box.z0 && z <= box.z1;
}

/**
 * @param geo   non-indexed THREE.BufferGeometry (FBX order = OBJ face order)
 * @param obj   readObjFaces() result
 * @param groups material names that take part (face, iris, lid overlays…)
 * @param openSide −1 if the −x eye is the open one
 * @param modes  per-group matching: 'mirror' (default) or 'auto' (discs)
 * @returns { moved, matched, unmatched, morph: Float32Array } (morph = closed−open per corner)
 */
export function openEyesAndBuildBlink(geo, obj, groups, { openSide = -1, uvTol = 0.006, posTol = 0.025, modes = {}, discUvSpan = 0.9, maxLid = 0.03, fillRadius = 0.006, scleraUv = [], tuck = 0.006, browFade = null } = {}) {
  const P = geo.attributes.position.array;
  const N = geo.attributes.normal.array;
  const gids = new Set(groups.map((g) => obj.materials.indexOf(g)).filter((i) => i >= 0));
  // Faces of full-square-UV islands (iris / highlight discs) are skipped.
  const triCount = obj.faceMat.length;
  const skip = new Uint8Array(triCount);
  for (const g of gids) {
    const faces = [];
    for (let t = 0; t < triCount; t++) if (obj.faceMat[t] === g) faces.push(t);
    for (const isl of uvIslands(obj, faces)) {
      let u0 = 1; let u1 = 0;
      const lo = [Infinity, Infinity, Infinity]; const hi = [-Infinity, -Infinity, -Infinity];
      for (const f of isl) for (let k = 0; k < 3; k++) {
        const u = obj.uvs[obj.faceT[f * 3 + k] * 2]; u0 = Math.min(u0, u); u1 = Math.max(u1, u);
        const v = obj.faceV[f * 3 + k];
        for (let i = 0; i < 3; i++) { lo[i] = Math.min(lo[i], obj.positions[v * 3 + i]); hi[i] = Math.max(hi[i], obj.positions[v * 3 + i]); }
      }
      const size = Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]);
      if (u1 - u0 >= discUvSpan && size < 0.05) for (const f of isl) skip[f] = 1; // small full-UV island = disc
    }
  }
  // Collect eye-region corners per (group, side).
  const corners = new Map(); // key gid → { open: [], closed: [] }
  for (let t = 0; t < triCount; t++) {
    const g = obj.faceMat[t];
    if (!gids.has(g) || skip[t]) continue;
    for (let k = 0; k < 3; k++) {
      const c = t * 3 + k;
      const x = P[c * 3]; const y = P[c * 3 + 1]; const z = P[c * 3 + 2];
      if (!inEye(x, y, z)) continue;
      const ti = obj.faceT[c];
      const rec = { c, u: obj.uvs[ti * 2], v: obj.uvs[ti * 2 + 1], x, y, z };
      if (!corners.has(g)) corners.set(g, { open: [], closed: [] });
      (Math.sign(x) === openSide ? corners.get(g).open : corners.get(g).closed).push(rec);
    }
  }

  const original = Float32Array.from(P);
  const originalN = Float32Array.from(N);
  const morph = new Float32Array(P.length);
  let moved = 0; let matched = 0; let unmatched = 0;

  const cell = uvTol;
  const indexBy = (list) => {
    const m = new Map();
    for (const r of list) {
      const key = `${Math.floor(r.u / cell)},${Math.floor(r.v / cell)}`;
      if (!m.has(key)) m.set(key, []);
      m.get(key).push(r);
    }
    return m;
  };
  const nearest = (grid, u, v, x, y, z) => {
    let best = null; let bd = Infinity;
    const cx = Math.floor(u / cell); const cy = Math.floor(v / cell);
    for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
      const l = grid.get(`${cx + dx},${cy + dy}`);
      if (!l) continue;
      for (const r of l) {
        const duv = Math.hypot(r.u - u, r.v - v);
        if (duv > uvTol) continue;
        const dp = Math.hypot(-r.x - x, r.y - y, r.z - z); // mirrored position distance
        if (dp > posTol) continue;
        const d = duv * 4 + dp;
        if (d < bd) { bd = d; best = r; }
      }
    }
    return best ? { r: best, d: bd } : null;
  };
  // Lids and brows have mirrored UV layouts (u <-> 1-u) and must use the
  // mirrored match even when the lid moved ~1 cm. Iris/overlay discs share one
  // UV layout for both eyes: there the identical-UV vertex is an exact mirror.
  const match = (a, gridB, mode) => {
    const m1 = nearest(gridB, 1 - a.u, a.v, a.x, a.y, a.z);
    if (mode === 'mirror') return m1?.r ?? null;
    const m2 = nearest(gridB, a.u, a.v, a.x, a.y, a.z);
    if (m2) {
      const r = m2.r;
      if (Math.hypot(-r.x - a.x, r.y - a.y, r.z - a.z) < 0.003) return r;
    }
    return m1?.r ?? m2?.r ?? null;
  };

  const sclera = [];
  for (const [gid, { open, closed }] of corners) {
    const mode = (modes[obj.materials[gid]] || 'mirror');
    const gOpen = indexBy(open);
    const gClosed = indexBy(closed);
    const missClosed = [];
    const missOpen = [];
    const doneClosed = [];
    const doneOpen = [];
    // Closed side → take mirrored open-side positions (opens the lid).
    for (const r of closed) {
      const m = match(r, gOpen, mode);
      if (!m) { unmatched++; missClosed.push(r); continue; }
      matched++;
      const c = r.c; const mc = m.c;
      P[c * 3] = -original[mc * 3]; P[c * 3 + 1] = original[mc * 3 + 1]; P[c * 3 + 2] = original[mc * 3 + 2];
      N[c * 3] = -originalN[mc * 3]; N[c * 3 + 1] = originalN[mc * 3 + 1]; N[c * 3 + 2] = originalN[mc * 3 + 2];
      if (Math.hypot(original[c * 3] - P[c * 3], original[c * 3 + 1] - P[c * 3 + 1], original[c * 3 + 2] - P[c * 3 + 2]) > maxLid) {
        P[c * 3] = original[c * 3]; P[c * 3 + 1] = original[c * 3 + 1]; P[c * 3 + 2] = original[c * 3 + 2];
        N[c * 3] = originalN[c * 3]; N[c * 3 + 1] = originalN[c * 3 + 1]; N[c * 3 + 2] = originalN[c * 3 + 2];
        unmatched++; matched--; missClosed.push(r); continue;
      }
      doneClosed.push(r);
      for (let i = 0; i < 3; i++) morph[c * 3 + i] = original[c * 3 + i] - P[c * 3 + i];
      if (Math.hypot(morph[c * 3], morph[c * 3 + 1], morph[c * 3 + 2]) > 1e-5) moved++;
    }
    // Open side keeps its geometry; its closed state mirrors the closed side.
    for (const r of open) {
      const m = match(r, gClosed, mode);
      if (!m) { missOpen.push(r); continue; }
      const c = r.c; const mc = m.c;
      const d = [-original[mc * 3] - original[c * 3], original[mc * 3 + 1] - original[c * 3 + 1], original[mc * 3 + 2] - original[c * 3 + 2]];
      if (Math.hypot(...d) > maxLid) { missOpen.push(r); continue; }
      doneOpen.push(r);
      morph[c * 3] = d[0]; morph[c * 3 + 1] = d[1]; morph[c * 3 + 2] = d[2];
    }
    // Unmatched corners would stay put while their neighbours move, leaving
    // spikes. Give them the inverse-distance mean motion of matched neighbours.
    const fill = (miss, done, applyOpen) => {
      for (const r of miss) {
        let w = 0; const acc = [0, 0, 0];
        for (const q of done) {
          const d = Math.hypot(q.x - r.x, q.y - r.y, q.z - r.z);
          if (d > fillRadius) continue;
          const wt = 1 / (d + 1e-4);
          w += wt;
          for (let i = 0; i < 3; i++) acc[i] += wt * morph[q.c * 3 + i];
        }
        if (!w) continue;
        const c = r.c;
        for (let i = 0; i < 3; i++) morph[c * 3 + i] = acc[i] / w;
        if (applyOpen) for (let i = 0; i < 3; i++) P[c * 3 + i] = original[c * 3 + i] - morph[c * 3 + i];
      }
    };
    fill(missClosed, doneClosed, true);
    fill(missOpen, doneOpen, false);
    for (const r of open) if (scleraUv.some((b) => r.u >= b[0] && r.v >= b[1] && r.u <= b[2] && r.v <= b[3])) sclera.push(r.c);
  }
  // The source wink also drops the brow; a blink should not. Fade the morph out
  // above the upper lid so brows/forehead stay put (avoids uncovering gaps).
  if (browFade) {
    const [y0, y1] = browFade;
    for (let c = 0; c < morph.length / 3; c++) {
      if (!morph[c * 3] && !morph[c * 3 + 1] && !morph[c * 3 + 2]) continue;
      const y = P[c * 3 + 1];
      const t = Math.min(1, Math.max(0, (y1 - y) / (y1 - y0)));
      const w = t * t * (3 - 2 * t);
      for (let i = 0; i < 3; i++) morph[c * 3 + i] *= w;
    }
  }
  // Eye whites on the open side stay put while the lid closes over them; tuck
  // them behind the lid in the blink state (after the brow fade, unfaded).
  for (const c of sclera) { morph[c * 3] = 0; morph[c * 3 + 1] = 0; morph[c * 3 + 2] = -tuck; }
  return { moved, matched, unmatched, morph };
}
