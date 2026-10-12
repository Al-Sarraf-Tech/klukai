// Geometry helper for keeping hidden body/clothing shells closed while
// recessing them beneath an outer layer. Pure and asset-free so it can be
// unit-tested without the private source model.

/**
 * Recess selected non-indexed FBX face corners along averaged normals.
 *
 * Normals are averaged per OBJ vertex so UV/normal splits inside the selected
 * island move together and do not open cracks. Returns the number of unique OBJ
 * vertices moved.
 */
export function insetFaces({ positions, normals, faceV, faces, amount }) {
  if (!(amount > 0)) return 0;

  const acc = new Map();
  for (const f of faces) {
    for (let k = 0; k < 3; k++) {
      const c = f * 3 + k;
      const v = faceV[c];
      const a = acc.get(v) || [0, 0, 0, []];
      a[0] += normals[c * 3];
      a[1] += normals[c * 3 + 1];
      a[2] += normals[c * 3 + 2];
      a[3].push(c);
      acc.set(v, a);
    }
  }

  for (const [, [nx, ny, nz, corners]] of acc) {
    const len = Math.hypot(nx, ny, nz) || 1;
    for (const c of corners) {
      positions[c * 3] -= (nx / len) * amount;
      positions[c * 3 + 1] -= (ny / len) * amount;
      positions[c * 3 + 2] -= (nz / len) * amount;
    }
  }

  return acc.size;
}
