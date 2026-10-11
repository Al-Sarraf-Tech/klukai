// The source model merged several GFL2 sub-meshes into one material group even
// though they sample different texture atlases (e.g. the jacket lives on
// cloth1, the stockings/gear on cloth2). This assigns each UV island to the
// atlas whose pixels are smoothest inside the island: on the right atlas an
// island covers one painted region; on a wrong one it straddles unrelated
// islands and outlines, so the mean interior gradient is much higher.
//
// `sharp` (a native module) is imported lazily inside gradientImage(): the pure
// helpers below (uvIslands, rng) are unit-tested on checkouts that never ran
// `npm ci` in tools/avatar, and a top-level import made the whole test file
// fail to load there.

const N = 1024;

/** Gradient-magnitude image (max of |dx|,|dy| summed over RGB) at N×N. */
export async function gradientImage(file) {
  const { default: sharp } = await import('sharp');
  const { data } = await sharp(file).removeAlpha().resize(N, N, { fit: 'fill' }).raw().toBuffer({ resolveWithObject: true });
  const g = new Float32Array(N * N);
  for (let y = 1; y < N - 1; y++) {
    for (let x = 1; x < N - 1; x++) {
      let gx = 0;
      let gy = 0;
      for (let c = 0; c < 3; c++) {
        gx += Math.abs(data[(y * N + x + 1) * 3 + c] - data[(y * N + x - 1) * 3 + c]);
        gy += Math.abs(data[((y + 1) * N + x) * 3 + c] - data[((y - 1) * N + x) * 3 + c]);
      }
      g[y * N + x] = Math.max(gx, gy);
    }
  }
  return g;
}

/** Deterministic PRNG (mulberry32) so builds are reproducible. */
export function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * UV islands: faces joined when they share an edge with the same two positions
 * AND the same two UVs. (Joining on UV index alone is wrong for Blender OBJs:
 * the exporter de-duplicates identical UV coordinates, so unrelated pieces
 * that happen to share a UV value would merge.)
 */
export function uvIslands(obj, faces) {
  const { faceV, faceT } = obj;
  const parent = new Int32Array(faces.length).map((_, i) => i);
  const find = (a) => {
    while (parent[a] !== a) { parent[a] = parent[parent[a]]; a = parent[a]; }
    return a;
  };
  const edgeOwner = new Map();
  faces.forEach((f, i) => {
    for (let k = 0; k < 3; k++) {
      const v0 = faceV[f * 3 + k]; const v1 = faceV[f * 3 + ((k + 1) % 3)];
      const t0 = faceT[f * 3 + k]; const t1 = faceT[f * 3 + ((k + 1) % 3)];
      const key = v0 < v1 ? `${v0},${v1},${t0},${t1}` : `${v1},${v0},${t1},${t0}`;
      const j = edgeOwner.get(key);
      if (j === undefined) edgeOwner.set(key, i);
      else { const ra = find(i); const rb = find(j); if (ra !== rb) parent[ra] = rb; }
    }
  });
  const islands = new Map();
  faces.forEach((f, i) => {
    const r = find(i);
    if (!islands.has(r)) islands.set(r, []);
    islands.get(r).push(f);
  });
  return [...islands.values()];
}

function islandUvBox(obj, island) {
  let u0 = 1e9; let v0 = 1e9; let u1 = -1e9; let v1 = -1e9;
  for (const f of island) for (let k = 0; k < 3; k++) {
    const t = obj.faceT[f * 3 + k];
    u0 = Math.min(u0, obj.uvs[t * 2]); v0 = Math.min(v0, obj.uvs[t * 2 + 1]);
    u1 = Math.max(u1, obj.uvs[t * 2]); v1 = Math.max(v1, obj.uvs[t * 2 + 1]);
  }
  return [u0, v0, u1, v1].map((x) => +x.toFixed(3));
}

