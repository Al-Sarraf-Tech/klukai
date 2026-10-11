// Wave-2 build helpers: hair chains, group merging, feature switches (no assets).
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  islandStats, matchesSeed, inBox3, fitPolyline, projectOnPolyline, arcLengths, chainBoneWeights, hairVertexWeights,
} from '../lib/hair-chains.mjs';
import { dropTwinIslands } from '../lib/atlas-assign.mjs';
import {
  CONFIG, FEATURES, featureOn, HAIR_CHAINS, OPEN_JACKET, HAIR_COPIES, validateConfig,
} from '../avatar.config.mjs';

const close = (a, b, eps = 1e-6, msg = '') => assert.ok(Math.abs(a - b) <= eps, `${msg} ${a} ≉ ${b}`);

// ── chain fitting ───────────────────────────────────────────────────────────

test('islandStats: top, bottom, centroid and bounds of an island', () => {
  const positions = Float32Array.from([0, 1, 0, 1, 0, 0, 0, 0, 1, 0, 2, 0]);
  const faceV = Int32Array.from([0, 1, 2, 0, 2, 3]);
  const st = islandStats(positions, faceV, [0, 1]);
  assert.deepEqual(st.top, [0, 2, 0]); assert.equal(st.bottom[1], 0);
  assert.deepEqual(st.lo, [0, 0, 0]); assert.deepEqual(st.hi, [1, 2, 1]);
  assert.equal(st.faces, 2);
});

test('matchesSeed: root position rules and side', () => {
  const st = { top: [0.02, 1.49, -0.15], bottom: [0, 0.85, -0.25], centroid: [0.03, 1.1, -0.2] };
  assert.ok(matchesSeed(st, { topMinY: 1.44, topMaxZ: -0.12 }));
  assert.ok(!matchesSeed(st, { topMinZ: 0 }));
  assert.ok(!matchesSeed(st, { topMinY: 1.44, topMaxZ: -0.12, side: -1 }), 'centroid x > 0 is the left side');
  assert.ok(matchesSeed(st, { side: 1, bottomMaxY: 1.3 }));
  assert.ok(!matchesSeed(st, { bottomMaxY: 0.8 }));
  assert.ok(inBox3([0, 0, 0], [-1, -1, -1, 1, 1, 1])); assert.ok(!inBox3([2, 0, 0], [-1, -1, -1, 1, 1, 1]));
});

test('fitPolyline follows the centroid of each height band, root → tip', () => {
  // a slanted strand: x grows as y falls
  const pts = [];
  for (let y = 0; y <= 1.0001; y += 0.01) for (const dz of [-0.01, 0.01]) pts.push([0.2 * (1 - y), y, -0.1 + dz]);
  const poly = fitPolyline(pts, { bones: 4, yTop: 1, yBottom: 0 });
  assert.equal(poly.length, 5);
  // interior bands are symmetric (exact); the end bands are one-sided (biased by half a band)
  poly.forEach((p, k) => { close(p[1], 1 - k * 0.25, 1e-9); close(p[0], 0.2 * k * 0.25, k === 0 || k === 4 ? 0.015 : 0.002, `x at ${k}`); close(p[2], -0.1, 1e-6); });
  // an empty band borrows its neighbour's x/z
  const gappy = fitPolyline([[0, 1, 0], [0.1, 0, 0]], { bones: 4, yTop: 1, yBottom: 0, slice: 0.05 });
  assert.equal(gappy.length, 5); assert.ok(gappy.every((p) => Number.isFinite(p[0])));
});

test('projectOnPolyline / arcLengths: arc length, segment, negative above the root', () => {
  const poly = [[0, 1, 0], [0, 0.5, 0], [0, 0, 0]];
  assert.deepEqual(arcLengths(poly), [0, 0.5, 1]);
  const a = projectOnPolyline(poly, [0.1, 0.75, 0]);
  close(a.s, 0.25); assert.equal(a.seg, 0); close(a.dist, 0.1);
  const b = projectOnPolyline(poly, [0, 0.2, 0.05]);
  close(b.s, 0.8); assert.equal(b.seg, 1);
  assert.ok(projectOnPolyline(poly, [0, 1.2, 0]).s < 0, 'above the root');
});

