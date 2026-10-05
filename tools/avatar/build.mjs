#!/usr/bin/env node
// Builds assets/build/klukai.glb from the Mixamo FBX clips + OBJ material groups
// + PNG textures. PERSONAL USE ONLY: the inputs are game-ripped and the output is
// a derived model. Never commit either; serve only through the authenticated API.
//
//   node build.mjs            → optimized GLB (webp ≤1024, meshopt, resampled clips)
//   node build.mjs --debug    → also keeps dropped material groups, no compression
//                               (assets/build/klukai.debug.glb) for inspection
import { THREE } from './lib/node-three.mjs';
import { readObjFaces } from './lib/obj-faces.mjs';
import { assignAtlases } from './lib/atlas-assign.mjs';
import { openEyesAndBuildBlink } from './lib/eyes.mjs';
import { CONFIG, validateConfig } from './avatar.config.mjs';
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
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../..');
const DEBUG = process.argv.includes('--debug');
const log = (...a) => console.log('[avatar]', ...a);

function fail(msg) { console.error(`[avatar] ERROR: ${msg}`); process.exit(1); }

const problems = validateConfig(CONFIG);
if (problems.length) fail(`invalid config:\n  ${problems.join('\n  ')}`);

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
// that samples one atlas when the group mixes atlases (see lib/atlas-assign.mjs).
const parts = [];
const islandReport = DEBUG ? [] : null;
fs.mkdirSync(path.join(repo, 'assets/build'), { recursive: true });
// The output is a derived, personal-use model: make sure git never picks it up
// even if the repo-level .gitignore does not list assets/build/.
fs.writeFileSync(path.join(repo, 'assets/build/.gitignore'), '# derived personal-use model output: never commit\n*\n');
for (const m of obj.materials) {
  const cfg = CONFIG.materials[m];
  if (cfg.drop && !DEBUG) continue;
  const id = obj.materials.indexOf(m);
  const faces = [];
  const seenTri = new Set();
  let dupes = 0;
  for (let t = 0; t < triCount; t++) {
    if (obj.faceMat[t] !== id) continue;
    // exact duplicate triangles (same corners, same winding) only cost bytes
    const v = [obj.faceV[t * 3], obj.faceV[t * 3 + 1], obj.faceV[t * 3 + 2]].map((i) => `${obj.positions[i * 3].toFixed(5)},${obj.positions[i * 3 + 1].toFixed(5)},${obj.positions[i * 3 + 2].toFixed(5)}`);
    const r = v.indexOf([...v].sort()[0]);
    const key = `${v[r]}|${v[(r + 1) % 3]}|${v[(r + 2) % 3]}`;
    if (seenTri.has(key)) { dupes++; continue; }
    seenTri.add(key);
    faces.push(t);
  }
  if (dupes) log(`dedup ${m}: removed ${dupes} duplicate triangles`);
  if (cfg.atlases && !cfg.drop) {
    const { assignment, stats } = await assignAtlases({
      obj, faces, atlases: cfg.atlases, texPath: tex, overrides: cfg.atlasOverrides || [], report: islandReport, defaultAtlas: cfg.atlasDefault,
    });
    if (islandReport) fs.writeFileSync(path.join(repo, 'assets/build', `islands-${m.replace(/[^A-Za-z0-9_]+/g, '_')}.json`), JSON.stringify(islandReport.splice(0), null, 0));
    log(`atlas split ${m}: ${Object.entries(stats).map(([a, n]) => `${a}=${n}`).join(' ')}`);
    for (const atlas of cfg.atlases) {
      const sub = faces.filter((f) => assignment.get(f) === atlas);
      if (sub.length) parts.push({ name: `${m}#${atlas.replace(/\.png$/, '')}`, source: m, texture: atlas, faces: sub, cfg });
    }
  } else {
    parts.push({ name: m, source: m, texture: cfg.texture, faces, cfg });
  }
}
if (!DEBUG) { // one draw call per (texture, role, sidedness)
  const merged = new Map();
  for (const p of parts) {
    const key = `${p.texture}|${p.cfg.role}|${!!p.cfg.doubleSided}`;
    if (merged.has(key)) merged.get(key).faces.push(...p.faces);
    else merged.set(key, { ...p, faces: [...p.faces] });
  }
  parts.length = 0;
  parts.push(...merged.values());
}
const attrNames = ['position', 'normal', 'uv', 'skinIndex', 'skinWeight'];
const out = new THREE.BufferGeometry();
const totalTris = parts.reduce((n, p) => n + p.faces.length, 0);
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
    cursor++;
  }
  out.addGroup(start * 3, (cursor - start) * 3, pi);
});
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
  mat.side = p.cfg.doubleSided ? THREE.DoubleSide : THREE.FrontSide;
  return mat;
});
const partByName = new Map(parts.map((p) => [p.name, p]));
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

