// Build configuration for the Klukai avatar GLB. Pure data + a validator so it
// can be unit-tested without the (uncommitted, personal-use) source assets.

/** Clip file → runtime clip name. Order = order in the GLB. */
export const CLIPS = Object.freeze([
  { file: 'Idle.fbx', name: 'idle', loop: true },
  { file: 'Talking.fbx', name: 'talking', loop: true },
  { file: 'Happy.fbx', name: 'happy', loop: true },
  { file: 'Bashful.fbx', name: 'bashful', loop: true },
  { file: 'Thinking.fbx', name: 'thinking', loop: true },
  { file: 'Thankful.fbx', name: 'thankful', loop: false },
  { file: 'Salute.fbx', name: 'salute', loop: false },
  { file: 'Excited.fbx', name: 'excited', loop: true },
  { file: 'Calling.fbx', name: 'calling', loop: false },
  { file: 'Defeated.fbx', name: 'defeated', loop: true },
  { file: 'Dismissing Gesture.fbx', name: 'dismiss', loop: false },
  { file: 'Nervously Look Around.fbx', name: 'nervous', loop: true },
  { file: 'Kneeling Down.fbx', name: 'kneel', loop: false },
  { file: 'Laying Sleeping.fbx', name: 'sleep', loop: true },
  { file: 'Female Standing Pose.fbx', name: 'stand', loop: true },
]);

export const REQUIRED_CLIPS = Object.freeze([
  'idle', 'talking', 'happy', 'bashful', 'thinking', 'thankful', 'salute', 'excited',
  'calling', 'defeated', 'dismiss', 'nervous', 'kneel', 'sleep', 'stand',
]);

/** "Dismissing Gesture.fbx" → "dismiss" (falls back to a slug for unknown files). */
export function clipNameForFile(file) {
  const hit = CLIPS.find((c) => c.file === file);
  if (hit) return hit.name;
  return file.replace(/\.fbx$/i, '').trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
}

/**
 * OBJ material (usemtl) → how it ships. The FBX from Mixamo lost material groups,
 * but its triangle order equals the OBJ face order, so groups are recovered from
 * the OBJ. `drop: true` removes a group from the build.
 *  - texture: file in assets/textures
 *  - role: drives viewer shading (skin | hair | face | eye | cloth | overlay)
 */
/** Default "Blazing Star" outfit atlases (SSR01). The cloth groups mix both. */
export const OUTFIT_ATLASES = Object.freeze(['c_ClukaySSR01_slg_cloth1_d.png', 'c_ClukaySSR01_slg_cloth2_d.png']);

export const MATERIALS = Object.freeze({
  // Bare shoulders/arms + hips. Hips sit a few mm under the jacket/shorts and
  // poke through when skinned: shrink them inward (never visible from outside).
  'Klukai_Body_(Default)': { texture: 'body_d.png', role: 'skin', maxSize: 512, shrink: { maxY: 1.05, amount: 0.005 } },
  'Klukai_Hair': { texture: 'c_Clukay_hair_d.png', role: 'hair', doubleSided: true },
  // Original face atlas. (c_Clukay_face_open_eyes.png from the April attempt
  // painted irises onto the cheeks — wrong UV spot; not used.)
  'Klukai_Face': { texture: 'c_Clukay_face_d.png', role: 'face' },
  // Eye highlight discs; rendered additively by the viewer.
  'Klukai_Eyeblend': { texture: 'c_Clukay_eyeblend.png', role: 'overlay' },
  // Mixed group: iris discs (eye atlas), brows / mouth interior (face atlas),
  // plus a face-shaped shadow shell that would hide the irises (dropped).
  'Klukai_Eyes': {
    atlases: ['c_Clukay_face_d.png', 'c_Clukay_eye_d.png'],
    atlasDefault: 'c_Clukay_face_d.png',
    role: 'face',
    atlasOverrides: [
      { box: [-0.05, 1.36, 0.03, -0.02, 1.40, 0.07], uvBox: [0, 0, 1, 1], minUvSpan: 0.9, atlas: 'c_Clukay_eye_d.png', why: 'right iris disc' },
      { box: [0.02, 1.36, 0.03, 0.05, 1.40, 0.07], uvBox: [0, 0, 1, 1], minUvSpan: 0.9, atlas: 'c_Clukay_eye_d.png', why: 'left iris disc' },
      { box: [-0.004, 1.355, 0.0, 0.004, 1.372, 0.06], atlas: null, why: 'face-shaped shadow shell over the eyes' },
    ],
  },
  'Eye_Shadow': { drop: true },
  // Near-duplicate layer of Cloth_1's collar/belt/pouch/legs (z-fights): dropped.
  'Klukai_Cloth_2': { drop: true },
  'Outline': { drop: true },
  'Klukai_Cloth_1': {
    atlases: OUTFIT_ATLASES,
    role: 'cloth',
    doubleSided: true,
    // Visually verified islands the smoothness heuristic gets wrong.
    atlasOverrides: [
      { uvBox: [0.63, 0.10, 0.89, 0.36], atlas: 'c_ClukaySSR01_slg_cloth1_d.png', why: 'cap crown (black panelled cap on cloth1)' },
    ],
  },
  'Cloth_1_(Speed_Star)': { drop: true },
  'Klukai_Cloth_1_(Astral_Luminous)': { drop: true },
  'Klukai_Cloth_3_(Astral_Luminous)': { drop: true },
  'Klukai_Cloth_4_(Astral_Luminous)': { drop: true },
  'Klukai_Leggings_(Astral_Luminous)': { drop: true },
  'Klukai_Body_Suit_(Astral_Luminous)': { drop: true },
  'Klukai_Fishnets_(Astral_Luminous)': { drop: true },
  'Klukai_Leggings_2_(Astral_Luminous)': { drop: true },
  // Leg skin fully under the stockings; it z-fights through them, so dropped.
  'Klukai_Body_(Astral_Luminous)': { drop: true },
});

