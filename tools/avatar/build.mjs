#!/usr/bin/env node
// Builds assets/build/klukai.glb from the Mixamo FBX clips + OBJ material groups
// + PNG textures. PERSONAL USE ONLY: the inputs are game-ripped and the output is
// a derived model. Never commit either; serve only through the authenticated API.
//
//   node build.mjs            → optimized GLB (webp ≤1024, meshopt, resampled clips)
//   node build.mjs --debug    → also keeps dropped material groups, no compression
//                               (assets/build/klukai.debug.glb) for inspection
//
// assets/build/ is bind-mounted into the running app: a plain `node build.mjs`
// replaces the LIVE model. Develop with AVATAR_OUT=…/assets/build/dev/klukai.glb
// (and AVATAR_CONFIG=path/to/variant.mjs, exporting CONFIG, for experiments).
import { THREE } from './lib/node-three.mjs';
import { readObjFaces } from './lib/obj-faces.mjs';
import { assignAtlases, partSpec, dropTwinIslands, uvIslands, matchOverride } from './lib/atlas-assign.mjs';
import { islandStats, matchesSeed, inBox3, fitPolyline, projectOnPolyline, arcLengths, hairVertexWeights } from './lib/hair-chains.mjs';
import { openEyesAndBuildBlink } from './lib/eyes.mjs';
import { reweightToChild, childBindInverse, jointWeightTotals, headShareFn } from './lib/head-skin.mjs';
import { smoothOutlineNormals, outlineWidths, jointShare, packOutline } from './lib/outline.mjs';
import { ormImage, textureGpuBytes } from './lib/orm.mjs';
import { flipTriangle } from './lib/flip.mjs';
import { createBodyCore } from './lib/body-core.mjs';
import { CONFIG as BASE_CONFIG, validateConfig, FEATURES, featureOn } from './avatar.config.mjs';
import { FBXLoader } from 'three/examples/jsm/loaders/FBXLoader.js';
import { GLTFExporter } from 'three/examples/jsm/exporters/GLTFExporter.js';
import { mergeVertices } from 'three/examples/jsm/utils/BufferGeometryUtils.js';
import { NodeIO } from '@gltf-transform/core';
import { ALL_EXTENSIONS, EXTTextureWebP } from '@gltf-transform/extensions';
import { dedup, prune, resample, meshopt, join, compactPrimitive } from '@gltf-transform/functions';
import { PropertyType } from '@gltf-transform/core';
import { MeshoptEncoder, MeshoptDecoder } from 'meshoptimizer';
import sharp from 'sharp';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../..');
const DEBUG = process.argv.includes('--debug');
const log = (...a) => console.log('[avatar]', ...a);

function fail(msg) { console.error(`[avatar] ERROR: ${msg}`); process.exit(1); }

const CONFIG = process.env.AVATAR_CONFIG ? (await import(pathToFileURL(path.resolve(process.env.AVATAR_CONFIG)).href)).CONFIG : BASE_CONFIG;
if (process.env.AVATAR_CONFIG) log(`config variant ${process.env.AVATAR_CONFIG}`);
const problems = validateConfig(CONFIG);
if (problems.length) fail(`invalid config:\n  ${problems.join('\n  ')}`);

// Wave-2 features are off unless enabled in the config or listed here.
const features = (process.env.AVATAR_FEATURES || '').split(',').map((x) => x.trim()).filter(Boolean);
for (const f of features) if (!FEATURES.includes(f)) fail(`unknown AVATAR_FEATURES entry ${f} (known: ${FEATURES.join(', ')})`);
const enabled = new Set(FEATURES.filter((f) => featureOn(CONFIG, f, features)));
if (enabled.has('hairChains') && !enabled.has('hairCopies')) enabled.add('hairCopies'); // the face group's hair copy cannot follow the chains
const on = (name) => enabled.has(name);
log(`features: ${FEATURES.map((f) => `${f}=${on(f) ? 'on' : 'off'}`).join(' ')}`);

const outFile = process.env.AVATAR_OUT ? path.resolve(process.env.AVATAR_OUT) : path.join(repo, DEBUG ? CONFIG.outFile.replace(/\.glb$/, '.debug.glb') : CONFIG.outFile);
const outDir = path.dirname(outFile);

const src = (f) => path.join(repo, CONFIG.sourceDir, f);
const tex = (f) => path.join(repo, CONFIG.textureDir, f);
for (const c of CONFIG.clips) if (!fs.existsSync(src(c.file))) fail(`missing source clip ${src(c.file)}`);
if (!fs.existsSync(src(CONFIG.objFile))) fail(`missing ${src(CONFIG.objFile)}`);

// FBXLoader warns once per vertex with >4 weights; it already keeps the 4 largest.
const warn = console.warn;
console.warn = (m, ...r) => { if (!String(m).includes('more than 4 skinning weights')) warn(m, ...r); };

function loadFbx(file) {
  const b = fs.readFileSync(src(file));
  return new FBXLoader().parse(b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength), '');
}

// ── 1. Base skinned mesh ─────────────────────────────────────────────────────
log(`base mesh from ${CONFIG.baseFbx}`);
const root = loadFbx(CONFIG.baseFbx);
let mesh = null;
root.traverse((o) => { if (o.isSkinnedMesh && !mesh) mesh = o; });
if (!mesh) fail('no skinned mesh in base FBX');
const geo = mesh.geometry;
if (geo.index) fail('expected non-indexed FBX geometry');