const debugColors = [0xff4040, 0x40ff40, 0x4040ff, 0xffff40, 0xff40ff, 0x40ffff, 0xff9040, 0x9040ff];
let dbg = 0;
for (const m of doc.getRoot().listMaterials()) {
  const part = partByName.get(m.getName());
  if (!part) fail(`exported material without a part: ${m.getName()}`);
  const cfg = part.cfg;
  m.setMetallicFactor(0).setRoughnessFactor(1).setAlphaMode('OPAQUE').setDoubleSided(!!cfg.doubleSided);
  m.setExtras(DEBUG ? { role: cfg.role || 'debug', source: part.source, dropped: !!cfg.drop } : { role: cfg.role });
  if (!DEBUG) m.setName(path.basename(part.texture || part.name, '.png'));
  if (part.texture) {
    m.setBaseColorTexture(await textureFor(part.texture, Math.min(cfg.maxSize || CONFIG.textureMaxSize, CONFIG.textureMaxSizeHard)));
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

const dedupTypes = [PropertyType.ACCESSOR, PropertyType.MESH, PropertyType.TEXTURE];
if (!DEBUG) dedupTypes.push(PropertyType.MATERIAL);
const resampleTol = +(process.env.AVATAR_RESAMPLE_TOL || CONFIG.resampleTolerance);
const meshoptLevel = process.env.AVATAR_MESHOPT_LEVEL || CONFIG.meshoptLevel;
const ops = [resample({ tolerance: resampleTol }), dedup({ propertyTypes: dedupTypes }), prune({ keepAttributes: false })];
// 'scene' quantization volume: head + body meshes keep sharing one skin.
if (!DEBUG) ops.push(meshopt({ encoder: MeshoptEncoder, level: meshoptLevel, quantizationVolume: 'scene' }));
if (!DEBUG) {
  // Fewer draw calls: identical materials (same texture/role/sidedness) merge,
  // then primitives sharing a material join. Morph targets only on the head.
  // GLTFExporter shares one attribute set across all primitives of a mesh;
  // give each primitive its own compact vertex subset first.
  for (const m of doc.getRoot().listMeshes()) for (const prim of m.listPrimitives()) compactPrimitive(prim);
  await doc.transform(dedup({ propertyTypes: [PropertyType.MATERIAL] }), join({ keepNamed: false }));
  splitHeadMesh(doc);
}
await doc.transform(...ops);

const outFile = process.env.AVATAR_OUT ? path.resolve(process.env.AVATAR_OUT) : path.join(repo, DEBUG ? CONFIG.outFile.replace(/\.glb$/, '.debug.glb') : CONFIG.outFile);
fs.mkdirSync(path.dirname(outFile), { recursive: true });
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
log(`materials: ${doc.getRoot().listMaterials().map((m) => m.getName()).join(', ')}`);
for (const t of doc.getRoot().listTextures()) {
  const s = t.getSize();
  log(`texture ${t.getName().padEnd(32)} ${s?.[0]}x${s?.[1]} ${t.getMimeType()} ${(t.getImage().byteLength / 1024).toFixed(0)} KB`);
}
if (!DEBUG && bytes.byteLength > CONFIG.budgetBytes) fail(`GLB ${mb.toFixed(2)} MB exceeds budget ${(CONFIG.budgetBytes / 1048576).toFixed(0)} MB`);
