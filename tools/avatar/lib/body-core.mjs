// Close the source rip's absent neck and abdomen with an inner, skinned
// bodysuit. Rings are [bind y, half width, front z, back z], in metres.
// The closed surface shares the existing bodysuit material and draw call.
export function createBodyCore({ rings, joints, segments = 32 }) {
  const points = []; const triangles = [];
  for (const [y, rx, front, back] of rings) {
    for (let k = 0; k < segments; k++) {
      const a = k * Math.PI * 2 / segments;
      points.push([rx * Math.cos(a), y, (front + back) / 2 + (front - back) / 2 * Math.sin(a)]);
    }
  }
  for (let r = 0; r < rings.length - 1; r++) {
    for (let k = 0; k < segments; k++) {
      const next = (k + 1) % segments;
      const a = r * segments + k; const b = (r + 1) * segments + k;
      const c = r * segments + next; const d = (r + 1) * segments + next;
      triangles.push([a, b, c], [b, d, c]);
    }
  }
  for (const r of [0, rings.length - 1]) {
    const [y, , front, back] = rings[r]; const center = points.length;
    points.push([0, y, (front + back) / 2]);
    for (let k = 0; k < segments; k++) {
      const a = r * segments + k; const b = r * segments + (k + 1) % segments;
      triangles.push(r === 0 ? [center, a, b] : [center, b, a]);
    }
  }
  const normals = points.map(() => [0, 0, 0]);
  for (const tri of triangles) {
    const [p, q, r] = tri.map((i) => points[i]);
    const a = q.map((x, k) => x - p[k]); const b = r.map((x, k) => x - p[k]);
    const n = [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
    for (const i of tri) for (let k = 0; k < 3; k++) normals[i][k] += n[k];
  }
  for (const n of normals) { const len = Math.hypot(...n); for (let k = 0; k < 3; k++) n[k] /= len; }
  const count = triangles.length * 3;
  const out = { position: new Float32Array(count * 3), normal: new Float32Array(count * 3),
    uv: new Float32Array(count * 2), skinIndex: new Uint16Array(count * 4), skinWeight: new Float32Array(count * 4) };
  let v = 0;
  for (const tri of triangles) for (const i of tri) {
    const p = points[i];
    let j = 0;
    while (j < joints.length - 2 && p[1] > joints[j + 1].y) j++;
    const a = joints[j]; const b = joints[j + 1];
    const w = Math.max(0, Math.min(1, (p[1] - a.y) / (b.y - a.y)));
    out.position.set(p, v * 3); out.normal.set(normals[i], v * 3);
    out.skinIndex.set([a.index, b.index, 0, 0], v * 4);
    out.skinWeight.set([1 - w, w, 0, 0], v * 4);
    v++;
  }
  return out;
}