// ── 2. Recover material groups from the OBJ (same triangle order) ───────────
const obj = readObjFaces(src(CONFIG.objFile));
const triCount = geo.attributes.position.count / 3;
if (obj.faceMat.length !== triCount) fail(`OBJ faces ${obj.faceMat.length} != FBX triangles ${triCount}`);
{ // spot-check geometry identity on a sample of triangles
  const P = geo.attributes.position.array;
  for (let t = 0; t < triCount; t += 997) {
    const vi = obj.faceV[t * 3];
    const d = Math.hypot(P[t * 9] - obj.positions[vi * 3], P[t * 9 + 1] - obj.positions[vi * 3 + 1], P[t * 9 + 2] - obj.positions[vi * 3 + 2]);
    if (d > 1e-3) fail(`OBJ/FBX triangle order mismatch at tri ${t} (d=${d.toFixed(4)})`);
  }
}
for (const m of obj.materials) if (!(m in CONFIG.materials)) fail(`OBJ material not in config: ${m}`);

// Head joint: Mixamo gave the head bone no weights, so the face, cap and bangs
// rode on the Neck and every head rotation in the clips was invisible. Make it
// a skin joint (inverse bind consistent with the Neck's bind pose) and move the
// Neck's weight onto it above the jaw line.
if (CONFIG.headSkin) {
  const hs = CONFIG.headSkin;
  root.updateMatrixWorld(true);
  const sk = mesh.skeleton;
  const bone = root.getObjectByName(hs.bone);
  const fromIdx = sk.bones.findIndex((b) => b.name === hs.from);
  if (!bone || fromIdx < 0) fail(`headSkin: bones ${hs.bone} / ${hs.from} not found`);
  let toIdx = sk.bones.indexOf(bone);
  if (toIdx < 0) {
    const inv = childBindInverse({ parentInverse: sk.boneInverses[fromIdx], parentWorld: sk.bones[fromIdx].matrixWorld, childWorld: bone.matrixWorld });
    mesh.bind(new THREE.Skeleton([...sk.bones, bone], [...sk.boneInverses, inv]), mesh.bindMatrix);
    toIdx = mesh.skeleton.bones.length - 1;
  }
  const shareAt = headShareFn(hs);
  const hairGroups = new Set((hs.behind?.groups || []).map((g) => obj.materials.indexOf(g)));
  const r = reweightToChild({
    skinIndex: geo.attributes.skinIndex.array, skinWeight: geo.attributes.skinWeight.array, positions: geo.attributes.position.array,
    from: fromIdx, to: toIdx, share: (x, y, z, c) => shareAt(x, y, z, hairGroups.has(obj.faceMat[Math.floor(c / 3)])),
    otherJoints: hs.behind?.joints ? mesh.skeleton.bones.map((b) => new RegExp(hs.behind.joints).test(b.name)) : null,
  });
  const totals = jointWeightTotals(geo.attributes.skinIndex.array, geo.attributes.skinWeight.array, mesh.skeleton.bones.length);
  log(`head skin: ${hs.bone} is joint ${toIdx}; ${r.touched} corners reweighted from ${hs.from} (${r.full} fully, ${r.dropped} dropped a 5th influence); weight sums ${hs.from} ${totals[fromIdx].toFixed(0)} → ${hs.bone} ${totals[toIdx].toFixed(0)}`);
  if (!(totals[toIdx] > 0)) fail('headSkin moved no weight');
}

// Hair chains (wave 2): bones under the Head for the viewer's spring physics.
const springData = on('hairChains') ? buildHairChains() : null;

/**
 * Fits a polyline through each hair mass, adds `hair_<chain>_<i>` bones under
 * the parent joint (inverse binds consistent with its bind pose) and reweights
 * the chain hair to head + chain bones (arm/thigh/spine influences dropped).
 * @returns { springChains, springColliders } for the scene extras
 */
