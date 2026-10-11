// Structure of the wave-2 dev build, made with
//   AVATAR_FEATURES=openJacket,hairChains AVATAR_OUT=../../assets/build/dev/w2/klukai.glb node build.mjs
// Skipped when absent (personal-use assets, never in CI). Checks the spring
// contract the viewer builds against, through three's GLTFLoader.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { CONFIG, REQUIRED_CLIPS, HAIR_CHAINS } from '../avatar.config.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../../..');
const glbPath = process.env.AVATAR_TEST_W2_GLB ? path.resolve(process.env.AVATAR_TEST_W2_GLB) : path.join(repo, 'assets/build/dev/w2/klukai.glb');
const w1Path = path.join(repo, 'assets/build/dev/klukai.glb');
const skip = fs.existsSync(glbPath) ? false : 'no wave-2 dev build';

let cache = null;
async function load(file = glbPath) {
  if (file === glbPath && cache) return cache;
  const { THREE } = await import('../lib/node-three.mjs');
  const { GLTFLoader } = await import('three/examples/jsm/loaders/GLTFLoader.js');
  const { MeshoptDecoder } = await import('three/examples/jsm/libs/meshopt_decoder.module.js');
  const orig = THREE.TextureLoader.prototype.load;
  THREE.TextureLoader.prototype.load = function load(url, onLoad) { const t = new THREE.Texture(); setTimeout(() => onLoad?.(t), 0); return t; };
  const warn = console.warn; console.warn = () => {};
  try {
    const b = fs.readFileSync(file);
    const gltf = await new Promise((res, rej) => new GLTFLoader().setMeshoptDecoder(MeshoptDecoder)
      .parse(b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength), '', res, rej));
    gltf.scene.updateMatrixWorld(true);
    const out = { gltf, THREE };
    if (file === glbPath) cache = out;
    return out;
  } finally { THREE.TextureLoader.prototype.load = orig; console.warn = warn; }
}

const skinnedOf = (scene) => { const l = []; scene.traverse((o) => { if (o.isSkinnedMesh) l.push(o); }); return l; };

test('w2 GLB: budget, draw calls, clips, outline still on every primitive', { skip }, async () => {
  assert.ok(fs.statSync(glbPath).size <= CONFIG.budgetBytes);
  const { gltf } = await load();
  const meshes = skinnedOf(gltf.scene);
  assert.ok(meshes.length <= 8, `${meshes.length} draw calls`);
  for (const m of meshes) assert.ok(m.geometry.getAttribute('_outline'), `${m.name} _outline`);
  assert.deepEqual(gltf.animations.map((a) => a.name).sort(), [...REQUIRED_CLIPS].sort());
  assert.ok(!gltf.animations.some((a) => a.tracks.some((t) => /hair_/.test(t.name))), 'chains are unanimated');
});

test('w2 GLB: springChains contract (names, bone order, parents, tails)', { skip }, async () => {
  const { gltf, THREE } = await load();
  const chains = gltf.scene.userData.springChains;
  assert.ok(Array.isArray(chains), 'gltf.scene.userData.springChains');
  assert.deepEqual(chains.map((c) => c.name), HAIR_CHAINS.chains.map((c) => c.name));
  const sk = skinnedOf(gltf.scene)[0].skeleton;
  for (const ch of chains) {
    const spec = HAIR_CHAINS.chains.find((c) => c.name === ch.name);
    assert.deepEqual(ch.bones, Array.from({ length: spec.bones }, (_, i) => `hair_${ch.name}_${i}`), 'root → tip names');
    const bones = ch.bones.map((n) => gltf.scene.getObjectByName(n));
    bones.forEach((b, i) => {
      assert.ok(b?.isBone, `${ch.bones[i]} is a bone`);
      assert.ok(sk.bones.includes(b), `${b.name} is a skin joint`);
      assert.equal(b.parent.name, i ? ch.bones[i - 1] : HAIR_CHAINS.parent, `${b.name} parent`);
    });
    assert.equal(ch.tail.length, 3); assert.ok(ch.tail.every(Number.isFinite));
    const last = bones.at(-1);
    const tip = new THREE.Vector3(...ch.tail).applyMatrix4(last.matrixWorld);
    const lastPos = new THREE.Vector3().setFromMatrixPosition(last.matrixWorld);
    const rootPos = new THREE.Vector3().setFromMatrixPosition(bones[0].matrixWorld);
    assert.ok(tip.y < lastPos.y && lastPos.y < rootPos.y, `${ch.name} hangs root → tip`);
    assert.ok(tip.distanceTo(lastPos) > 0.03 && tip.distanceTo(lastPos) < 0.3, `${ch.name} tail length`);
  }
});

