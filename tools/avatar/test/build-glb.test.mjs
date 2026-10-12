// Structure of the DEV build (assets/build/dev/klukai.glb, made with
//   AVATAR_OUT=../../assets/build/dev/klukai.glb node build.mjs
// ). Skipped when it is absent: the inputs are personal-use and never in CI.
// Checks what the viewer relies on, end to end through meshopt + quantisation
// and three's GLTFLoader (the same loader the app uses).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { CONFIG, REQUIRED_CLIPS } from '../avatar.config.mjs';
import { repackOrm } from '../lib/orm.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../../..');
const glbPath = process.env.AVATAR_TEST_GLB ? path.resolve(process.env.AVATAR_TEST_GLB) : path.join(repo, 'assets/build/dev/klukai.glb');
const have = fs.existsSync(glbPath);
const skip = have ? false : 'no dev build (assets are personal-use, never in CI)';

async function readDoc() {
  const { NodeIO } = await import('@gltf-transform/core');
  const { ALL_EXTENSIONS } = await import('@gltf-transform/extensions');
  const { MeshoptDecoder } = await import('meshoptimizer');
  await MeshoptDecoder.ready;
  return new NodeIO().registerExtensions(ALL_EXTENSIONS).registerDependencies({ 'meshopt.decoder': MeshoptDecoder }).read(glbPath);
}

async function loadThree() {
  const { THREE } = await import('../lib/node-three.mjs');
  const { GLTFLoader } = await import('three/examples/jsm/loaders/GLTFLoader.js');
  const { MeshoptDecoder } = await import('three/examples/jsm/libs/meshopt_decoder.module.js');
  // Images are irrelevant here: hand GLTFLoader an empty texture right away.
  const orig = THREE.TextureLoader.prototype.load;
  THREE.TextureLoader.prototype.load = function load(url, onLoad) { const t = new THREE.Texture(); setTimeout(() => onLoad?.(t), 0); return t; };
  const warn = console.warn; console.warn = () => {};
  try {
    const b = fs.readFileSync(glbPath);
    return await new Promise((res, rej) => new GLTFLoader().setMeshoptDecoder(MeshoptDecoder)
      .parse(b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength), '', res, rej));
  } finally { THREE.TextureLoader.prototype.load = orig; console.warn = warn; }
}

// Bind-pose metres, deliberately in the two source-model gaps. Excluding
// the jacket and hair prevents either from hiding a missing body again.
for (const [name, y, z] of [['neck', 1.28, -0.016], ['stomach', 1.0, -0.008]]) {
  test(`dev GLB: ${name} has a body surface underneath hair and jacket, from every side`, { skip }, async () => {
    const { THREE } = await import('../lib/node-three.mjs');
    const gltf = await loadThree();
    gltf.scene.updateMatrixWorld(true);
    const body = [];
    gltf.scene.traverse((m) => {
      if (m.isSkinnedMesh && m.material.name === 'bodysuit') {
        m.skeleton.update();
        body.push(m);
      }
    });
    assert.ok(body.length, 'opaque inner bodysuit exists');
    for (let degrees = 0; degrees < 360; degrees += 30) {
      const angle = degrees * Math.PI / 180;
      const radial = new THREE.Vector3(Math.sin(angle), 0, Math.cos(angle));
      const origin = new THREE.Vector3(0, y, z).addScaledVector(radial, 0.5);
      const ray = new THREE.Raycaster(origin, radial.clone().negate(), 0, 0.5);
      const hits = ray.intersectObjects(body, false);
      assert.ok(hits.length, `${name} missing at ${degrees} degrees`);
      assert.ok(hits[0].distance > 0.35 && hits[0].distance < 0.49, `${name} surface fits inside the outfit`);
    }
  });
}

test('dev GLB: size, draw calls, clips, one shared skin', { skip }, async () => {
  assert.ok(fs.statSync(glbPath).size <= CONFIG.budgetBytes, 'under the 15 MB budget');
  const r = (await readDoc()).getRoot();
  assert.equal(r.listSkins().length, 1);
  const prims = r.listMeshes().flatMap((m) => m.listPrimitives());
  assert.ok(prims.length <= 8, `draw calls ${prims.length}`);
  assert.deepEqual(r.listNodes().filter((n) => n.getMesh()).map((n) => n.getName()).sort(), ['Klukai', 'KlukaiHead'], 'body + blink head mesh');
  assert.ok(r.listNodes().filter((n) => n.getMesh()).every((n) => n.getSkin() === r.listSkins()[0]));
  assert.deepEqual(r.listAnimations().map((a) => a.getName()).sort(), [...REQUIRED_CLIPS].sort());
});

test('dev GLB: Head is a skin joint carrying the face/cap weight', { skip }, async () => {
  const r = (await readDoc()).getRoot();
  const joints = r.listSkins()[0].listJoints().map((j) => j.getName());
  const hi = joints.indexOf('mixamorigHead');
  assert.ok(hi >= 0, 'mixamorigHead in skin.joints');
  let head = 0; let neck = 0; const ni = joints.indexOf('mixamorigNeck');
  for (const p of r.listMeshes().flatMap((m) => m.listPrimitives())) {
    const J = p.getAttribute('JOINTS_0'); const W = p.getAttribute('WEIGHTS_0');
    const a = []; const b = [];
    for (let i = 0; i < J.getCount(); i++) {
      J.getElement(i, a); W.getElement(i, b);
      for (let k = 0; k < 4; k++) { if (a[k] === hi) head += b[k]; if (a[k] === ni) neck += b[k]; }
    }
  }
  assert.ok(head > 1000, `head weight sum ${head}`);
  assert.ok(head > neck * 5, 'the head, not the neck, carries the face');
});

