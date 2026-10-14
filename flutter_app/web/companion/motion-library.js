// Source motions: Mesh2Motion's hand-keyed CC0 human addon, pinned in motions/.
// Only source-rig data is public. Target rest poses remain in this browser.
import { awaitWithAbort } from './appearance.js';
export const MOTION_CATALOG = Object.freeze([
  { id: 'wave', label: 'Greeting', source: 'Greeting', clip: 'motion_wave', category: 'Everyday' },
  { id: 'nod', label: 'Nod', source: 'Head Nod', clip: 'motion_nod', category: 'Everyday' },
  { id: 'confused', label: 'Confused', source: 'Confused', clip: 'motion_confused', category: 'Everyday' },
  { id: 'bow', label: 'Bow', source: 'Bow', clip: 'motion_bow', category: 'Everyday' },
  { id: 'dance_charleston', label: 'Charleston', source: 'Dance Charleston', clip: 'motion_dance_charleston', category: 'Playful', auto: false },
  { id: 'dance_groove', label: 'Groove', source: 'Dance Reach Hip', clip: 'motion_dance_groove', category: 'Playful', auto: false },
  { id: 'cheer', label: 'Cheer', source: 'Victory Fist Pump', clip: 'motion_cheer', category: 'Playful' },
  { id: 'listening', label: 'Listening', source: 'Idle Listening', clip: 'motion_listening', category: 'Everyday', passive: true },
].map(Object.freeze));

const boneMap = {
  Hips: 'pelvis', Spine: 'spine_01', Spine1: 'spine_02', Spine2: 'spine_03', Neck: 'neck_01', Head: 'head',
};
for (const [side, suffix] of [['Left', 'l'], ['Right', 'r']]) {
  for (const [target, source] of Object.entries({ Shoulder: 'clavicle', Arm: 'upperarm', ForeArm: 'lowerarm', Hand: 'hand', UpLeg: 'thigh', Leg: 'calf', Foot: 'foot', ToeBase: 'ball' })) {
    boneMap[side + target] = `${source}_${suffix}`;
  }
  for (const [finger, source] of Object.entries({ Thumb: 'thumb', Index: 'index', Middle: 'middle', Ring: 'ring', Pinky: 'pinky' })) {
    for (let n = 1; n <= 3; n++) boneMap[`${side}Hand${finger}${n}`] = `${source}_0${n}_${suffix}`;
  }
}

function validatePack(pack) {
  if (pack?.version !== 1 || !Array.isArray(pack.nodes) || !Array.isArray(pack.clips) || pack.nodes.length > 128 || pack.clips.length > 20) throw new Error('Invalid motion pack');
  const names = new Set();
  for (let i = 0; i < pack.nodes.length; i++) {
    const n = pack.nodes[i];
    if (!n || !/^[A-Za-z0-9_]+$/.test(n.name) || names.has(n.name) || !Number.isInteger(n.parent) || n.parent < -1 || n.parent >= i) throw new Error('Invalid motion pack hierarchy');
    names.add(n.name);
    for (const [key, size] of [['position', 3], ['quaternion', 4], ['scale', 3]]) {
      if (!Array.isArray(n[key]) || n[key].length !== size || !n[key].every(Number.isFinite)) throw new Error('Invalid motion pack transform');
    }
  }
  for (const c of pack.clips) {
    if (!Number.isFinite(c.duration) || c.duration <= 0 || c.duration > 20 || !Array.isArray(c.tracks) || c.tracks.length > 384) throw new Error('Invalid motion pack clip');
    for (const t of c.tracks) {
      const size = t.path === 'quaternion' ? 4 : ['position', 'scale'].includes(t.path) ? 3 : 0;
      if (!Number.isInteger(t.node) || !pack.nodes[t.node] || !size || ![undefined, 'LINEAR', 'STEP'].includes(t.interpolation) || !Array.isArray(t.times) || !Array.isArray(t.values) || !t.times.length || t.times.length > 2000 || t.values.length !== t.times.length * size || !t.times.every((v, i) => Number.isFinite(v) && v >= 0 && v <= c.duration && (!i || v > t.times[i - 1])) || !t.values.every(Number.isFinite)) throw new Error('Invalid motion pack track');
    }
  }
}

