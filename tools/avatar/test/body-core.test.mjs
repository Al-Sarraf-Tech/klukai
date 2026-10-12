import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createBodyCore } from '../lib/body-core.mjs';

test('body core is closed, faces outward, and blends only the surrounding spine joints', () => {
  const g = createBodyCore({
    rings: [[0, 1, 1, -1], [1, 1, 1, -1], [2, 1, 1, -1]], segments: 8,
    joints: [{ index: 3, y: 0 }, { index: 7, y: 2 }],
  });
  const edges = new Map();
  const point = (i) => Array.from(g.position.slice(i * 3, i * 3 + 3));
  const key = (p) => p.map((x) => x.toFixed(5)).join(',');
  let middle = 0;
  for (let i = 0; i < g.position.length / 3; i++) {
    const p = point(i); const n = g.normal.slice(i * 3, i * 3 + 3);
    assert.ok(Math.abs(Math.hypot(...n) - 1) < 1e-6);
    const w = g.skinWeight.slice(i * 4, i * 4 + 4);
    assert.ok(Math.abs(w.reduce((a, b) => a + b, 0) - 1) < 1e-6);
    if (p[1] === 1) {
      middle++;
      assert.deepEqual(Array.from(g.skinIndex.slice(i * 4, i * 4 + 2)), [3, 7]);
      assert.deepEqual(Array.from(w), [0.5, 0.5, 0, 0]);
      assert.ok(p[0] * n[0] + p[2] * n[2] > 0.9);
    }
  }
  assert.ok(middle > 0);
  for (let i = 0; i < g.position.length / 3; i += 3) {
    const p = [point(i), point(i + 1), point(i + 2)];
    const a = p[1].map((x, k) => x - p[0][k]); const b = p[2].map((x, k) => x - p[0][k]);
    const n = [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
    const c = [0, 1, 2].map((k) => (p[0][k] + p[1][k] + p[2][k]) / 3);
    assert.ok(n[0]*c[0]+n[1]*(c[1]-1)+n[2]*c[2] > 0, 'outward winding, including caps');
    for (let k = 0; k < 3; k++) {
      const edge = [key(p[k]), key(p[(k + 1) % 3])].sort().join('|');
      edges.set(edge, (edges.get(edge) || 0) + 1);
    }
  }
  assert.ok([...edges.values()].every((n) => n === 2), 'every edge has two faces: no holes');
});