function uvInside(b, o) {
  return b[0] >= o[0] && b[1] >= o[1] && b[2] <= o[2] && b[3] <= o[3];
}

function inBox(p, box) {
  return p[0] >= box[0] && p[1] >= box[1] && p[2] >= box[2] && p[0] <= box[3] && p[1] <= box[4] && p[2] <= box[5];
}

/**
 * First override that claims an island: its centroid inside `box` (OBJ bind
 * metres), its UV bounding box inside `uvBox` (OBJ v-up) and at least
 * `minUvSpan` wide. Omitted criteria match anything. Order matters.
 */
export function matchOverride(overrides, { centroid, uvBox }) {
  return overrides.find((o) => (o.box ? inBox(centroid, o.box) : true) && (o.uvBox ? uvInside(uvBox, o.uvBox) : true)
    && (o.minUvSpan ? uvBox[2] - uvBox[0] >= o.minUvSpan : true)) || null;
}

/**
 * What a slice of a material group ships as. An override may carry its own
 * `role`, `doubleSided` and `name`, and its `atlas` may be a flat colour
 * (`{ color: '#rrggbb' }`) instead of an image. Everything else inherits the
 * group config. `key` identifies one shipped material: (image|colour, role, sidedness).
 * @returns null when the slice is dropped (override atlas: null)
 */
export function partSpec(cfg, atlas, override = null) {
  if (atlas === null) return null;
  const role = override?.role ?? cfg.role;
  const doubleSided = !!(override?.doubleSided ?? cfg.doubleSided);
  const color = typeof atlas === 'object' ? atlas.color.toLowerCase() : null;
  const texture = color ? null : atlas;
  const label = override?.name || (color ? `color-${color.slice(1)}` : texture.replace(/\.png$/i, ''));
  return { texture, color, role, doubleSided, label, key: `${texture ?? color}|${role}|${doubleSided ? 'ds' : 'ss'}` };
}

/** max/min of the per-atlas scores; Infinity when one scores 0 (a perfectly flat region). */
export function scoreRatio(scores) {
  if (scores.length < 2) return Infinity;
  const s = [...scores].sort((a, b) => a - b);
  return s[0] > 0 ? s[s.length - 1] / s[0] : Infinity;
}

/**
 * Islands are "ambiguous" when the best atlas is not clearly smoother than the
 * runner-up: on GFL2 atlases the correct region is often the detailed one
 * (zipper teeth, logos), so these need a visual check / an override.
 */
export const AMBIGUOUS_RATIO = 1.5;

/**
 * @returns { assignment: Map(faceIndex → atlas file | { color } | null), overrideOf: Map(faceIndex → override), stats }
 * overrides: [{ box:[x0,y0,z0,x1,y1,z1], uvBox, minUvSpan, atlas, role?, doubleSided?, name? }] applied per island.
 */
