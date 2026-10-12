// Retarget the pinned avatar's motion onto an offline, canonical-name variant.
// Usage: node tools/avatar/retarget-variant.mjs SOURCE.glb TARGET_RAW.glb OUTPUT.glb
// All three paths are explicit; the pinned source is never overwritten.
import fs from 'node:fs';
import path from 'node:path';
import { NodeIO } from '@gltf-transform/core';
import { ALL_EXTENSIONS, EXTTextureWebP } from '@gltf-transform/extensions';
import { dedup, prune, resample, meshopt, join } from '@gltf-transform/functions';
import { MeshoptEncoder, MeshoptDecoder } from 'meshoptimizer';
import { Quaternion } from 'three';
import sharp from 'sharp';

const [sourceFile, targetFile, outFile] = process.argv.slice(2);
const resolved = file => fs.existsSync(file) ? fs.realpathSync(file) : path.resolve(file);
if (!sourceFile || !targetFile || !outFile || path.basename(resolved(outFile)) === 'klukai.glb' || [sourceFile, targetFile].some(file => resolved(file) === resolved(outFile))) throw Error('Explicit variant output required; refusing input/default overwrite');
await Promise.all([MeshoptEncoder.ready, MeshoptDecoder.ready]);
const io = new NodeIO().registerExtensions(ALL_EXTENSIONS).registerDependencies({'meshopt.decoder':MeshoptDecoder,'meshopt.encoder':MeshoptEncoder});
const source = await io.read(sourceFile), target = await io.read(targetFile);
const targets = new Map(target.getRoot().listNodes().map(n=>[n.getName(),n]));
for (const name of ['Hips','Head','Neck','LeftArm','RightArm','LeftFoot','RightFoot']) if (!targets.has('mixamorig'+name)) throw Error('Missing canonical bone '+name);
const buffer = target.getRoot().listBuffers()[0] || target.createBuffer();
const quat = a => new Quaternion().fromArray(a);
let channels = 0;
for (const original of source.getRoot().listAnimations()) {
 const anim = target.createAnimation(original.getName()).setExtras(original.getExtras());
 for (const ch of original.listChannels()) {
  const s = ch.getTargetNode(), t = targets.get(s?.getName()), kind = ch.getTargetPath();
  if (!t || !s.getName().startsWith('mixamorig') || !['rotation','translation'].includes(kind)) continue;
  if (kind === 'translation' && s.getName() !== 'mixamorigHips') continue;
  const old = ch.getSampler(), values = new Float32Array(old.getOutput().getArray());
  if (old.getInterpolation() === 'CUBICSPLINE') throw Error('Resample cubic clips before retargeting');
  if (kind === 'rotation') {
   const sourceWorld=quat(s.getWorldRotation()), targetWorld=quat(t.getWorldRotation());
   const correction=targetWorld.clone().invert().multiply(sourceWorld);
   const left=quat(t.getRotation()).multiply(correction).multiply(quat(s.getRotation()).invert());
   const right=correction.clone().invert();
   for(let i=0;i<values.length;i+=4) left.clone().multiply(new Quaternion().fromArray(values,i)).multiply(right).normalize().toArray(values,i);
  } else {
   const sr=s.getTranslation(), tr=t.getTranslation();
   for(let i=0;i<values.length;i++) values[i]+=tr[i%3]-sr[i%3];
  }
  const input=target.createAccessor().setType('SCALAR').setArray(new Float32Array(old.getInput().getArray())).setBuffer(buffer);
  const output=target.createAccessor().setType(kind==='rotation'?'VEC4':'VEC3').setArray(values).setBuffer(buffer);
  const sampler=target.createAnimationSampler().setInput(input).setOutput(output).setInterpolation(old.getInterpolation());
  anim.addSampler(sampler).addChannel(target.createAnimationChannel().setTargetNode(t).setTargetPath(kind).setSampler(sampler));channels++;
 }
}
if (!channels) throw Error('No motion retargeted');
for(const tex of target.getRoot().listTextures()) {
 const bytes=tex.getImage();if(!bytes)continue;
 tex.setImage(new Uint8Array(await sharp(Buffer.from(bytes)).resize({width:1024,height:1024,fit:'inside',withoutEnlargement:true}).webp({quality:92,alphaQuality:100}).toBuffer())).setMimeType('image/webp');
}
target.createExtension(EXTTextureWebP).setRequired(true);
await target.transform(dedup(),join({keepNamed:false}),resample({tolerance:0.0001}),prune(),meshopt({encoder:MeshoptEncoder,level:'medium',quantizationVolume:'scene'}));
target.getRoot().getAsset().generator='Klukai private avatar conversion; personal noncommercial use';
fs.mkdirSync(path.dirname(outFile),{recursive:true});const bytes=await io.writeBinary(target);fs.writeFileSync(outFile,bytes);
console.log(JSON.stringify({output:outFile,bytes:bytes.length,clips:target.getRoot().listAnimations().map(a=>a.getName()),channels}));
