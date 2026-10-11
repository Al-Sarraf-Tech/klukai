// Unit tests for the build's pure helpers (no source assets, no native modules).
//   node --test tools/avatar/test/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as THREE from 'three';

import { smoothstep, reweightToChild, headShareFn, childBindInverse, jointWeightTotals } from '../lib/head-skin.mjs';
import { smoothOutlineNormals, outlineWidths, jointShare, packOutline, ellipsoidDistance, positionKey } from '../lib/outline.mjs';
import { repackOrm, RMO_LAYOUT, textureGpuBytes } from '../lib/orm.mjs';
import { matchOverride, partSpec, scoreRatio, AMBIGUOUS_RATIO } from '../lib/atlas-assign.mjs';
import { flipTriangle } from '../lib/flip.mjs';
import {
  CONFIG, MATERIALS, HEAD_SKIN, ORM, OUTLINE, OUTFIT_ATLASES, validateConfig, validateOverrides,
} from '../avatar.config.mjs';

const close = (a, b, eps = 1e-6, msg) => assert.ok(Math.abs(a - b) <= eps, `${msg || ''} ${a} ≉ ${b}`);

// ── head skinning ───────────────────────────────────────────────────────────

test('smoothstep is 0/1 outside the band, monotone and symmetric inside', () => {
  assert.equal(smoothstep(1, 2, 0.5), 0);
  assert.equal(smoothstep(1, 2, 2.5), 1);
  close(smoothstep(1, 2, 1.5), 0.5);
  assert.ok(smoothstep(1, 2, 1.2) < smoothstep(1, 2, 1.3));
  assert.equal(smoothstep(1, 1, 0.9), 0);
  assert.equal(smoothstep(1, 1, 1.1), 1);
});

test('reweightToChild moves the Neck share to Head over the y band and renormalises', () => {
  const N = 1; const H = 9; const S = 2;
  // v0 below the band, v1 mid-band, v2 above, v3 no neck weight, v4 already 4 influences
  const positions = Float32Array.from([0, 1.20, 0, 0, 1.29, 0, 0, 1.40, 0, 0, 1.40, 0, 0, 1.29, 0]);
  const skinIndex = Uint16Array.from([N, S, 0, 0, N, S, 0, 0, N, 0, 0, 0, S, 0, 0, 0, N, S, 3, 4]);
  const skinWeight = Float32Array.from([0.5, 0.5, 0, 0, 0.6, 0.4, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0.4, 0.3, 0.2, 0.1]);
  const r = reweightToChild({ skinIndex, skinWeight, positions, from: N, to: H, blend: [1.27, 1.31] });
  const w = (v, j) => { let s = 0; for (let k = 0; k < 4; k++) if (skinIndex[v * 4 + k] === j) s += skinWeight[v * 4 + k]; return s; };
  close(w(0, N), 0.5); assert.equal(w(0, H), 0, 'below the band nothing moves');
  close(w(1, N) + w(1, H), 0.6, 1e-6, 'mid-band: neck + head keep the neck total');
  close(w(1, H), 0.6 * smoothstep(1.27, 1.31, 1.29), 1e-6);
  close(w(1, S), 0.4);
  close(w(2, H), 1); assert.equal(w(2, N), 0, 'above the band all neck weight is on the head');
  close(w(3, S), 1); assert.equal(w(3, H), 0, 'vertices without neck weight are untouched');
  // v4 had 4 influences: a 5th (Head) forces a drop; Head is kept, the smallest other goes
  assert.ok(w(4, H) > 0, 'the new Head influence is never the one dropped');
  assert.equal(w(4, 4), 0, 'smallest other influence dropped');
  for (let v = 0; v < 5; v++) { let s = 0; for (let k = 0; k < 4; k++) s += skinWeight[v * 4 + k]; close(s, 1, 1e-5, `v${v} sums to 1`); }
  assert.equal(r.touched, 3); assert.equal(r.full, 1); assert.equal(r.dropped, 1);
});