function buildHairChains() {
  const hc = CONFIG.hairChains;
  root.updateMatrixWorld(true);
  const sk = mesh.skeleton;
  const parentBone = root.getObjectByName(hc.parent);
  const parentIdx = sk.bones.indexOf(parentBone);
  if (parentIdx < 0) fail(`hairChains: ${hc.parent} is not a skin joint (headSkin must make it one)`);
  // Hair islands (OBJ bind metres); islands an override gives a non-hair role (the bodysuit) are skipped.
  const candidates = [];
  for (const g of hc.groups) {
    const cfg = CONFIG.materials[g];
    for (const island of uvIslands(obj, dedupFaces(g))) {
      const st = islandStats(obj.positions, obj.faceV, island);
      let u0 = 1e9; let v0 = 1e9; let u1 = -1e9; let v1 = -1e9;
      for (const f of island) for (let k = 0; k < 3; k++) { const t = obj.faceT[f * 3 + k]; u0 = Math.min(u0, obj.uvs[t * 2]); v0 = Math.min(v0, obj.uvs[t * 2 + 1]); u1 = Math.max(u1, obj.uvs[t * 2]); v1 = Math.max(v1, obj.uvs[t * 2 + 1]); }
      const ov = matchOverride(cfg.atlasOverrides || [], { centroid: st.centroid, uvBox: [u0, v0, u1, v1].map((x) => +x.toFixed(3)) });
      if (ov && (ov.role ?? cfg.role) !== cfg.role) continue;
      if (st.bottom[1] >= hc.hangingBelowY) continue; // scalp, bangs: stay on the head
      if ((hc.exclude || []).some((e) => inBox3(st.centroid, e.box))) continue;
      candidates.push({ island, st });
    }
  }
  const chains = hc.chains.map((c) => ({ ...c, seeds: [], members: [] }));
  for (const cand of candidates) {
    const ch = chains.find((c) => matchesSeed(cand.st, c.seed));
    if (ch) { ch.seeds.push(cand); cand.chain = ch; }
  }
  for (const ch of chains) {
    if (!ch.seeds.length) fail(`hairChains: chain ${ch.name} has no seed islands`);
    const pts = [];
    for (const { island } of ch.seeds) for (const f of island) for (let k = 0; k < 3; k++) { const v = obj.faceV[f * 3 + k]; pts.push([obj.positions[v * 3], obj.positions[v * 3 + 1], obj.positions[v * 3 + 2]]); }
    const tops = ch.seeds.map((x) => x.st.top[1]).sort((a, b) => a - b);
    const ys = pts.map((p) => p[1]).sort((a, b) => a - b);
    ch.poly = fitPolyline(pts, { bones: ch.bones, yTop: ch.yTop ?? tops[tops.length >> 1], yBottom: ys[Math.floor(ys.length * 0.03)] });
    ch.arc = arcLengths(ch.poly);
    ch.members.push(...ch.seeds);
  }
  let loose = 0;
  for (const cand of candidates) { // fragments: nearest chain
    if (cand.chain) continue;
    let best = null; let bd = Infinity;
    for (const ch of chains) { const d = projectOnPolyline(ch.poly, cand.st.centroid).dist; if (d < bd) { bd = d; best = ch; } }
    if (best && bd <= hc.assignMaxDist) { best.members.push(cand); cand.chain = best; } else loose++;
  }
  // Bones: world rotation identity in the bind pose, one per segment.
  const newBones = []; const newInverses = [];
  for (const ch of chains) {
    let parent = parentBone;
    ch.boneObjs = [];
    for (let k = 0; k < ch.bones; k++) {
      const b = new THREE.Bone();
      b.name = `hair_${ch.name}_${k}`;
      parent.add(b);
      parent.updateMatrixWorld(true);
      const world = new THREE.Matrix4().makeTranslation(ch.poly[k][0], ch.poly[k][1], ch.poly[k][2]);
      new THREE.Matrix4().copy(parent.matrixWorld).invert().multiply(world).decompose(b.position, b.quaternion, b.scale);
      b.updateMatrixWorld(true);
      newBones.push(b);
      newInverses.push(childBindInverse({ parentInverse: sk.boneInverses[parentIdx], parentWorld: parentBone.matrixWorld, childWorld: b.matrixWorld }));
      ch.boneObjs.push(b);
      parent = b;
    }
  }
  const base = sk.bones.length;
  mesh.bind(new THREE.Skeleton([...sk.bones, ...newBones], [...sk.boneInverses, ...newInverses]), mesh.bindMatrix);
  // Reweight every corner of the chain hair (FBX corners = OBJ face order).
  const P = geo.attributes.position.array; const SI = geo.attributes.skinIndex.array; const SW = geo.attributes.skinWeight.array;
  let offset = base; let corners = 0;
  for (const ch of chains) {
    const idx = (k) => offset + k;
    for (const { island } of ch.members) {
      for (const f of island) {
        for (let k = 0; k < 3; k++) {
          const c = f * 3 + k;
          const w = hairVertexWeights({ poly: ch.poly, arc: ch.arc, p: [P[c * 3], P[c * 3 + 1], P[c * 3 + 2]], head: hc.head, rootBlend: hc.rootBlend });
          const list = [{ j: parentIdx, w: w.head }, ...w.bones.map((x) => ({ j: idx(x.bone), w: x.w }))].filter((x) => x.w > 1e-6);
          const sum = list.reduce((a, x) => a + x.w, 0);
          for (let q = 0; q < 4; q++) { SI[c * 4 + q] = q < list.length ? list[q].j : 0; SW[c * 4 + q] = q < list.length ? list[q].w / sum : 0; }
          corners++;
        }
      }
    }
    offset += ch.bones;
    const len = ch.arc[ch.arc.length - 1];
    log(`hair chain ${ch.name}: ${ch.bones} bones, ${len.toFixed(2)} m, root (${ch.poly[0].map((x) => x.toFixed(3))}), ${ch.seeds.length} seed + ${ch.members.length - ch.seeds.length} assigned islands, ${ch.members.reduce((n, m) => n + m.island.length, 0)} faces`);
  }
  log(`hair chains: ${newBones.length} bones (joints ${base} → ${base + newBones.length}), ${corners} corners reweighted, ${loose} hanging islands left on their old weights`);
  if (DEBUG) {
    const r3 = (p) => p.map((x) => +x.toFixed(3));
    fs.writeFileSync(path.join(outDir, 'hair-chains.json'), JSON.stringify({
      chains: chains.map((ch) => ({ name: ch.name, poly: ch.poly.map(r3), members: ch.members.map((m) => ({ faces: m.island.length, seed: ch.seeds.includes(m), centroid: r3(m.st.centroid), top: r3(m.st.top), bottom: r3(m.st.bottom) })) })),
      loose: candidates.filter((c) => !c.chain).map((c) => ({ faces: c.island.length, centroid: r3(c.st.centroid), top: r3(c.st.top), bottom: r3(c.st.bottom) })),
    }, null, 1));
  }
  // Spring data in each bone's local space (scene units: metres).
  root.updateMatrixWorld(true);
  const local = (bone, p) => new THREE.Vector3(...p).applyMatrix4(new THREE.Matrix4().copy(bone.matrixWorld).invert()).toArray().map((x) => +x.toFixed(4));
  const springChains = chains.map((ch) => ({ name: ch.name, bones: ch.boneObjs.map((b) => b.name), tail: local(ch.boneObjs[ch.bones - 1], ch.poly[ch.bones]) }));
  const springColliders = (hc.colliders || []).map((c) => {
    const bone = root.getObjectByName(c.bone);
    if (!bone) fail(`hairChains collider ${c.name}: no bone ${c.bone}`);
    const a = c.center || new THREE.Vector3().setFromMatrixPosition(bone.matrixWorld).toArray();
    const out = { name: c.name, bone: c.bone, offset: local(bone, a) };
    let radius = c.radius;
    if (c.to) {
      const to = root.getObjectByName(c.to);
      if (!to) fail(`hairChains collider ${c.name}: no bone ${c.to}`);
      const b = new THREE.Vector3().setFromMatrixPosition(to.matrixWorld).toArray();
      out.to = { bone: c.to, offset: [0, 0, 0] };
      if (radius === 'auto') radius = fitCapsuleRadius(a, b);
    }
    out.radius = +radius.toFixed(4);
    return out;
  });
  return { springChains, springColliders };
}

