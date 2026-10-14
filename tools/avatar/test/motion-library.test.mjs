import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as THREE from 'three';
import fs from 'node:fs';
import { buildMotionPack, SOURCE } from '../build-motion-pack.mjs';

const motion = await import('../../../flutter_app/web/companion/motion-library.js').catch(() => ({}));

test('retargeting preserves the live rig and converts source rest offsets and hip scale', () => {
  assert.equal(typeof motion.retargetMotionPack, 'function');
  const root = new THREE.Group();
  const hips = new THREE.Bone(); hips.name = 'mixamorigHips'; hips.position.y = 2; root.add(hips);
  const arm = new THREE.Bone(); arm.name = 'mixamorigRightArm'; arm.position.x = -2;
  arm.quaternion.setFromAxisAngle(new THREE.Vector3(0, 0, 1), Math.PI / 4); hips.add(arm);
  root.updateMatrixWorld(true);
  const original = root.toJSON();
  const pack = { version: 1, nodes: [
    { name: 'pelvis', parent: -1, position: [0, 1, 0], quaternion: [0, 0, 0, 1], scale: [1, 1, 1] },
    { name: 'upperarm_r', parent: 0, position: [-1, 0, 0], quaternion: [0, 0, 0, 1], scale: [1, 1, 1] },
  ], clips: [{ name: 'Head Nod', duration: 1, tracks: [
    { node: 0, path: 'position', times: [0, 1], values: [0, 1, 0, 0, 1.1, 0] },
    { node: 1, path: 'quaternion', times: [0, 1], values: [0, 0, 0, 1, 0, 0, Math.SQRT1_2, Math.SQRT1_2] },
  ] }] };
  const { clips, available } = motion.retargetMotionPack({ THREE, root, pack, fps: 30 });
  const unsupported = structuredClone(pack);
  unsupported.clips[0].tracks[0].interpolation = 'CUBICSPLINE';
  assert.throws(() => motion.retargetMotionPack({ THREE, root, pack: unsupported }), /Invalid motion pack track/);
  assert.deepEqual(root.toJSON(), original, 'baking must never animate or alter the live target');
  assert.equal(clips.length, 1); assert.equal(available[0].id, 'nod');
  const mixer = new THREE.AnimationMixer(root);
  const a = mixer.clipAction(clips[0]).setLoop(THREE.LoopOnce, 1); a.clampWhenFinished = true; a.play();
  mixer.update(1); root.updateMatrixWorld(true);
  assert.ok(Math.abs(hips.position.y - 2.2) < 1e-5, 'vertical hip displacement scales to target proportions');
  assert.equal(arm.position.x, -2, 'target bone lengths survive');
  const expected = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 0, 1), Math.PI * 0.75);
  assert.ok(arm.quaternion.clone().normalize().angleTo(expected) < 1e-5, 'motion is relative to target rest orientation');
});

test('invalid source data and unsupported targets fail without touching the target', () => {
  assert.equal(typeof motion.retargetMotionPack, 'function');
  const root = new THREE.Group();
  assert.throws(() => motion.retargetMotionPack({ THREE, root, pack: { version: 900 } }), /motion pack/i);
  assert.equal(root.children.length, 0);
});

const reviewedSource = '/tmp/klukai-animation-sources/human-addon-animations.glb';
test('the reviewed CC0 pack preserves discrete channels and contains source motion only', { skip: !fs.existsSync(reviewedSource) }, async () => {
  const pack = await buildMotionPack(fs.readFileSync(reviewedSource));
  assert.equal(pack.source.sha256, SOURCE.sha256);
  assert.equal(pack.clips.length, 8);
  assert.ok(pack.clips.some((c) => c.tracks.some((t) => t.interpolation === 'STEP')));
  assert.equal(pack.nodes.some((n) => n.name.startsWith('mixamorig')), false);
  assert.equal('meshes' in pack, false); assert.equal('textures' in pack, false);
});

const modelPath = process.env.AVATAR_TEST_GLB;
test('real default and variant motions keep foot contacts above their rest floor', { skip: !modelPath || !fs.existsSync(modelPath) }, async () => {
  const { GLTFLoader } = await import('three/examples/jsm/loaders/GLTFLoader.js');
  const { MeshoptDecoder } = await import('three/examples/jsm/libs/meshopt_decoder.module.js');
  const pack = JSON.parse(fs.readFileSync(new URL('../../../flutter_app/web/companion/motions/everyday-playful.json', import.meta.url)));
  const original = THREE.TextureLoader.prototype.load;
  THREE.TextureLoader.prototype.load = function (url, onLoad) { const t = new THREE.Texture(); setTimeout(() => onLoad?.(t), 0); return t; };
  globalThis.self ??= globalThis;
  try {
    const variant = modelPath.replace(/klukai\.glb$/, 'variants/klukai_garf.glb');
    assert.ok(fs.existsSync(variant), 'the real GARF fixture is required for alternate bind-pose coverage');
    for (const file of [modelPath, variant]) {
      const bytes = fs.readFileSync(file);
      const { scene: root } = await new GLTFLoader().setMeshoptDecoder(MeshoptDecoder).parseAsync(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength), '');
      root.updateMatrixWorld(true);
      const feet = ['LeftFoot', 'RightFoot', 'LeftToeBase', 'RightToeBase'].map((n) => root.getObjectByName(`mixamorig${n}`));
      const minY = () => Math.min(...feet.map((f) => f.getWorldPosition(new THREE.Vector3()).y));
      const floor = minY();
      const { clips } = motion.retargetMotionPack({ THREE, root, pack });
      assert.equal(clips.length, 8);
      for (const clip of clips) {
        const mixer = new THREE.AnimationMixer(root), action = mixer.clipAction(clip).setLoop(THREE.LoopOnce, 1);
        action.clampWhenFinished = true; action.play();
        for (let t = 0; t <= clip.duration; t += 1 / 30) {
          mixer.setTime(t); root.updateMatrixWorld(true);
          assert.ok(minY() >= floor - 0.001, `${file} ${clip.name}: feet ${minY()} below ${floor} at ${t}`);
        }
        action.stop(); mixer.uncacheClip(clip);
      }
    }
  } finally { THREE.TextureLoader.prototype.load = original; }
});

test('a superseded model stops waiting for shared source loading without cancelling another caller', async () => {
  const originalFetch = globalThis.fetch;
  let resolveFetch, calls = 0;
  globalThis.fetch = () => { calls++; return new Promise((resolve) => { resolveFetch = resolve; }); };
  try {
    const loader = await import('../../../flutter_app/web/companion/motion-library.js?abort-test');
    const controller = new AbortController();
    const first = loader.loadMotionClips({ THREE, root: new THREE.Group(), signal: controller.signal });
    const second = loader.loadMotionClips({ THREE, root: new THREE.Group() });
    controller.abort();
    await assert.rejects(first, { name: 'AbortError' });
    assert.equal(calls, 1);
    resolveFetch({ ok: true, json: async () => ({ version: -1 }) });
    await assert.rejects(second, /Invalid motion pack/);
    globalThis.fetch = async () => { calls++; return { ok: false, status: 503 }; };
    await assert.rejects(loader.loadMotionClips({ THREE, root: new THREE.Group() }), /503/);
    assert.equal(calls, 2, 'a failed download is retried for a later appearance');
  } finally { globalThis.fetch = originalFetch; }
});
