import { test } from 'node:test';
import assert from 'node:assert/strict';

import { insetFaces } from '../lib/inset-faces.mjs';
import { CONFIG, OPEN_JACKET, validateConfig } from '../avatar.config.mjs';

const close = (a, b, eps = 1e-6, msg = '') => assert.ok(Math.abs(a - b) <= eps, `${msg} ${a} ≉ ${b}`);

test('insetFaces keeps a shell but recesses selected corners along averaged normals', () => {
  const positions = Float32Array.from([
    0, 0, 0, 1, 0, 0, 1, 1, 0,
    0, 0, 0, 1, 1, 0, 0, 1, 0,
    2, 0, 0, 3, 0, 0, 2, 1, 0,
  ]);
  const normals = Float32Array.from(Array(9).fill([0, 0, 1]).flat());
  const faceV = Int32Array.from([0, 1, 2, 0, 2, 3, 4, 5, 6]);

  const touched = insetFaces({ positions, normals, faceV, faces: [0, 1], amount: 0.01 });

  assert.equal(touched, 4);
  for (let c = 0; c < 6; c++) close(positions[c * 3 + 2], -0.01, 1e-7, `selected corner ${c}`);
  for (let c = 6; c < 9; c++) close(positions[c * 3 + 2], 0, 1e-7, `unselected corner ${c}`);
});

test('open jacket keeps lower torso geometry and recesses it under the jacket', () => {
  assert.deepEqual(validateConfig(CONFIG), []);

  const lowerTorso = OPEN_JACKET.atlasOverrides.filter((o) => /bodysuit lower (front|back)/.test(o.why));
  assert.equal(lowerTorso.length, 2, 'front and back lower-torso islands are identified');
  assert.ok(lowerTorso.every((o) => o.atlas?.color === '#1b1b20'), 'lower torso remains black bodysuit geometry');
  assert.ok(lowerTorso.every((o) => o.inset > 0 && o.inset < 0.02), 'lower torso is recessed under the jacket instead of dropped');
});