/** Capsule radius that stays inside the clothed body: 0.9 × the 30th percentile of body/cloth distances to the segment. */
function fitCapsuleRadius(a, b) {
  const ids = new Set(['Klukai_Cloth_1', 'Klukai_Body_(Default)'].map((g) => obj.materials.indexOf(g)));
  const ab = [b[0] - a[0], b[1] - a[1], b[2] - a[2]]; const l2 = ab[0] ** 2 + ab[1] ** 2 + ab[2] ** 2;
  const d = [];
  for (let t = 0; t < triCount; t++) {
    if (!ids.has(obj.faceMat[t])) continue;
    const v = obj.faceV[t * 3]; const p = [obj.positions[v * 3], obj.positions[v * 3 + 1], obj.positions[v * 3 + 2]];
    const s = ((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1] + (p[2] - a[2]) * ab[2]) / l2;
    if (s < 0.15 || s > 0.85) continue;
    d.push(Math.hypot(p[0] - a[0] - s * ab[0], p[1] - a[1] - s * ab[1], p[2] - a[2] - s * ab[2]));
  }
  d.sort((x, y) => x - y);
  return Math.min(0.15, Math.max(0.03, 0.9 * (d[Math.floor(d.length * 0.3)] ?? 0.05)));
}

// Eyes: undo the baked wink and keep the closed lid as a "blink" morph target.
const eyes = openEyesAndBuildBlink(geo, obj, CONFIG.eyes.groups, { openSide: CONFIG.eyes.openSide, modes: CONFIG.eyes.modes, scleraUv: CONFIG.eyes.scleraUv, browFade: CONFIG.eyes.browFade });
log(`eyes: matched ${eyes.matched} corners (moved ${eyes.moved}, unmatched ${eyes.unmatched})`);
if (eyes.moved < 50) fail('eye opening matched too few vertices — check EYE_BOX / groups');

// Inward shrink for skin that hides under clothes (see config `shrink`). Offsets
// are averaged per OBJ vertex so split normals at UV seams do not open cracks.
for (const [name, cfg] of Object.entries(CONFIG.materials)) {
  if (!cfg.shrink || cfg.drop) continue;
  const id = obj.materials.indexOf(name);
  const P = geo.attributes.position.array;
  const Nrm = geo.attributes.normal.array;
  const acc = new Map();
  for (let t = 0; t < triCount; t++) {
    if (obj.faceMat[t] !== id) continue;
    for (let k = 0; k < 3; k++) {
      const c = t * 3 + k;
      const v = obj.faceV[c];
      if (P[c * 3 + 1] > cfg.shrink.maxY) continue;
      const a = acc.get(v) || [0, 0, 0, []];
      a[0] += Nrm[c * 3]; a[1] += Nrm[c * 3 + 1]; a[2] += Nrm[c * 3 + 2]; a[3].push(c);
      acc.set(v, a);
    }
  }
  for (const [, [nx, ny, nz, cs]] of acc) {
    const l = Math.hypot(nx, ny, nz) || 1;
    for (const c of cs) {
      P[c * 3] -= (nx / l) * cfg.shrink.amount;
      P[c * 3 + 1] -= (ny / l) * cfg.shrink.amount;
      P[c * 3 + 2] -= (nz / l) * cfg.shrink.amount;
    }
  }
  log(`shrink ${name}: ${acc.size} vertices by ${cfg.shrink.amount * 1000} mm`);
}

