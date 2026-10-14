#!/usr/bin/env node
// Extract only the pinned CC0 source skeleton and selected animation channels.
// No Klukai model, texture, bind pose, or private source is read by this build.
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { NodeIO } from '@gltf-transform/core';
import { resample } from '@gltf-transform/functions';
import { MOTION_CATALOG } from '../../flutter_app/web/companion/motion-library.js';

export const SOURCE = Object.freeze({
  url: 'https://raw.githubusercontent.com/Mesh2Motion/mesh2motion-app/653a9698f5f315523072f1c3ff3496100401317b/static/animations/human-addon-animations.glb',
  sha256: 'a0d64d555e0d492026b72d58bf8e16c5e86779295f9093e376dcc001915c2c95',
  license: 'CC0-1.0',
  licenseUrl: 'https://github.com/Mesh2Motion/mesh2motion-app/blob/653a9698f5f315523072f1c3ff3496100401317b/LICENSE-CC0.MD',
});

export async function buildMotionPack(bytes) {
  const sha256 = createHash('sha256').update(bytes).digest('hex');
  if (sha256 !== SOURCE.sha256) throw new Error('Source hash does not match the reviewed CC0 animation pack');
  const doc = await new NodeIO().readBinary(new Uint8Array(bytes));
  const wanted = new Set(MOTION_CATALOG.map((c) => c.source));
  for (const a of doc.getRoot().listAnimations()) if (!wanted.has(a.getName())) a.dispose();
  for (const name of wanted) if (!doc.getRoot().listAnimations().some((a) => a.getName() === name)) throw new Error(`Missing clip: ${name}`);
  await doc.transform(resample({ tolerance: 0.0001 }));
  const needed = new Set();
  for (const skin of doc.getRoot().listSkins()) for (const joint of skin.listJoints()) for (let n = joint; n; n = n.getParentNode()) needed.add(n);
  const nodes = [], lookup = new Map();
  function walk(node, parent = -1) {
    if (!needed.has(node)) return;
    const index = nodes.length; lookup.set(node, index);
    nodes.push({ name: node.getName(), parent, position: node.getTranslation(), quaternion: node.getRotation(), scale: node.getScale() });
    for (const child of node.listChildren()) walk(child, index);
  }
  for (const scene of doc.getRoot().listScenes()) for (const child of scene.listChildren()) walk(child);
  const clips = doc.getRoot().listAnimations().map((animation) => {
    let duration = 0;
    const tracks = [];
    for (const channel of animation.listChannels()) {
      const sampler = channel.getSampler();
      const times = Array.from(sampler.getInput().getArray());
      duration = Math.max(duration, times.at(-1));
      const node = lookup.get(channel.getTargetNode());
      if (node === undefined) continue;
      const property = { rotation: 'quaternion', translation: 'position', scale: 'scale' }[channel.getTargetPath()];
      if (!property || !['LINEAR', 'STEP'].includes(sampler.getInterpolation())) throw new Error('Unreviewed source interpolation');
      const values = Array.from(sampler.getOutput().getArray());
      const rest = nodes[node][property];
      if (values.every((value, i) => Math.abs(value - rest[i % rest.length]) < 1e-7)) continue;
      tracks.push({ node, path: property, interpolation: sampler.getInterpolation(), times, values });
    }
    return { name: animation.getName(), duration, tracks };
  });
  return { version: 1, source: SOURCE, nodes, clips };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [input, output] = process.argv.slice(2);
  if (!input || !output || path.resolve(input) === path.resolve(output) || path.resolve(output).includes('/assets/build/')) throw new Error('Usage: build-motion-pack.mjs SOURCE.glb OUTPUT.json (never assets/build)');
  const pack = await buildMotionPack(fs.readFileSync(input));
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.writeFileSync(output, `${JSON.stringify(pack, (_key, value) => typeof value === 'number' ? Math.round(value * 1e6) / 1e6 : value)}\n`);
  console.log(JSON.stringify({ output, bytes: fs.statSync(output).size, nodes: pack.nodes.length, clips: pack.clips.map((c) => c.name) }));
}