export async function assignAtlases({ obj, faces, atlases, texPath, overrides = [], seed = 416, report = null, defaultAtlas = null }) {
  // One atlas: nothing to score, only overrides can change an island.
  const grads = atlases.length > 1 ? await Promise.all(atlases.map((a) => gradientImage(texPath(a)))) : [];
  const rand = rng(seed);
  const result = new Map();
  const overrideOf = new Map();
  const stats = {};
  for (const island of uvIslands(obj, faces)) {
    const scores = new Array(atlases.length).fill(0);
    const seen = new Set();
    let samples = 0;
    const centroid = [0, 0, 0];
    for (const f of island) {
      for (let k = 0; k < 3; k++) for (let c = 0; c < 3; c++) centroid[c] += obj.positions[obj.faceV[f * 3 + k] * 3 + c] / (island.length * 3);
      const key = [obj.faceT[f * 3], obj.faceT[f * 3 + 1], obj.faceT[f * 3 + 2]].sort((a, b) => a - b).join(',');
      if (seen.has(key)) continue; // double-sided duplicates add nothing
      seen.add(key);
      for (let s = 0; s < 2; s++) {
        let w0 = rand(); let w1 = rand();
        if (w0 + w1 > 1) { w0 = 1 - w0; w1 = 1 - w1; }
        const w2 = 1 - w0 - w1;
        const t = [obj.faceT[f * 3], obj.faceT[f * 3 + 1], obj.faceT[f * 3 + 2]];
        const u = w0 * obj.uvs[t[0] * 2] + w1 * obj.uvs[t[1] * 2] + w2 * obj.uvs[t[2] * 2];
        const v = w0 * obj.uvs[t[0] * 2 + 1] + w1 * obj.uvs[t[1] * 2 + 1] + w2 * obj.uvs[t[2] * 2 + 1];
        const x = Math.min(N - 1, Math.max(0, Math.floor((((u % 1) + 1) % 1) * (N - 1))));
        const y = Math.min(N - 1, Math.max(0, Math.floor((1 - (((v % 1) + 1) % 1)) * (N - 1))));
        grads.forEach((g, i) => { scores[i] += g[y * N + x]; });
        samples++;
      }
    }
    let best = 0;
    for (let i = 1; i < atlases.length; i++) if (scores[i] < scores[best]) best = i;
    const uvb = islandUvBox(obj, island);
    const ov = matchOverride(overrides, { centroid, uvBox: uvb });
    const atlas = ov ? ov.atlas : (defaultAtlas || atlases[best]);
    const mean = scores.map((x) => +(x / Math.max(1, samples)).toFixed(1));
    const ratio = scoreRatio(mean);
    if (report) {
      report.push({
        id: report.length, faces: island.length, centroid: centroid.map((c) => +c.toFixed(3)), scores: mean, atlas, override: ov ? (ov.why || true) : false,
        uv: uvb, ratio: Number.isFinite(ratio) ? +ratio.toFixed(2) : null, ambiguous: atlases.length > 1 && ratio < AMBIGUOUS_RATIO,
      });
    }
    const statKey = atlas === null ? 'dropped' : (typeof atlas === 'object' ? `color${atlas.color}` : atlas);
    stats[statKey] = (stats[statKey] || 0) + island.length;
    for (const f of island) { result.set(f, atlas); if (ov) overrideOf.set(f, ov); }
  }
  return { assignment: result, overrideOf, stats };
}

function islandCentroid(obj, island) {
  const c = [0, 0, 0];
  for (const f of island) for (let k = 0; k < 3; k++) for (let i = 0; i < 3; i++) c[i] += obj.positions[obj.faceV[f * 3 + k] * 3 + i] / (island.length * 3);
  return c;
}

/**
 * Merging another OBJ group into this one (e.g. the Astral Luminous jacket's
 * own bodysuit pieces into Cloth_1): the groups share most islands as near-
 * copies (same UV bounds, same face count, offset by < `posTol`), which would
 * z-fight. Keeps only the extra group's islands without such a twin.
 * @returns { kept: face indices of `extraFaces` to add, twins: dropped island count, added: island count }
 */
export function dropTwinIslands({ obj, baseFaces, extraFaces, uvTol = 0.004, posTol = 0.003 }) {
  const base = uvIslands(obj, baseFaces).map((isl) => ({ n: isl.length, uv: islandUvBox(obj, isl), c: islandCentroid(obj, isl) }));
  const kept = [];
  let twins = 0; let added = 0;
  for (const isl of uvIslands(obj, extraFaces)) {
    const uv = islandUvBox(obj, isl); const c = islandCentroid(obj, isl);
    const twin = base.some((b) => b.n === isl.length && Math.hypot(b.c[0] - c[0], b.c[1] - c[1], b.c[2] - c[2]) <= posTol
      && b.uv.every((x, i) => Math.abs(x - uv[i]) <= uvTol));
    if (twin) twins++; else { added++; kept.push(...isl); }
  }
  return { kept: kept.sort((a, b) => a - b), twins, added };
}