// A "part" = one shipped material: an OBJ material group, or the slice of it
// that samples one atlas (or a flat colour, or carries its own role/sidedness)
// when atlas overrides split the group (see lib/atlas-assign.mjs).
const parts = [];
const flipFaces = new Set(); // inside-out source triangles (override `flip: true`)
const mergedFrom = new Map(); // target group → extra source groups (openJacket)
if (on('openJacket')) mergedFrom.set(CONFIG.openJacket.into, CONFIG.openJacket.from);
const islandReport = DEBUG ? [] : null;
fs.mkdirSync(path.join(repo, 'assets/build'), { recursive: true });
// The output is a derived, personal-use model: make sure git never picks it up
// even if the repo-level .gitignore does not list assets/build/.
fs.writeFileSync(path.join(repo, 'assets/build/.gitignore'), '# derived personal-use model output: never commit\n*\n');
fs.mkdirSync(outDir, { recursive: true });
/** A group's faces in OBJ order, without exact duplicate triangles (same corners, same winding: only cost bytes). */
function dedupFaces(m) {
  const id = obj.materials.indexOf(m);
  const faces = [];
  const seenTri = new Set();
  let dupes = 0;
  for (let t = 0; t < triCount; t++) {
    if (obj.faceMat[t] !== id) continue;
    const v = [obj.faceV[t * 3], obj.faceV[t * 3 + 1], obj.faceV[t * 3 + 2]].map((i) => `${obj.positions[i * 3].toFixed(5)},${obj.positions[i * 3 + 1].toFixed(5)},${obj.positions[i * 3 + 2].toFixed(5)}`);
    const r = v.indexOf([...v].sort()[0]);
    const key = `${v[r]}|${v[(r + 1) % 3]}|${v[(r + 2) % 3]}`;
    if (seenTri.has(key)) { dupes++; continue; }
    seenTri.add(key);
    faces.push(t);
  }
  if (dupes) log(`dedup ${m}: removed ${dupes} duplicate triangles`);
  return faces;
}
for (const m of obj.materials) {
  let cfg = CONFIG.materials[m];
  if (cfg.drop && !DEBUG) continue;
  const extra = mergedFrom.get(m) || [];
  if (extra.length) cfg = { ...cfg, atlasOverrides: [...(CONFIG.openJacket.atlasOverrides || []), ...(cfg.atlasOverrides || [])] };
  let faces = dedupFaces(m);
  if (on('hairCopies') && m === CONFIG.hairCopies.group) {
    const r = dropTwinIslands({ obj, baseFaces: dedupFaces(CONFIG.hairCopies.twinsOf), extraFaces: faces });
    log(`hairCopies: ${m} keeps ${r.added} islands (${r.kept.length} faces), drops ${r.twins} near-copies of ${CONFIG.hairCopies.twinsOf} (${faces.length - r.kept.length} faces)`);
    faces = r.kept;
  }
  for (const g of extra) { // merged groups: add only their islands without a near-copy here
    const r = dropTwinIslands({ obj, baseFaces: faces, extraFaces: dedupFaces(g) });
    faces.push(...r.kept);
    log(`merge ${g} → ${m}: ${r.added} own islands (${r.kept.length} faces) added, ${r.twins} near-copy islands skipped`);
  }
  if (!cfg.drop && (cfg.atlases || cfg.atlasOverrides)) {
    const atlases = cfg.atlases || [cfg.texture];
    const { assignment, overrideOf, stats } = await assignAtlases({
      obj, faces, atlases, texPath: tex, overrides: cfg.atlasOverrides || [], report: islandReport, defaultAtlas: cfg.atlasDefault || (cfg.atlases ? null : cfg.texture),
    });
    if (islandReport) {
      const isl = islandReport.splice(0);
      fs.writeFileSync(path.join(outDir, `islands-${m.replace(/[^A-Za-z0-9_]+/g, '_')}.json`), JSON.stringify(isl, null, 0));
      const amb = isl.filter((i) => i.ambiguous && !i.override);
      if (amb.length) {
        log(`ambiguous ${m}: ${amb.length} islands (score ratio < 1.5, no override), largest first:`);
        for (const i of amb.sort((a, b) => b.faces - a.faces).slice(0, 40)) log(`  #${i.id} ${i.faces} faces at [${i.centroid}] uv [${i.uv}] scores [${i.scores}] → ${String(i.atlas).replace(/^c_ClukaySSR01_slg_|\.png$/g, '')}`);
      }
    }
    log(`atlas split ${m}: ${Object.entries(stats).map(([a, n]) => `${a}=${n}`).join(' ')}`);
    const byKey = new Map();
    for (const f of faces) {
      const spec = partSpec(cfg, assignment.get(f), overrideOf.get(f));
      if (!spec) continue;
      const ov = overrideOf.get(f);
      if (ov?.flip) flipFaces.add(f);
      let p = byKey.get(spec.key);
      if (!p) { p = { ...spec, name: `${m}#${spec.label}`, source: m, faces: [], cfg }; byKey.set(spec.key, p); }
      p.faces.push(f);
    }
    parts.push(...byKey.values());
  } else {
    const role = cfg.role || 'debug';
    parts.push({ name: m, source: m, texture: cfg.texture || null, color: null, role, doubleSided: !!cfg.doubleSided, key: `${cfg.texture}|${role}|${cfg.doubleSided ? 'ds' : 'ss'}`, label: m, faces, cfg });
  }
}
if (!DEBUG) { // one draw call per (texture or colour, role, sidedness)
  const merged = new Map();
  for (const p of parts) {
    if (merged.has(p.key)) merged.get(p.key).faces.push(...p.faces);
    else merged.set(p.key, { ...p, faces: [...p.faces] });
  }
  parts.length = 0;
  parts.push(...merged.values());
}
const attrNames = ['position', 'normal', 'uv', 'skinIndex', 'skinWeight'];
if (CONFIG.bodyCore) {
  const part = parts.find((p) => p.label === 'bodysuit' && p.color && !p.doubleSided);
  if (!part) fail('bodyCore requires the opaque bodysuit material');
  root.updateMatrixWorld(true);
  const joints = CONFIG.bodyCore.joints.map((name) => {
    const index = mesh.skeleton.bones.findIndex((b) => b.name === name);
    if (index < 0) fail(`bodyCore: missing skin joint ${name}`);
    // Inverse binds, not animation frames, define the geometry's coordinate space.
    const bind = mesh.skeleton.boneInverses[index].clone().invert();
    return { index, y: new THREE.Vector3().setFromMatrixPosition(bind).y };
  });
  part.generated = createBodyCore({ ...CONFIG.bodyCore, joints });
  log(`inner body: ${part.generated.position.length / 9} triangles, neck through abdomen, shared bodysuit material`);
}
const out = new THREE.BufferGeometry();
const totalTris = parts.reduce((n, p) => n + p.faces.length + (p.generated?.position.length || 0) / 9, 0);
const morphOut = new Float32Array(totalTris * 9);
for (const name of attrNames) {
  const a = geo.attributes[name];
  if (!a) fail(`FBX geometry lacks ${name}`);
  out.setAttribute(name, new THREE.BufferAttribute(new a.array.constructor(totalTris * 3 * a.itemSize), a.itemSize, a.normalized));
}
let cursor = 0;
parts.forEach((p, pi) => {
  const start = cursor;
  for (const t of p.faces) {
    morphOut.set(eyes.morph.subarray(t * 9, t * 9 + 9), cursor * 9);
    for (const name of attrNames) {
      const a = geo.attributes[name];
      const s = a.itemSize * 3;
      out.attributes[name].array.set(a.array.subarray(t * s, t * s + s), cursor * s);
    }
    if (flipFaces.has(t)) {
      flipTriangle([...attrNames.map((n) => out.attributes[n]), { array: morphOut, itemSize: 3 }], out.attributes.normal, cursor);
    }
    cursor++;
  }
  if (p.generated) {
    for (const name of attrNames) out.attributes[name].array.set(p.generated[name], cursor * 3 * out.attributes[name].itemSize);
    // The generated body does not blink; its morph deltas stay zero.
    cursor += p.generated.position.length / 9;
  }
  out.addGroup(start * 3, (cursor - start) * 3, pi);
});
if (flipFaces.size) log(`flipped ${flipFaces.size} inside-out triangles right side out`);
// glTF UV origin is top-left; three/OBJ/FBX use bottom-left. GLTFExporter would
// flip the *images* on export, but textures are attached later in glTF-Transform
// from the original PNGs, so flip V here instead.
out.morphAttributes.position = [new THREE.BufferAttribute(morphOut, 3)];
out.morphTargetsRelative = true;
{
  const uv = out.attributes.uv.array;
  for (let i = 1; i < uv.length; i += 2) uv[i] = 1 - uv[i];
}
const indexed = mergeVertices(out, 1e-5);
log(`geometry: ${triCount} tris → kept ${totalTris} in ${parts.length} parts, ${indexed.attributes.position.count} verts after weld`);