test('dev GLB: _OUTLINE on every primitive (normalized int8 vec4), loads in three as _outline', { skip }, async () => {
  const r = (await readDoc()).getRoot();
  for (const p of r.listMeshes().flatMap((m) => m.listPrimitives())) {
    const a = p.getAttribute(CONFIG.outline.attribute);
    assert.ok(a, `${p.getMaterial().getName()} has ${CONFIG.outline.attribute}`);
    assert.equal(a.getType(), 'VEC4'); assert.ok(a.getNormalized()); assert.ok(a.getArray() instanceof Int8Array);
    assert.equal(a.getCount(), p.getAttribute('POSITION').getCount());
  }
  const gltf = await loadThree();
  let meshes = 0; let zero = 0; let total = 0;
  gltf.scene.traverse((o) => {
    if (!o.isMesh) return;
    meshes++;
    const at = o.geometry.getAttribute('_outline');
    assert.ok(at, `${o.name} lacks _outline`);
    assert.equal(at.itemSize, 4);
    for (let i = 0; i < at.count; i++) {
      const l = Math.hypot(at.getX(i), at.getY(i), at.getZ(i));
      assert.ok(l > 0.97 && l < 1.03, `${o.name} vertex ${i} outline normal length ${l}`);
      const w = at.getW(i);
      assert.ok(w >= 0 && w <= 1, `w ${w}`);
      if (w === 0) zero++;
      total++;
    }
  });
  assert.ok(meshes >= 8, `${meshes} mesh primitives in three`);
  assert.ok(zero > 0 && zero < total / 4, `masked vertices ${zero}/${total}`);
});

test('dev GLB: three sees Head in the skeleton, ORM on cloth, a flat bodysuit, hair at 2048', { skip }, async () => {
  const gltf = await loadThree();
  const head = gltf.scene.getObjectByName('mixamorigHead');
  const mats = new Map();
  gltf.scene.traverse((o) => {
    if (!o.isSkinnedMesh) return;
    assert.ok(o.skeleton.bones.includes(head), `${o.name}: Head is a bone of the skeleton`);
    for (const m of [].concat(o.material)) mats.set(m.name, m);
  });
  for (const m of mats.values()) assert.ok(m.userData.role, `${m.name} keeps extras.role`);
  const cloth = [...mats.values()].filter((m) => /cloth[12]_d$/.test(m.name));
  assert.equal(cloth.length, 2);
  for (const m of cloth) {
    assert.ok(m.aoMap, `${m.name} aoMap`); assert.ok(m.metalnessMap, `${m.name} metalnessMap`); assert.ok(m.roughnessMap);
    assert.equal(m.userData.role, 'cloth');
  }
  const suit = mats.get('bodysuit');
  assert.ok(suit, 'bodysuit material (shorts + high collar)');
  assert.equal(suit.userData.role, 'cloth');
  assert.equal(suit.side, 0, 'single-sided (FrontSide)');
  assert.equal(suit.map, null, 'flat colour, no texture');
  assert.ok(suit.color.r < 0.05 && suit.color.g < 0.05 && suit.color.b < 0.05, 'near black');
});

test('dev GLB: textures (hair 2048, others ≤ 1024, ORM lossless and channel-exact)', { skip }, async () => {
  const r = (await readDoc()).getRoot();
  const byName = new Map(r.listTextures().map((t) => [t.getName(), t]));
  assert.deepEqual(byName.get('c_Clukay_hair_d').getSize(), [2048, 2048]);
  for (const [n, t] of byName) {
    assert.equal(t.getMimeType(), 'image/webp', n);
    if (n !== 'c_Clukay_hair_d') assert.ok(Math.max(...t.getSize()) <= 1024, `${n} ${t.getSize()}`);
  }
  for (const m of r.listMaterials().filter((x) => /cloth[12]_d$/.test(x.getName()))) {
    const occ = m.getOcclusionTexture(); const mr = m.getMetallicRoughnessTexture();
    assert.ok(occ && mr && occ === mr, `${m.getName()}: one ORM image for both slots`);
    assert.deepEqual(occ.getSize(), [CONFIG.orm.maxSize, CONFIG.orm.maxSize]);
    assert.equal(m.getMetallicFactor(), 1); assert.equal(m.getRoughnessFactor(), 1);
    const rmo = path.join(repo, CONFIG.textureDir, CONFIG.orm.maps[`${m.getName()}.png`]);
    if (!fs.existsSync(rmo)) continue;
    const { default: sharp } = await import('sharp');
    const got = await sharp(Buffer.from(occ.getImage())).removeAlpha().raw().toBuffer();
    const src = await sharp(rmo).resize({ width: CONFIG.orm.maxSize, height: CONFIG.orm.maxSize, fit: 'inside' }).raw().toBuffer({ resolveWithObject: true });
    const want = repackOrm(src.data, src.info.channels, CONFIG.orm.from);
    assert.equal(got.length, want.length);
    let diff = 0; for (let i = 0; i < want.length; i++) diff = Math.max(diff, Math.abs(got[i] - want[i]));
    assert.equal(diff, 0, `${m.getName()}: decoded ORM differs from the repacked source by ${diff}`);
  }
});
