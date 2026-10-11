// Some ripped sub-meshes are inside-out (winding and normals both point into the
// body: the bodysuit's shorts and high collar, drawn two-sided in the game).
// Single-sided, the front wall culls away and only the far inner wall shows.
// Turning such triangles right side out = swap two corners + negate normals.
// Pure typed-array helpers (non-indexed geometry, 3 corners per triangle).

/**
 * Flips triangle `tri` in place.
 * @param attrs   [{ array, itemSize }] every per-corner attribute (incl. morph deltas)
 * @param normals { array } (itemSize 3), negated after the swap (null to skip)
 */
export function flipTriangle(attrs, normals, tri) {
  for (const { array, itemSize: n } of attrs) {
    const b = (tri * 3 + 1) * n;
    const c = (tri * 3 + 2) * n;
    for (let i = 0; i < n; i++) { const x = array[b + i]; array[b + i] = array[c + i]; array[c + i] = x; }
  }
  if (normals) for (let i = tri * 9; i < tri * 9 + 9; i++) normals.array[i] = -normals.array[i];
}
