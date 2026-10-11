// GFL2 ships `_rmo` maps: R = roughness, G = metal mask, B = baked occlusion.
// glTF wants one ORM texture: R = occlusion, G = roughness, B = metallic (the
// same image serves occlusionTexture and metallicRoughnessTexture). The viewer
// is toon-shaded and reads the maps directly (three: aoMap = R, metalnessMap = B).
//
// `sharp` is imported lazily (see atlas-assign.mjs) so the pure repack is
// unit-tested without native modules or assets.

/** Source channel of each glTF ORM slot in a GFL2 `_rmo` image. */
export const RMO_LAYOUT = Object.freeze({ occlusion: 2, roughness: 0, metallic: 1 });

/**
 * Reorders interleaved 8-bit pixels into ORM (3 channels).
 * @param data      Uint8Array/Buffer of `channels`-interleaved pixels
 * @param channels  3 or 4
 * @param layout    { occlusion, roughness, metallic } → source channel index
 */
export function repackOrm(data, channels, layout = RMO_LAYOUT) {
  if (!(channels === 3 || channels === 4)) throw new Error(`repackOrm: ${channels} channels`);
  for (const k of ['occlusion', 'roughness', 'metallic']) {
    if (!(layout[k] >= 0 && layout[k] < channels)) throw new Error(`repackOrm: bad source channel for ${k}`);
  }
  const n = Math.floor(data.length / channels);
  const out = new Uint8Array(n * 3);
  const { occlusion: o, roughness: r, metallic: m } = layout;
  for (let i = 0; i < n; i++) {
    const s = i * channels;
    out[i * 3] = data[s + o];
    out[i * 3 + 1] = data[s + r];
    out[i * 3 + 2] = data[s + m];
  }
  return out;
}

/**
 * Reads an `_rmo` PNG, resizes it to fit `maxSize` and encodes ORM as lossless
 * webp (lossy webp's chroma subsampling would smear the packed channels into
 * each other) or PNG.
 * @returns { data: Uint8Array, width, height, mime }
 */
export async function ormImage(file, { maxSize = 512, format = 'webp', layout = RMO_LAYOUT } = {}) {
  const { default: sharp } = await import('sharp');
  const { data, info } = await sharp(file)
    .resize({ width: maxSize, height: maxSize, fit: 'inside', withoutEnlargement: true })
    .raw()
    .toBuffer({ resolveWithObject: true });
  const orm = repackOrm(data, info.channels, layout);
  const img = sharp(Buffer.from(orm.buffer, orm.byteOffset, orm.byteLength), { raw: { width: info.width, height: info.height, channels: 3 } });
  const out = format === 'webp' ? await img.webp({ lossless: true, effort: 6 }).toBuffer() : await img.png().toBuffer();
  return { data: new Uint8Array(out), width: info.width, height: info.height, mime: format === 'webp' ? 'image/webp' : 'image/png' };
}

/** GPU bytes for an RGBA8 texture with a full mip chain (≈ 4/3 of the base level). */
export function textureGpuBytes(width, height) {
  let bytes = 0;
  for (let w = width, h = height; ; w = Math.max(1, w >> 1), h = Math.max(1, h >> 1)) {
    bytes += w * h * 4;
    if (w === 1 && h === 1) break;
  }
  return bytes;
}