const materials = parts.map((p) => {
  const mat = new THREE.MeshStandardMaterial({ name: p.name, metalness: 0, roughness: 1 });
  mat.side = p.doubleSided ? THREE.DoubleSide : THREE.FrontSide;
  return mat;
});
const partByName = new Map(parts.map((p) => [p.name, p]));
if (partByName.size !== parts.length) fail('duplicate part names');
mesh.geometry = indexed;
mesh.morphTargetDictionary = { blink: 0 };
mesh.morphTargetInfluences = [0];
mesh.material = materials;
mesh.name = 'Klukai';
geo.dispose();

// ── 3. Clips ────────────────────────────────────────────────────────────────
const boneNames = new Set();
root.traverse((o) => { if (o.isBone) boneNames.add(o.name); });
const restHips = root.getObjectByName('mixamorigHips').position.clone();

const clips = [];
for (const c of CONFIG.clips) {
  const clip = (c.file === CONFIG.baseFbx ? root : loadFbx(c.file)).animations[0];
  if (!clip) fail(`no animation in ${c.file}`);
  clip.name = c.name;
  clip.tracks = clip.tracks.filter((t) => {
    const [node, prop] = t.name.split('.');
    return boneNames.has(node) && (prop === 'quaternion' || (prop === 'position' && node === 'mixamorigHips'));
  });
  fixRootMotion(clip, c);
  clip.resetDuration();
  clip.userData = { loop: c.loop };
  clips.push(clip);
  log(`clip ${c.name.padEnd(9)} ${clip.duration.toFixed(2)}s ${clip.tracks.length} tracks`);
}

/**
 * Keeps her centred in the frame: removes horizontal hip drift relative to the
 * first frame, and for "sleep" (Mixamo keeps the hips at standing height while
 * the body lies flat) lowers the hips so she rests on the ground plane.
 */
function fixRootMotion(clip, c) {
  const t = clip.tracks.find((x) => x.name === 'mixamorigHips.position');
  if (!t) return;
  const v = t.values;
  const x0 = v[0];
  const z0 = v[2];
  for (let i = 0; i < v.length; i += 3) {
    v[i] = v[i] - x0 + restHips.x;
    v[i + 2] = v[i + 2] - z0 + restHips.z;
  }
  if (c.name === 'sleep') {
    for (let i = 1; i < v.length; i += 3) v[i] = 0.14;
  }
}

// ── 4. Export with three.js (geometry + skin + clips; textures added below) ─
const scene = new THREE.Scene();
scene.name = 'Klukai';
root.name = 'KlukaiRig';
scene.add(root);
const glb = await new GLTFExporter().parseAsync(scene, { binary: true, animations: clips, onlyVisible: false });
log(`raw export ${(glb.byteLength / 1048576).toFixed(1)} MB`);

/**
 * Moves primitives whose "blink" morph target actually moves vertices into a
 * separate skinned mesh (same skin), and strips the all-zero target from the
 * rest, so the ~30k body vertices skip morph work in the vertex shader.
 */