test('chainBoneWeights: one bone in each segment middle, 50/50 at joints, sums to 1, clamped', () => {
  const arc = [0, 0.1, 0.2, 0.3];
  assert.deepEqual(chainBoneWeights(arc, 0.05), [{ bone: 0, w: 1 }]);
  const j = chainBoneWeights(arc, 0.1); assert.equal(j.length, 2); close(j[0].w, 0.5); close(j[1].w, 0.5);
  assert.deepEqual(chainBoneWeights(arc, -1), [{ bone: 0, w: 1 }]);
  assert.deepEqual(chainBoneWeights(arc, 9), [{ bone: 2, w: 1 }]);
  for (let s = 0; s <= 0.3; s += 0.013) close(chainBoneWeights(arc, s).reduce((n, x) => n + x.w, 0), 1, 1e-9);
  const mid = chainBoneWeights(arc, 0.18); assert.deepEqual(mid.map((x) => x.bone), [1, 2]); assert.ok(mid[0].w > mid[1].w);
});

test('hairVertexWeights: head at the scalp and root, chain further down, always sums to 1', () => {
  const poly = [[0, 1.48, -0.15], [0, 1.3, -0.2], [0, 1.1, -0.25], [0, 0.9, -0.28]];
  const arc = arcLengths(poly);
  const head = { center: [0, 1.4, -0.015], radius: [0.1, 0.15] };
  const near = hairVertexWeights({ poly, arc, p: [0, 1.42, -0.08], head, rootBlend: 0.06 });
  assert.equal(near.head, 1); assert.deepEqual(near.bones, []);
  const root = hairVertexWeights({ poly, arc, p: [0, 1.48, -0.16], head, rootBlend: 0.06 });
  assert.ok(root.head > 0.9, 'the tie stays on the head');
  const low = hairVertexWeights({ poly, arc, p: [0.02, 0.95, -0.27], head, rootBlend: 0.06 });
  assert.equal(low.head, 0); assert.equal(low.bones.at(-1).bone, 2);
  for (const w of [near, root, low]) close(w.head + w.bones.reduce((n, x) => n + x.w, 0), 1, 1e-9);
  assert.ok(low.bones.length <= 2, 'at most two chain bones (+ head) → ≤ 3 influences');
});

// ── merging groups / dropping copies ───────────────────────────────────────

/** Two quads (2 triangles each) as an OBJ-like record; `shift` moves the second copy. */
function objWithIslands(specs) {
  const positions = []; const uvs = []; const faceV = []; const faceT = [];
  specs.forEach(([x, y, z, u]) => {
    const v = positions.length / 3; const t = uvs.length / 2;
    positions.push(x, y, z, x + 0.1, y, z, x + 0.1, y + 0.1, z, x, y + 0.1, z);
    uvs.push(u, 0, u + 0.1, 0, u + 0.1, 0.1, u, 0.1);
    faceV.push(v, v + 1, v + 2, v, v + 2, v + 3); faceT.push(t, t + 1, t + 2, t, t + 2, t + 3);
  });
  return { positions: Float32Array.from(positions), uvs: Float32Array.from(uvs), faceV: Int32Array.from(faceV), faceT: Int32Array.from(faceT) };
}