test('w2 GLB: springColliders shapes reference real bones', { skip }, async () => {
  const { gltf } = await load();
  const cols = gltf.scene.userData.springColliders;
  assert.ok(Array.isArray(cols) && cols.length >= 4);
  for (const c of cols) {
    assert.ok(gltf.scene.getObjectByName(c.bone)?.isBone, `${c.bone}`);
    assert.equal(c.offset.length, 3); assert.ok(c.offset.every(Number.isFinite));
    assert.ok(c.radius > 0.02 && c.radius < 0.2, `${c.name} radius ${c.radius}`);
    if (c.to) { assert.ok(gltf.scene.getObjectByName(c.to.bone)?.isBone); assert.equal(c.to.offset.length, 3); }
  }
  assert.ok(cols.some((c) => c.bone === 'mixamorigHead'), 'head collider');
});

test('w2 GLB: inverse binds consistent for all joints (rest world · IBM identical)', { skip }, async () => {
  const { gltf, THREE } = await load();
  const sk = skinnedOf(gltf.scene)[0].skeleton;
  const ref = new THREE.Matrix4().multiplyMatrices(sk.bones[0].matrixWorld, sk.boneInverses[0]);
  let worst = 0;
  sk.bones.forEach((b, i) => {
    const m = new THREE.Matrix4().multiplyMatrices(b.matrixWorld, sk.boneInverses[i]);
    for (let k = 0; k < 16; k++) worst = Math.max(worst, Math.abs(m.elements[k] - ref.elements[k]));
  });
  assert.ok(worst < 1e-4, `max deviation ${worst}`);
  assert.ok(sk.bones.length >= 52 + HAIR_CHAINS.chains.reduce((n, c) => n + c.bones, 0));
});

test('w2 GLB: chain hair weighted to head + one chain only (no arm/thigh/spine), ≤ 4, normalised', { skip }, async () => {
  const { gltf } = await load();
  let chained = 0; const perChain = {};
  for (const m of skinnedOf(gltf.scene)) {
    const names = m.skeleton.bones.map((b) => b.name);
    const SI = m.geometry.attributes.skinIndex; const SW = m.geometry.attributes.skinWeight;
    for (let i = 0; i < SI.count; i++) {
      const js = []; let sum = 0;
      for (let k = 0; k < 4; k++) { const w = SW.getComponent(i, k); sum += w; if (w > 0) js.push(names[SI.getComponent(i, k)]); }
      if (!js.some((n) => /^hair_/.test(n))) continue;
      chained++;
      assert.ok(Math.abs(sum - 1) < 0.02, `weights sum ${sum}`);
      for (const n of js) assert.ok(/^hair_/.test(n) || n === HAIR_CHAINS.parent, `chain hair vertex also on ${n}`);
      const chainsHere = new Set(js.filter((n) => /^hair_/.test(n)).map((n) => n.split('_')[1]));
      assert.equal(chainsHere.size, 1, 'one chain per vertex');
      const c = [...chainsHere][0]; perChain[c] = (perChain[c] || 0) + 1;
    }
  }
  assert.ok(chained > 3000, `${chained} chain vertices`);
  for (const c of HAIR_CHAINS.chains) assert.ok(perChain[c.name] > 50, `${c.name}: ${perChain[c.name]} vertices`);
});

test('w2 GLB: clips identical to the wave-1 build; hair copies gone from the face', { skip }, async (t) => {
  const { gltf } = await load();
  const face = skinnedOf(gltf.scene).find((m) => [].concat(m.material).some((x) => x.name === 'c_Clukay_face_d'));
  assert.ok(face.geometry.attributes.position.count < 5000, `face primitive ${face.geometry.attributes.position.count} verts (hair copies dropped)`);
  if (!fs.existsSync(w1Path)) { t.diagnostic('no wave-1 dev build to compare clips'); return; }
  const w1 = (await load(w1Path)).gltf;
  for (const a of w1.animations) {
    const b = gltf.animations.find((x) => x.name === a.name);
    assert.ok(b && Math.abs(a.duration - b.duration) < 1e-9 && a.tracks.length === b.tracks.length, a.name);
    for (const tr of a.tracks) assert.deepEqual([...b.tracks.find((x) => x.name === tr.name).values], [...tr.values], `${a.name} ${tr.name}`);
  }
});