function splitHeadMesh(document) {
  const r = document.getRoot();
  const mesh = r.listMeshes()[0];
  const node = r.listNodes().find((n) => n.getMesh() === mesh);
  const head = document.createMesh('KlukaiHead').setWeights([0]).setExtras({ targetNames: ['blink'] });
  for (const prim of mesh.listPrimitives()) {
    const target = prim.listTargets()[0];
    const pos = target?.getAttribute('POSITION');
    let moves = false;
    if (pos) { const a = pos.getArray(); for (let i = 0; i < a.length && !moves; i++) moves = Math.abs(a[i]) > 1e-7; }
    if (moves) { mesh.removePrimitive(prim); head.addPrimitive(prim); }
    else for (const t of prim.listTargets()) { prim.removeTarget(t); t.dispose(); }
  }
  if (!head.listPrimitives().length) { head.dispose(); return; }
  mesh.setWeights([]).setExtras({});
  const headNode = document.createNode('KlukaiHead').setMesh(head).setSkin(node.getSkin());
  const parent = node.getParentNode();
  if (parent) parent.addChild(headNode); else r.listScenes()[0].addChild(headNode);
  log(`head mesh: ${head.listPrimitives().length} primitives carry the blink morph; body: ${mesh.listPrimitives().length}`);
}

/**
 * `_OUTLINE` (vec4, normalized int8) on every primitive of every mesh: smoothed
 * normal across all primitives + width mask (lib/outline.mjs). Runs on the
 * final float geometry (after shrink, eye repair, join, split and prune), before
 * meshopt quantises positions.
 */
function addOutlineAttribute(document, partOfMaterial) {
  const o = CONFIG.outline;
  const r = document.getRoot();
  const skin = r.listSkins()[0];
  const handRe = new RegExp(o.handJoints);
  const handSel = (skin ? skin.listJoints() : []).map((j) => handRe.test(j.getName()));
  const prims = r.listMeshes().flatMap((m) => m.listPrimitives());
  const data = prims.map((prim) => {
    const P = prim.getAttribute('POSITION');
    if (!P || P.getNormalized() || !(P.getArray() instanceof Float32Array)) fail('outline: expected float positions before quantization');
    return { positions: P.getArray(), normals: prim.getAttribute('NORMAL')?.getArray() || null, indices: prim.getIndices()?.getArray() || null };
  });
  const smooth = smoothOutlineNormals(data, { quantum: o.quantum, minLength: o.minLength });
  const buffer = r.listBuffers()[0];
  const summary = [];
  prims.forEach((prim, i) => {
    const mat = prim.getMaterial();
    const part = partOfMaterial.get(mat);
    const role = part?.role ?? mat?.getExtras()?.role;
    const zero = o.zeroRoles.includes(role) || (!!part?.texture && o.zeroTextures.includes(part.texture));
    const J = prim.getAttribute('JOINTS_0'); const W = prim.getAttribute('WEIGHTS_0');
    const handShare = J && W ? jointShare(J.getArray(), W.getArray(), handSel) : null;
    const w = outlineWidths({ positions: data[i].positions, zero, masked: o.maskRoles.includes(role), masks: o.faceMask, handShare, handWidth: o.handWidth });
    const acc = document.createAccessor(`${mat?.getName() || 'prim'}_outline`).setType('VEC4').setArray(packOutline(smooth[i], w)).setNormalized(true).setBuffer(buffer);
    prim.setAttribute(o.attribute, acc);
    let lo = 0; let mid = 0;
    for (const x of w) { if (x < 0.05) lo++; else if (x < 0.95) mid++; }
    summary.push(`${mat?.getName()}(${role}) w0=${lo} wmid=${mid}/${w.length}`);
  });
  log(`outline ${o.attribute} on ${prims.length} primitives: ${summary.join(', ')}`);
}

// ── 5. Textures, materials, compression (glTF-Transform) ────────────────────
await MeshoptEncoder.ready;
await MeshoptDecoder.ready;
const io = new NodeIO()
  .registerExtensions(ALL_EXTENSIONS)
  .registerDependencies({ 'meshopt.encoder': MeshoptEncoder, 'meshopt.decoder': MeshoptDecoder });
const doc = await io.readBinary(new Uint8Array(glb));
const webp = doc.createExtension(EXTTextureWebP).setRequired(true);
void webp;

const texCache = new Map();
async function textureFor(file, maxSize) {
  const key = `${file}@${maxSize}`;
  if (texCache.has(key)) return texCache.get(key);
  const p = tex(file);
  if (!fs.existsSync(p)) fail(`missing texture ${p}`);
  const img = sharp(p).resize({ width: maxSize, height: maxSize, fit: 'inside', withoutEnlargement: true });
  const data = DEBUG ? await img.png().toBuffer() : await img.webp({ quality: CONFIG.webpQuality, alphaQuality: 100, effort: 6 }).toBuffer();
  const t = doc.createTexture(path.basename(file, path.extname(file)))
    .setImage(new Uint8Array(data))
    .setMimeType(DEBUG ? 'image/png' : 'image/webp')
    .setURI(`${path.basename(file, path.extname(file))}.${DEBUG ? 'png' : 'webp'}`);
  texCache.set(key, t);
  return t;
}

/** `_rmo` → glTF ORM (R occlusion, G roughness, B metallic), lossless. */
async function ormTextureFor(file) {
  const key = `orm:${file}`;
  if (texCache.has(key)) return texCache.get(key);
  const p = tex(file);
  if (!fs.existsSync(p)) fail(`missing texture ${p}`);
  const img = await ormImage(p, { maxSize: Math.min(CONFIG.orm.maxSize, CONFIG.textureMaxSizeHard), format: DEBUG ? 'png' : 'webp', layout: CONFIG.orm.from });
  const name = path.basename(file, path.extname(file)).replace(/_rmo$/, '_orm');
  const t = doc.createTexture(name).setImage(img.data).setMimeType(img.mime).setURI(`${name}.${DEBUG ? 'png' : 'webp'}`);
  texCache.set(key, t);
  return t;
}

