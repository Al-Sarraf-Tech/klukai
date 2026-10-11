#!/usr/bin/env node
// GLB summary for viewer feature detection: node tools/avatar/debug/glbinfo.mjs assets/build/dev/klukai.glb
import { NodeIO } from '@gltf-transform/core';
import { ALL_EXTENSIONS } from '@gltf-transform/extensions';
import { MeshoptDecoder } from 'meshoptimizer';
await MeshoptDecoder.ready;
const io = new NodeIO().registerExtensions(ALL_EXTENSIONS).registerDependencies({ 'meshopt.decoder': MeshoptDecoder });
const doc = await io.read(process.argv[2]);
const r = doc.getRoot();
const sk = r.listSkins()[0];
console.log('joints', sk.listJoints().length, 'Head joint:', sk.listJoints().some((j) => j.getName() === 'mixamorigHead'), sk.listJoints().slice(50).map(j=>j.getName()).join(','));
for (const m of r.listMeshes()) for (const p of m.listPrimitives()) {
  const mat = p.getMaterial();
  console.log(m.getName(), p.getAttribute('POSITION').getCount(), mat?.getName(), JSON.stringify(mat?.getExtras()), 'attrs', p.listSemantics().join(','), 'ao', !!mat?.getOcclusionTexture(), 'mr', !!mat?.getMetallicRoughnessTexture(), 'nrm', !!mat?.getNormalTexture(), 'ds', mat?.getDoubleSided(), 'col', mat?.getBaseColorFactor()?.map(v=>+v.toFixed(2)).join(','), 'tex', !!mat?.getBaseColorTexture(), 'targets', p.listTargets().length);
}
for (const t of r.listTextures()) console.log('tex', t.getName(), t.getMimeType(), t.getSize()?.join('x'), t.getImage()?.byteLength);