export const ROLES = Object.freeze(['skin', 'hair', 'face', 'eye', 'cloth', 'overlay']);

/** Eye repair (see lib/eyes.mjs): groups that move with the eyelids. */
export const EYES = Object.freeze({
  openSide: -1,
  groups: ['Klukai_Face', 'Klukai_Eyes', 'Klukai_Eyeblend', 'Eye_Shadow', 'Outline'],
  // Lids/brows/lash overlays use mirrored UV halves. Iris + highlight discs
  // (full-square UV islands) are already symmetric and are skipped.
  // Eye-white UV regions of the face atlas (OBJ v-up); tucked behind the lid on blink.
  scleraUv: [[0.0, 0.0, 0.35, 0.25], [0.65, 0.0, 1.0, 0.25]],
  // Blink morph fades to zero between these heights (metres): lids move, brows don't.
  browFade: [1.393, 1.405],
  modes: { Klukai_Face: 'mirror', Klukai_Eyes: 'mirror', Klukai_Eyeblend: 'mirror', Eye_Shadow: 'mirror', Outline: 'mirror' },
});

export const CONFIG = Object.freeze({
  sourceDir: 'assets/source-models',
  textureDir: 'assets/textures',
  objFile: 'klukai_for_mixamo.obj',
  baseFbx: 'Idle.fbx',
  outFile: 'assets/build/klukai.glb',
  textureMaxSize: 1024,
  textureMaxSizeHard: 2048,
  webpQuality: 90,
  animationFps: 30,
  // ~0.1° rotation error: invisible, and ~40% smaller with meshopt filters.
  resampleTolerance: 1e-3,
  meshoptLevel: 'high',
  budgetBytes: 15 * 1024 * 1024,
  clips: CLIPS,
  materials: MATERIALS,
  eyes: EYES,
});

/** Returns a list of problems (empty = valid). */
export function validateConfig(cfg = CONFIG) {
  const errors = [];
  const names = cfg.clips.map((c) => c.name);
  for (const n of names) {
    if (n !== n.toLowerCase() || !/^[a-z][a-z0-9_]*$/.test(n)) errors.push(`clip name not lowercase slug: ${n}`);
  }
  const dupes = names.filter((n, i) => names.indexOf(n) !== i);
  if (dupes.length) errors.push(`duplicate clip names: ${dupes.join(', ')}`);
  for (const r of REQUIRED_CLIPS) if (!names.includes(r)) errors.push(`missing required clip: ${r}`);
  if (!cfg.clips.some((c) => c.file === cfg.baseFbx)) errors.push('baseFbx must also be a clip source');
  for (const c of cfg.clips) if (!/\.fbx$/i.test(c.file)) errors.push(`clip source is not .fbx: ${c.file}`);
  const kept = Object.entries(cfg.materials).filter(([, m]) => !m.drop);
  if (!kept.length) errors.push('no materials kept');
  for (const [name, m] of kept) {
    const imgs = m.atlases ? m.atlases : [m.texture];
    if (!imgs.length || imgs.some((t) => !t || !/\.(png|jpe?g|webp)$/i.test(t))) errors.push(`material ${name} has no image texture`);
    if (m.texture && m.atlases) errors.push(`material ${name} sets both texture and atlases`);
    if (!ROLES.includes(m.role)) errors.push(`material ${name} has unknown role ${m.role}`);
    if (m.maxSize && m.maxSize > cfg.textureMaxSizeHard) errors.push(`material ${name} maxSize over hard cap`);
    if (m.shrink && !(m.shrink.amount > 0 && m.shrink.amount < 0.02)) errors.push(`material ${name} shrink amount out of range`);
  }
  if (!kept.some(([, m]) => m.role === 'face')) errors.push('a face material is required');
  if (cfg.textureMaxSize > 1024) errors.push('textureMaxSize must be <= 1024');
  if (!(cfg.budgetBytes > 0 && cfg.budgetBytes <= 15 * 1024 * 1024)) errors.push('budget must be <= 15 MB');
  return errors;
}