const debugColors = [0xff4040, 0x40ff40, 0x4040ff, 0xffff40, 0xff40ff, 0x40ffff, 0xff9040, 0x9040ff];
let dbg = 0;
const partOfMaterial = new Map();
const usedNames = new Set();
for (const m of doc.getRoot().listMaterials()) {
  const part = partByName.get(m.getName());
  if (!part) fail(`exported material without a part: ${m.getName()}`);
  const cfg = part.cfg;
  partOfMaterial.set(m, part);
  m.setMetallicFactor(0).setRoughnessFactor(1).setAlphaMode('OPAQUE').setDoubleSided(part.doubleSided);
  m.setExtras(DEBUG ? { role: part.role || 'debug', source: part.source, dropped: !!cfg.drop } : { role: part.role });
  if (!DEBUG) {
    let nm = part.texture ? path.basename(part.texture, '.png') : part.label;
    if (usedNames.has(nm)) nm = `${nm}_${part.role}${part.doubleSided ? '' : '_1s'}`;
    usedNames.add(nm);
    m.setName(nm);
  }
  if (part.texture) {
    m.setBaseColorTexture(await textureFor(part.texture, Math.min(cfg.maxSize || CONFIG.textureMaxSize, CONFIG.textureMaxSizeHard)));
    const rmo = CONFIG.orm?.maps?.[part.texture];
    if (rmo) {
      // One ORM image for both slots. Factors 1: the maps carry the values
      // (metal mask on buckles/zips only), so PBR viewers read them right too.
      const orm = await ormTextureFor(rmo);
      m.setOcclusionTexture(orm).setOcclusionStrength(1).setMetallicRoughnessTexture(orm).setMetallicFactor(1).setRoughnessFactor(1);
    }
  } else if (part.color) {
    const c = new THREE.Color(part.color); // sRGB hex → linear, as glTF baseColorFactor expects
    m.setBaseColorFactor([c.r, c.g, c.b, 1]);
  } else {
    const c = new THREE.Color(debugColors[dbg++ % debugColors.length]);
    m.setBaseColorFactor([c.r, c.g, c.b, 1]);
  }
}
for (const a of doc.getRoot().listAnimations()) {
  const c = CONFIG.clips.find((x) => x.name === a.getName());
  a.setExtras({ loop: !!c?.loop });
}
doc.getRoot().getAsset().generator = 'klukai tools/avatar/build.mjs (personal use only)';
if (springData) { // three: gltf.scene.userData.springChains / springColliders
  const sc = doc.getRoot().listScenes()[0];
  sc.setExtras({ ...sc.getExtras(), ...springData });
}

const dedupTypes = [PropertyType.ACCESSOR, PropertyType.MESH, PropertyType.TEXTURE];
if (!DEBUG) dedupTypes.push(PropertyType.MATERIAL);
const resampleTol = +(process.env.AVATAR_RESAMPLE_TOL || CONFIG.resampleTolerance);
const meshoptLevel = process.env.AVATAR_MESHOPT_LEVEL || CONFIG.meshoptLevel;
if (!DEBUG) {
  // Fewer draw calls: identical materials (same texture/role/sidedness) merge,
  // then primitives sharing a material join. Morph targets only on the head.
  // GLTFExporter shares one attribute set across all primitives of a mesh;
  // give each primitive its own compact vertex subset first.
  for (const m of doc.getRoot().listMeshes()) for (const prim of m.listPrimitives()) compactPrimitive(prim);
  await doc.transform(dedup({ propertyTypes: [PropertyType.MATERIAL] }), join({ keepNamed: false }));
  splitHeadMesh(doc);
}
await doc.transform(resample({ tolerance: resampleTol }), dedup({ propertyTypes: dedupTypes }), prune({ keepAttributes: false }));
if (CONFIG.outline) addOutlineAttribute(doc, partOfMaterial);
// 'scene' quantization volume: head + body meshes keep sharing one skin.
if (!DEBUG) await doc.transform(meshopt({ encoder: MeshoptEncoder, level: meshoptLevel, quantizationVolume: 'scene' }));

const bytes = await io.writeBinary(doc);
if (!DEBUG && bytes.byteLength > CONFIG.budgetBytes) fail(`GLB ${(bytes.byteLength / 1048576).toFixed(2)} MB exceeds budget`);
// The output dir is bind-mounted read-only into the live container: write a
// temp file and rename it, so the served GLB is always complete.
const tmpFile = `${outFile}.tmp-${process.pid}`;
fs.writeFileSync(tmpFile, bytes);
fs.renameSync(tmpFile, outFile);

const mb = bytes.byteLength / 1048576;
log(`wrote ${path.relative(repo, outFile)} ${mb.toFixed(2)} MB`);
log(`clips: ${doc.getRoot().listAnimations().map((a) => a.getName()).join(', ')}`);
log(`materials: ${doc.getRoot().listMaterials().map((m) => `${m.getName()}(${m.getExtras().role}${m.getDoubleSided() ? ',2s' : ''})`).join(', ')}`);
let gpu = 0;
for (const t of doc.getRoot().listTextures()) {
  const s = t.getSize();
  if (s) gpu += textureGpuBytes(s[0], s[1]);
  log(`texture ${t.getName().padEnd(32)} ${s?.[0]}x${s?.[1]} ${t.getMimeType()} ${(t.getImage().byteLength / 1024).toFixed(0)} KB`);
}
log(`texture GPU memory ≈ ${(gpu / 1048576).toFixed(1)} MB (RGBA8 + mips, each texture once)`);
if (!DEBUG && bytes.byteLength > CONFIG.budgetBytes) fail(`GLB ${mb.toFixed(2)} MB exceeds budget ${(CONFIG.budgetBytes / 1048576).toFixed(0)} MB`);