test('dropTwinIslands keeps only islands without a near-copy (same UVs, same faces, within posTol)', () => {
  // base: island A at x=0 (faces 0,1); extra: A shifted 0.6 mm (faces 2,3) + unique B (faces 4,5) + A' moved 5 cm (faces 6,7)
  const obj = objWithIslands([[0, 0, 0, 0], [0.0006, 0, 0, 0], [1, 1, 1, 0.5], [0.05, 0, 0, 0]]);
  const r = dropTwinIslands({ obj, baseFaces: [0, 1], extraFaces: [2, 3, 4, 5, 6, 7] });
  assert.deepEqual(r.kept, [4, 5, 6, 7]);
  assert.equal(r.twins, 1); assert.equal(r.added, 2);
  assert.deepEqual(dropTwinIslands({ obj, baseFaces: [0, 1], extraFaces: [2, 3], posTol: 1e-5 }).kept, [2, 3], 'tolerance respected');
});

// ── features (wave 2 stays off unless asked) ───────────────────────────────

test('wave-2 features are off by default, on per build or per config', () => {
  assert.deepEqual([...FEATURES].sort(), ['hairChains', 'hairCopies', 'openJacket']);
  for (const f of FEATURES) {
    assert.equal(featureOn(CONFIG, f), false, `${f} off by default: a plain build is the wave-1 model`);
    assert.equal(featureOn(CONFIG, f, [f]), true);
    assert.equal(featureOn({ ...CONFIG, [f]: { ...CONFIG[f], enabled: true } }, f), true);
  }
  assert.equal(featureOn({ ...CONFIG, openJacket: null }, 'openJacket', ['openJacket']), false);
});

test('wave-2 config: chains per contract, open jacket overrides, validation', () => {
  assert.deepEqual(validateConfig(CONFIG), []);
  assert.deepEqual(HAIR_CHAINS.chains.map((c) => c.name), ['ponytail', 'back', 'sideL', 'sideR']);
  assert.equal(HAIR_CHAINS.parent, 'mixamorigHead');
  assert.ok(HAIR_CHAINS.colliders.some((c) => c.name === 'head') && HAIR_CHAINS.colliders.some((c) => c.to));
  assert.equal(OPEN_JACKET.into, 'Klukai_Cloth_1');
  assert.ok(OPEN_JACKET.atlasOverrides.some((o) => /bust/.test(o.why) && o.atlas === CONFIG.materials.Klukai_Cloth_1.atlases[0]));
  assert.ok(OPEN_JACKET.atlasOverrides.filter((o) => o.atlas === null).length >= 2, 'the poking-through bodysuit hips are dropped');
  assert.equal(HAIR_COPIES.twinsOf, 'Klukai_Hair');
  const bad = (patch) => validateConfig({ ...CONFIG, ...patch });
  assert.ok(bad({ hairChains: { ...HAIR_CHAINS, chains: [{ name: 'a', bones: 1, seed: {} }] } }).some((e) => e.includes('bones')));
  assert.ok(bad({ hairChains: { ...HAIR_CHAINS, chains: [...HAIR_CHAINS.chains, HAIR_CHAINS.chains[0]] } }).some((e) => e.includes('unique')));
  assert.ok(bad({ hairChains: { ...HAIR_CHAINS, head: { center: [0, 1, 0], radius: [0.2, 0.1] } } }).some((e) => e.includes('head')));
  assert.ok(bad({ hairChains: { ...HAIR_CHAINS, colliders: [{ name: 'x', bone: 'b', radius: 'auto' }] } }).some((e) => e.includes('radius')));
  assert.ok(bad({ hairChains: { ...HAIR_CHAINS, groups: ['Outline'] } }).some((e) => e.includes('kept material')));
  assert.ok(bad({ openJacket: { ...OPEN_JACKET, into: 'Outline' } }).some((e) => e.includes('openJacket.into')));
  assert.ok(bad({ openJacket: { ...OPEN_JACKET, from: ['Nope'] } }).some((e) => e.includes('openJacket.from')));
  assert.ok(bad({ openJacket: { ...OPEN_JACKET, atlasOverrides: [{ atlas: 'x.png' }] } }).some((e) => e.includes('not one of')));
  assert.ok(bad({ hairCopies: { ...HAIR_COPIES, group: 'Outline' } }).some((e) => e.includes('hairCopies')));
});