test('headShareFn: jaw band in front, long rope band for hair behind the ear plane', () => {
  const f = headShareFn(HEAD_SKIN);
  const front = f(0, 1.29, 0.08, true);
  assert.equal(front.other, 0, 'in front of the ear plane only Neck weight moves');
  close(front.from, smoothstep(1.27, 1.31, 1.29));
  const tail = f(0, 1.2, -0.2, true);
  assert.ok(tail.other > 0.2 && tail.other < 0.8, `ponytail at the nape moves part of every influence (${tail.other})`);
  assert.equal(f(0, 0.9, -0.2, true).other, 0, 'tail tips stay on the spine/legs');
  assert.equal(f(0, 1.5, -0.2, true).other, 1, 'ponytail root is fully on the head');
  assert.equal(f(0, 1.2, -0.2, false).other, 0, 'cloth/skin behind the head (hood) never gets the rope rule');
  const lower = f(0, 1.1, -0.2, true).other; const upper = f(0, 1.3, -0.2, true).other;
  assert.ok(lower < upper, 'share grows toward the head');
});

test('reweightToChild with a rope share only takes from the allowed joints', () => {
  const positions = Float32Array.from([0, 1.2, -0.2]);
  const skinIndex = Uint16Array.from([1, 2, 3, 0]); // Spine2, RightArm, Neck
  const skinWeight = Float32Array.from([0.5, 0.3, 0.2, 0]);
  const otherJoints = [false, true, false, false]; // only joint 1 (spine) may give
  reweightToChild({ skinIndex, skinWeight, positions, from: 3, to: 9, share: () => ({ from: 1, other: 0.5 }), otherJoints });
  const w = (j) => { let s = 0; for (let k = 0; k < 4; k++) if (skinIndex[k] === j) s += skinWeight[k]; return s; };
  close(w(2), 0.3, 1e-6, 'arm weight untouched (it drapes the hair over the shoulders)');
  close(w(1), 0.25); close(w(9), 0.2 + 0.25); assert.equal(w(3), 0);
});

test('childBindInverse equals the inverse rest world when rest = bind, and follows the parent otherwise', () => {
  const parentWorld = new THREE.Matrix4().makeTranslation(0, 1.25, -0.03);
  const childWorld = new THREE.Matrix4().makeTranslation(0, 1.32, 0).multiply(new THREE.Matrix4().makeRotationX(0.1));
  const inv = childBindInverse({ parentInverse: parentWorld.clone().invert(), parentWorld, childWorld });
  const id = inv.clone().multiply(childWorld);
  id.elements.forEach((v, i) => close(v, [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1][i], 1e-9));
  // parent bound 1 cm lower than its rest: the child's bind moves with it
  const parentBind = new THREE.Matrix4().makeTranslation(0, 1.24, -0.03);
  const inv2 = childBindInverse({ parentInverse: parentBind.clone().invert(), parentWorld, childWorld });
  const childBind = inv2.clone().invert();
  close(new THREE.Vector3().setFromMatrixPosition(childBind).y, 1.31, 1e-9);
});

test('jointWeightTotals sums per joint', () => {
  const t = jointWeightTotals(Uint16Array.from([0, 1, 1, 2]), Float32Array.from([0.5, 0.5, 0.25, 0.75]), 3);
  assert.deepEqual([...t], [0.5, 0.75, 0.75]);
});

// ── outline attribute ───────────────────────────────────────────────────────

/** Unit cube as 6 hard-split faces (24 verts, own normals per face), 2 triangles each. */
function hardCube() {
  const P = []; const N = []; const I = [];
  const faces = [[[1, 0, 0], [0, 1, 0], [0, 0, 1]], [[-1, 0, 0], [0, 0, 1], [0, 1, 0]], [[0, 1, 0], [0, 0, 1], [1, 0, 0]],
    [[0, -1, 0], [1, 0, 0], [0, 0, 1]], [[0, 0, 1], [1, 0, 0], [0, 1, 0]], [[0, 0, -1], [0, 1, 0], [1, 0, 0]]];
  for (const [n, u, v] of faces) {
    const base = P.length / 3;
    for (const [a, b] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) {
      P.push(...n.map((x, i) => x + a * u[i] + b * v[i])); N.push(...n);
    }
    I.push(base, base + 1, base + 2, base, base + 2, base + 3);
  }
  return { positions: Float32Array.from(P), normals: Float32Array.from(N), indices: Uint16Array.from(I) };
}

test('smoothed outline normals close hard splits (cube corners point diagonally)', () => {
  const cube = hardCube();
  const [n] = smoothOutlineNormals([cube]);
  for (let i = 0; i < n.length / 3; i++) {
    const p = [cube.positions[i * 3], cube.positions[i * 3 + 1], cube.positions[i * 3 + 2]];
    const d = p.map((x) => x / Math.hypot(...p));
    for (let k = 0; k < 3; k++) close(n[i * 3 + k], d[k], 1e-5, `vertex ${i}`);
  }
});

