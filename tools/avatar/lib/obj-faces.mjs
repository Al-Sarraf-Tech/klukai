// Reads an OBJ (triangles only) into flat arrays: per-face material index,
// position indices and UV indices. Enough to recover material groups and UV
// islands for the Mixamo FBX, whose triangle order matches the OBJ face order.
import fs from 'node:fs';

export function readObjFaces(file) {
  const text = fs.readFileSync(file, 'utf8');
  const V = [];
  const VT = [];
  const materials = [];
  const faceMat = [];
  const faceV = [];
  const faceT = [];
  let cur = -1;
  let start = 0;
  while (start < text.length) {
    let end = text.indexOf('\n', start);
    if (end === -1) end = text.length;
    const line = text.slice(start, end);
    start = end + 1;
    if (line.startsWith('v ')) {
      const p = line.split(/\s+/);
      V.push(+p[1], +p[2], +p[3]);
    } else if (line.startsWith('vt ')) {
      const p = line.split(/\s+/);
      VT.push(+p[1], +p[2]);
    } else if (line.startsWith('usemtl ')) {
      const name = line.slice(7).trim();
      cur = materials.indexOf(name);
      if (cur === -1) { materials.push(name); cur = materials.length - 1; }
    } else if (line.startsWith('f ')) {
      const p = line.trim().split(/\s+/).slice(1);
      if (p.length !== 3) throw new Error(`non-triangle face in OBJ (${p.length} verts)`);
      faceMat.push(cur);
      for (const q of p) {
        const [v, t] = q.split('/');
        faceV.push(parseInt(v, 10) - 1);
        faceT.push(t ? parseInt(t, 10) - 1 : -1);
      }
    }
  }
  return {
    positions: Float32Array.from(V),
    uvs: Float32Array.from(VT),
    materials,
    faceMat: Int32Array.from(faceMat),
    faceV: Int32Array.from(faceV),
    faceT: Int32Array.from(faceT),
  };
}