/** Bake CC0 source motion onto a stripped copy of the current appearance's rig.
 * The source's world-space rotation delta is applied to the target rest pose;
 * converting back through each target parent keeps different bone axes valid.
 * No geometry, materials, morphs, live bone transforms or private data are copied
 * into the distributable source pack. */
export function retargetMotionPack({ THREE, root, pack, fps = 30 }) {
  validatePack(pack);
  if (!root?.getObjectByName('mixamorigHips') || !(fps >= 10 && fps <= 60)) throw new Error('Unsupported motion pack target');
  const sourceRoot = new THREE.Object3D();
  const sourceNodes = pack.nodes.map((n) => {
    const o = new THREE.Object3D(); o.name = n.name;
    o.position.fromArray(n.position); o.quaternion.fromArray(n.quaternion); o.scale.fromArray(n.scale);
    return o;
  });
  pack.nodes.forEach((n, i) => (n.parent < 0 ? sourceRoot : sourceNodes[n.parent]).add(sourceNodes[i]));
  sourceRoot.updateMatrixWorld(true);
  const sources = new Map(sourceNodes.map((n) => [n.name, n]));
  const sourceRest = new Map(sourceNodes.map((n) => [n.name, { q: n.getWorldQuaternion(new THREE.Quaternion()), p: n.getWorldPosition(new THREE.Vector3()) }]));

  // Keep only canonical bones and the ancestors needed to evaluate their pose.
  const needed = new Set();
  root.traverse((n) => { if (boneMap[n.name.replace(/^mixamorig/, '')] && n.name.startsWith('mixamorig')) for (let p = n; p; p = p.parent) { needed.add(p); if (p === root) break; } });
  const targetNodes = [];
  function copyHierarchy(node) {
    const o = new THREE.Object3D(); o.name = node.name;
    o.position.copy(node.position); o.quaternion.copy(node.quaternion); o.scale.copy(node.scale);
    targetNodes.push(o);
    for (const child of node.children) if (needed.has(child)) o.add(copyHierarchy(child));
    return o;
  }
  const targetRoot = copyHierarchy(root); targetRoot.updateMatrixWorld(true);
  const mapped = targetNodes.flatMap((target) => {
    const sourceName = boneMap[target.name.replace(/^mixamorig/, '')];
    const source = target.name.startsWith('mixamorig') && sources.get(sourceName);
    return source ? [{ target, source, sourceRest: sourceRest.get(sourceName), restQ: target.getWorldQuaternion(new THREE.Quaternion()) }] : [];
  });
  const hips = mapped.find((m) => m.target.name === 'mixamorigHips');
  if (!hips || mapped.length < 2) throw new Error('Unsupported motion pack skeleton');
  const targetHipRest = hips.target.getWorldPosition(new THREE.Vector3());
  const hipScale = Math.max(0.25, Math.min(4, targetHipRest.y / Math.max(0.01, hips.sourceRest.p.y)));
  const feet = mapped.filter((m) => /(?:Left|Right)(?:Foot|ToeBase)$/.test(m.target.name));
  const position = new THREE.Vector3();
  const minFoot = (side) => Math.min(...feet.map((m) => m[side].getWorldPosition(position).y));
  const sourceFloor = feet.length ? minFoot('source') : 0;
  const targetFloor = feet.length ? minFoot('target') : 0;
  const mixer = new THREE.AnimationMixer(sourceRoot);
  const q = new THREE.Quaternion(), parentQ = new THREE.Quaternion(), p = new THREE.Vector3();
  const clips = [], available = [];
  for (const spec of MOTION_CATALOG) {
    const raw = pack.clips.find((c) => c.name === spec.source);
    if (!raw) continue;
    const sourceClip = new THREE.AnimationClip(raw.name, raw.duration, raw.tracks.map((t) => {
      const Track = t.path === 'quaternion' ? THREE.QuaternionKeyframeTrack : THREE.VectorKeyframeTrack;
      return new Track(`${pack.nodes[t.node].name}.${t.path}`, t.times, t.values, t.interpolation === 'STEP' ? THREE.InterpolateDiscrete : THREE.InterpolateLinear);
    }));
    const action = mixer.clipAction(sourceClip).setLoop(THREE.LoopOnce, 1); action.clampWhenFinished = true; action.play();
    const frames = Math.ceil(raw.duration * fps) + 1;
    const times = Float32Array.from({ length: frames }, (_, i) => Math.min(raw.duration, i / fps));
    const rotations = mapped.map(() => new Float32Array(frames * 4));
    const positions = new Float32Array(frames * 3);
    for (let frame = 0; frame < frames; frame++) {
      mixer.setTime(times[frame]); sourceRoot.updateMatrixWorld(true);
      for (let i = 0; i < mapped.length; i++) {
        const m = mapped[i];
        m.source.getWorldQuaternion(q).multiply(m.sourceRest.q.clone().invert()).multiply(m.restQ);
        m.target.parent?.getWorldQuaternion(parentQ);
        if (m.target.parent) q.premultiply(parentQ.invert());
        m.target.quaternion.copy(q).normalize(); m.target.updateMatrixWorld(true);
        m.target.quaternion.toArray(rotations[i], frame * 4);
      }
      hips.source.getWorldPosition(p);
      const limit = 0.12;
      p.set(targetHipRest.x + Math.max(-limit, Math.min(limit, (p.x - hips.sourceRest.p.x) * hipScale)),
        targetHipRest.y + (p.y - hips.sourceRest.p.y) * hipScale,
        targetHipRest.z + Math.max(-limit, Math.min(limit, (p.z - hips.sourceRest.p.z) * hipScale)));
      hips.target.parent?.worldToLocal(p); hips.target.position.copy(p); hips.target.updateMatrixWorld(true);
      // Different leg lengths and ankle offsets otherwise put dance feet below
      // the floor. Match the source's lowest contact to this model's rest floor,
      // retaining authored lifts when both feet leave the ground.
      if (feet.length) {
        const contactY = targetFloor + Math.max(0, minFoot('source') - sourceFloor) * hipScale;
        const adjustment = contactY - minFoot('target');
        hips.target.getWorldPosition(p); p.y += adjustment;
        hips.target.parent?.worldToLocal(p); hips.target.position.copy(p); hips.target.updateMatrixWorld(true);
      }
      p.toArray(positions, frame * 3);
    }
    action.stop(); mixer.uncacheClip(sourceClip);
    clips.push(new THREE.AnimationClip(spec.clip, raw.duration, [
      ...mapped.map((m, i) => new THREE.QuaternionKeyframeTrack(`${m.target.name}.quaternion`, times, rotations[i])),
      new THREE.VectorKeyframeTrack('mixamorigHips.position', times, positions),
    ]));
    available.push(spec);
  }
  return { clips, available };
}

let packPromise;
const targetCache = new WeakMap();
export async function loadMotionClips({ THREE, root, signal } = {}) {
  if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
  if (!packPromise) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    packPromise = fetch(new URL('./motions/everyday-playful.json?v=04911b8d948b', import.meta.url), { cache: 'force-cache', signal: controller.signal })
      .then((r) => { if (!r.ok) throw new Error(`Motion pack: ${r.status}`); return r.json(); })
      .then((pack) => { validatePack(pack); return pack; })
      .catch((e) => { packPromise = null; throw e; })
      .finally(() => clearTimeout(timeout));
  }
  // A superseded appearance stops waiting without cancelling a shared download.
  const pack = await awaitWithAbort(packPromise, signal);
  if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
  if (!targetCache.has(root)) targetCache.set(root, retargetMotionPack({ THREE, root, pack }));
  return targetCache.get(root);
}