test('smoothing is shared across primitives (seam between two meshes agrees)', () => {
  const cube = hardCube();
  // split the cube's faces into two primitives
  const a = { ...cube, indices: cube.indices.slice(0, 18) };
  const b = { ...cube, indices: cube.indices.slice(18) };
  const [na, nb] = smoothOutlineNormals([a, b]);
  for (let i = 0; i < na.length; i++) close(na[i], nb[i], 1e-6);
});

test('cancelling layers (two-sided card) keep their own normal', () => {
  const positions = Float32Array.from([0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0]);
  const normals = Float32Array.from([0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0, -1, 0, 0, -1, 0, 0, -1]);
  const front = { positions: positions.slice(0, 9), normals: normals.slice(0, 9), indices: Uint16Array.from([0, 1, 2]) };
  const back = { positions: positions.slice(9), normals: normals.slice(9), indices: Uint16Array.from([0, 2, 1]) };
  const [nf, nb] = smoothOutlineNormals([front, back]);
  close(nf[2], 1); close(nb[2], -1);
});

test('face normals are oriented like the vertex normals (flipped winding is harmless)', () => {
  const positions = Float32Array.from([0, 0, 0, 1, 0, 0, 0, 1, 0]);
  const normals = Float32Array.from([0, 0, 1, 0, 0, 1, 0, 0, 1]);
  const [n] = smoothOutlineNormals([{ positions, normals, indices: Uint16Array.from([0, 2, 1]) }]);
  close(n[2], 1);
});

test('outline widths: zero prims, face masks (feathered), hands', () => {
  const positions = Float32Array.from([0, 0, 0, 0.5, 0, 0, 2, 0, 0]);
  assert.deepEqual([...outlineWidths({ positions, zero: true })], [0, 0, 0]);
  const masks = [{ center: [0, 0, 0], radius: [1, 1, 1], feather: 0.5 }];
  const w = outlineWidths({ positions, masked: true, masks });
  assert.equal(w[0], 0); assert.equal(w[1], 0); assert.equal(w[2], 1);
  const ramp = outlineWidths({ positions: Float32Array.from([1.25, 0, 0]), masked: true, masks });
  assert.ok(ramp[0] > 0 && ramp[0] < 1, 'feathered edge');
  assert.deepEqual([...outlineWidths({ positions, masks })], [1, 1, 1], 'masks only apply when masked');
  const hands = outlineWidths({ positions, handShare: Float32Array.from([1, 0.5, 0]), handWidth: 0.6 });
  close(hands[0], 0.6); close(hands[1], 0.8); assert.equal(hands[2], 1);
  close(ellipsoidDistance(0, 2, 0, { center: [0, 0, 0], radius: [1, 2, 1] }), 1);
});

test('jointShare sums the selected joints', () => {
  const s = jointShare(Uint8Array.from([0, 1, 2, 3, 2, 2, 0, 0]), Float32Array.from([0.1, 0.2, 0.3, 0.4, 0.5, 0.5, 0, 0]), [false, false, true, true]);
  close(s[0], 0.7); close(s[1], 1);
});

test('packOutline: normalized int8, w in 0..127', () => {
  const p = packOutline(Float32Array.from([1, 0, -1, 0.5, 0.5, 0.7071]), Float32Array.from([1, 0]));
  assert.ok(p instanceof Int8Array);
  assert.deepEqual([...p.slice(0, 4)], [127, 0, -127, 127]);
  assert.equal(p[7], 0);
  assert.equal(positionKey(0.000004, 0, 0, 1e-5), positionKey(0, 0, 0, 1e-5));
});

// ── ORM ─────────────────────────────────────────────────────────────────────

test('repackOrm: GFL2 _rmo (R rough, G metal, B occ) → glTF ORM (R occ, G rough, B metal)', () => {
  const rmo = Uint8Array.from([10, 20, 30, 255, 40, 50, 60, 255]);
  assert.deepEqual([...repackOrm(rmo, 4)], [30, 10, 20, 60, 40, 50]);
  assert.deepEqual([...repackOrm(Uint8Array.from([1, 2, 3]), 3, RMO_LAYOUT)], [3, 1, 2]);
  assert.deepEqual(RMO_LAYOUT, ORM.from, 'config layout = library layout');
  assert.throws(() => repackOrm(rmo, 2));
  assert.throws(() => repackOrm(rmo, 3, { occlusion: 5, roughness: 0, metallic: 1 }));
});

