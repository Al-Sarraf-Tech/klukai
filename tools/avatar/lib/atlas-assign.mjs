// The source model merged several GFL2 sub-meshes into one material group even
// though they sample different texture atlases (e.g. the jacket lives on
// cloth1, the stockings/gear on cloth2). This assigns each UV island to the
// atlas whose pixels are smoothest inside the island: on the right atlas an
// island covers one painted region; on a wrong one it straddles unrelated
// islands and outlines, so the mean interior gradient is much higher.
import sharp from 'sharp';

const N = 1024;

/** Gradient-magnitude image (max of |dx|,|dy| summed over RGB) at N×N. */
export async function gradientImage(file) {
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
 * @returns Map(faceIndex → atlas file) for the given faces.
 * overrides: [{ box:[x0,y0,z0,x1,y1,z1], atlas }] applied by island centroid.
 */
export async function assignAtlases({ obj, faces, atlases, texPath, overrides = [], seed = 416, report = null, defaultAtlas = null }) {
  const grads = await Promise.all(atlases.map((a) => gradientImage(texPath(a))));
  const rand = rng(seed);
  const result = new Map();
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
    const ov = overrides.find((o) => (o.box ? inBox(centroid, o.box) : true) && (o.uvBox ? uvInside(uvb, o.uvBox) : true)
      && (o.minUvSpan ? uvb[2] - uvb[0] >= o.minUvSpan : true));
    const atlas = ov ? ov.atlas : (defaultAtlas || atlases[best]);
    if (report) report.push({ faces: island.length, centroid: centroid.map((c) => +c.toFixed(3)), scores: scores.map((x) => +(x / samples).toFixed(1)), atlas, override: !!ov, uv: islandUvBox(obj, island) });
    stats[atlas ?? 'dropped'] = (stats[atlas ?? 'dropped'] || 0) + island.length;
    for (const f of island) result.set(f, atlas);
  }
  return { assignment: result, stats };
}
