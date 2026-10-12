/** Inspect real converted assets: opaque helper shells must not cover the irises. */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const dir = process.env.AVATAR_TEST_VARIANTS_DIR || fileURLToPath(new URL('../../../assets/build/variants/', import.meta.url));
for (const id of ['klukai_garf', 'garf_dorm', 'garf_speed_star', 'garf_astral_luminous', 'garf_cerulean_breaker']) {
  const file = path.join(dir, `${id}.glb`);
  test(`${id}: green iris uses face shading, highlights blend, and transparent helper is absent`, { skip: !fs.existsSync(file) && !process.env.AVATAR_TEST_VARIANTS_DIR }, () => {
    const bytes = fs.readFileSync(file);
    const gltf = JSON.parse(bytes.subarray(20, 20 + bytes.readUInt32LE(12)).toString());
    const primitives = gltf.meshes.flatMap(m => m.primitives);
    const materials = primitives.map(p => gltf.materials[p.material]);
    const iris = materials.find(m => m.name === 'Klukai Eyes');
    const reflection = materials.find(m => m.name === 'Klukai Eyeblend');
    assert.ok(iris, 'authored iris geometry must remain');
    assert.equal(iris.extras?.role, 'face', 'use the working original eye shading contract');
    assert.ok(iris.pbrMetallicRoughness?.baseColorTexture, 'keep the green iris texture');
    assert.ok(reflection, 'preserve authored catchlights');
    assert.equal(reflection.extras?.role, 'overlay', 'black in the highlight texture must blend away');
    assert.ok(!materials.some(m => m.name === 'Eye Shadow'), 'source fully-transparent helper must not become opaque');
    const face = primitives.find(p => gltf.materials[p.material].name === 'Klukai Face');
    assert.ok(face.targets?.length, 'eyelid blink deformation must survive helper removal');
    assert.equal(gltf.animations.length, 15, 'keep all existing movement clips');
  });
}