test('texture GPU estimate: RGBA8 with mips ≈ 4/3 of the base level', () => {
  close(textureGpuBytes(1024, 1024) / (1024 * 1024 * 4), 4 / 3, 1e-3);
  assert.equal(textureGpuBytes(1, 1), 4);
});

// ── atlas overrides ─────────────────────────────────────────────────────────

test('matchOverride: box, uvBox, minUvSpan, first match wins', () => {
  const ovs = [
    { box: [0, 0, 0, 1, 1, 1], uvBox: [0, 0, 0.5, 0.5], atlas: 'a.png' },
    { box: [0, 0, 0, 1, 1, 1], atlas: 'b.png' },
    { uvBox: [0, 0, 1, 1], minUvSpan: 0.9, atlas: 'c.png' },
  ];
  assert.equal(matchOverride(ovs, { centroid: [0.5, 0.5, 0.5], uvBox: [0.1, 0.1, 0.4, 0.4] }).atlas, 'a.png');
  assert.equal(matchOverride(ovs, { centroid: [0.5, 0.5, 0.5], uvBox: [0.1, 0.1, 0.6, 0.4] }).atlas, 'b.png');
  assert.equal(matchOverride(ovs, { centroid: [2, 2, 2], uvBox: [0, 0, 0.95, 0.5] }).atlas, 'c.png');
  assert.equal(matchOverride(ovs, { centroid: [2, 2, 2], uvBox: [0, 0, 0.5, 0.5] }), null);
});

test('partSpec: overrides carry role/sidedness/flat colour; key = (image|colour, role, sidedness)', () => {
  const hair = MATERIALS.Klukai_Hair;
  const plain = partSpec(hair, hair.texture);
  assert.deepEqual([plain.texture, plain.color, plain.role, plain.doubleSided], ['c_Clukay_hair_d.png', null, 'hair', true]);
  const suit = hair.atlasOverrides[0];
  const s = partSpec(hair, suit.atlas, suit);
  assert.equal(s.texture, null); assert.equal(s.color, '#1b1b20');
  assert.equal(s.role, 'cloth'); assert.equal(s.doubleSided, false); assert.equal(s.label, 'bodysuit');
  assert.notEqual(s.key, plain.key);
  assert.equal(partSpec(hair, null), null, 'null atlas drops the island');
  const collar = partSpec(hair, hair.atlasOverrides[1].atlas, hair.atlasOverrides[1]);
  assert.equal(collar.key, s.key, 'shorts and collar share one material (one draw call)');
});

test('score ratio flags ambiguous islands', () => {
  assert.equal(scoreRatio([10, 12]), 1.2);
  assert.ok(scoreRatio([10, 12]) < AMBIGUOUS_RATIO);
  assert.ok(scoreRatio([4.8, 76.4]) > AMBIGUOUS_RATIO);
  assert.equal(scoreRatio([0, 5]), Infinity);
  assert.equal(scoreRatio([5]), Infinity);
});

test('flipTriangle swaps two corners in every attribute and negates normals', () => {
  const pos = { array: Float32Array.from([0, 0, 0, 1, 0, 0, 0, 1, 0]), itemSize: 3 };
  const uv = { array: Float32Array.from([0, 0, 1, 0, 0, 1]), itemSize: 2 };
  const nrm = { array: Float32Array.from([0, 0, -1, 0, 0, -1, 0, 0, -1]), itemSize: 3 };
  flipTriangle([pos, uv, nrm], nrm, 0);
  assert.deepEqual([...pos.array], [0, 0, 0, 0, 1, 0, 1, 0, 0]);
  assert.deepEqual([...uv.array], [0, 0, 0, 1, 1, 0]);
  assert.deepEqual([...nrm.array], [-0, -0, 1, -0, -0, 1, -0, -0, 1]);
});

// ── shipped config ──────────────────────────────────────────────────────────

test('shipped config: verified overrides, head skin, ORM, outline', () => {
  assert.deepEqual(validateConfig(CONFIG), []);
  const cloth = MATERIALS.Klukai_Cloth_1.atlasOverrides;
  const cap = cloth.find((o) => /cap crown/.test(o.why));
  assert.ok(cap.box && cap.box[1] >= 1.4, 'cap crown override is boxed above the gloves');
  for (const part of ['chest band outer', 'hem band', 'hood', 'zipper', 'torso straps', 'hip bag', 'cat pouch', 'buckle box', 'goggle', 'shoes']) {
    assert.ok(cloth.some((o) => o.why.includes(part)), `override for ${part}`);
  }
  for (const o of MATERIALS.Klukai_Hair.atlasOverrides) {
    assert.equal(o.role, 'cloth'); assert.equal(o.doubleSided, false); assert.equal(o.flip, true);
  }
  assert.equal(MATERIALS.Klukai_Hair.maxSize, 2048, 'hair ships at 2048');
  assert.equal(CONFIG.textureMaxSize, 1024, 'everything else stays at 1024');
  assert.equal(HEAD_SKIN.bone, 'mixamorigHead'); assert.equal(HEAD_SKIN.from, 'mixamorigNeck');
  assert.deepEqual(HEAD_SKIN.blend, [1.27, 1.31]);
  assert.deepEqual(Object.keys(ORM.maps).sort(), [...OUTFIT_ATLASES].sort());
  assert.ok(ORM.maxSize <= 512);
  assert.equal(OUTLINE.attribute, '_OUTLINE');
});

test('config validation catches bad overrides, ORM and head-skin settings', () => {
  const m = (o) => validateOverrides('X', { atlases: ['a.png', 'b.png'], role: 'cloth', atlasOverrides: [o] });
  assert.deepEqual(m({ box: [0, 0, 0, 1, 1, 1], atlas: 'a.png' }), []);
  assert.ok(m({ atlas: 'c.png' }).some((e) => e.includes('not one of')));
  assert.ok(m({ box: [1, 0, 0, 0, 1, 1], atlas: 'a.png' }).some((e) => e.includes('box')));
  assert.ok(m({ uvBox: [0, 0, 1], atlas: 'a.png' }).some((e) => e.includes('uvBox')));
  assert.ok(m({ box: [0, 0, 0, 1, 1, 1] }).some((e) => e.includes('no atlas')));
  assert.ok(m({ atlas: { color: 'black' }, name: 'x' }).some((e) => e.includes('colour')));
  assert.ok(m({ atlas: { color: '#000000' } }).some((e) => e.includes('needs a name')));
  assert.ok(m({ atlas: 'a.png', role: 'fur' }).some((e) => e.includes('unknown role')));
  assert.ok(m({ atlas: 'a.png', doubleSided: 'no' }).some((e) => e.includes('doubleSided')));
  assert.ok(m({ atlas: 'a.png', flip: 1 }).some((e) => e.includes('flip')));
  assert.ok(m({ atlas: null, flip: true }).some((e) => e.includes('dropped')));
  assert.ok(m({ atlas: 'a.png', name: 'Bad Name' }).some((e) => e.includes('slug')));
  assert.deepEqual(m({ atlas: null }), []);
  const bad = (patch) => validateConfig({ ...CONFIG, ...patch });
  assert.ok(bad({ headSkin: { ...HEAD_SKIN, blend: [1.3, 1.2] } }).some((e) => e.includes('headSkin blend')));
  assert.ok(bad({ headSkin: { ...HEAD_SKIN, from: HEAD_SKIN.bone } }).some((e) => e.includes('distinct')));
  assert.ok(bad({ orm: { ...ORM, maps: { 'nope.png': 'x_rmo.png' } } }).some((e) => e.includes('no kept material')));
  assert.ok(bad({ orm: { ...ORM, from: { occlusion: 0, roughness: 0, metallic: 1 } } }).some((e) => e.includes('distinct')));
  assert.ok(bad({ outline: { ...OUTLINE, attribute: 'outline' } }).some((e) => e.includes('_UPPERCASE')));
  assert.ok(bad({ outline: { ...OUTLINE, faceMask: [{ center: [0, 0], radius: [1, 1, 1] }] } }).some((e) => e.includes('face mask')));
  assert.ok(bad({ materials: { ...MATERIALS, Klukai_Hair: { ...MATERIALS.Klukai_Hair, maxSize: 4096 } } }).some((e) => e.includes('hard cap')));
});
