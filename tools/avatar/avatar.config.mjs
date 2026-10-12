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
 *  - texture: file in assets/textures (or `atlases` for groups that mix several)
 *  - role: drives viewer shading (skin | hair | face | eye | cloth | overlay)
 *  - maxSize: texture cap for this material (default textureMaxSize, ≤ textureMaxSizeHard)
 *  - atlasOverrides: per-island fixes (see below)
 */
/** Default "Blazing Star" outfit atlases (SSR01). The cloth groups mix both. */
export const OUTFIT_ATLASES = Object.freeze(['c_ClukaySSR01_slg_cloth1_d.png', 'c_ClukaySSR01_slg_cloth2_d.png']);
const C1 = OUTFIT_ATLASES[0];
const C2 = OUTFIT_ATLASES[1];
/** Glossy black bodysuit (shorts, high neck): a flat colour, no texture shipped. */
const BODYSUIT = Object.freeze({ color: '#1b1b20' });

// Atlas overrides: `box` = island centroid (OBJ bind metres), `uvBox` = the
// island's UV bounds must lie inside (OBJ v-up). An override may also carry its
// own `role`, `doubleSided` and `name`, and `atlas` may be a flat colour
// `{ color: '#rrggbb' }` or null (drop the island). `flip: true` turns an
// inside-out island right side out (winding + normals). First match wins.
export const MATERIALS = Object.freeze({
  // Bare shoulders/arms + hips. Hips sit a few mm under the jacket/shorts and
  // poke through when skinned: shrink them inward (never visible from outside).
  'Klukai_Body_(Default)': { texture: 'body_d.png', role: 'skin', maxSize: 512, shrink: { maxY: 1.05, amount: 0.005 } },
  // Hair cards at 2048 (the painted strand grooves are the hair's detail). The
  // black bodysuit's shorts and high-neck collar were merged into this group and
  // sampled the hair atlas (light blue streaks / a pale neck tube): they ship as
  // their own flat black fabric material.
  'Klukai_Hair': {
    texture: 'c_Clukay_hair_d.png',
    role: 'hair',
    doubleSided: true,
    maxSize: 2048,
    atlasOverrides: [
      { box: [-0.15, 0.70, -0.09, 0.15, 0.90, 0.08], atlas: BODYSUIT, role: 'cloth', doubleSided: false, flip: true, name: 'bodysuit', why: 'black shorts (2×441 faces inside the hair group, inside-out)' },
      { box: [-0.05, 1.32, -0.06, 0.05, 1.36, 0.0], uvBox: [0.2, 0.0, 0.5, 0.2], atlas: BODYSUIT, role: 'cloth', doubleSided: false, flip: true, name: 'bodysuit', why: 'black high-neck collar (rendered as a light-blue hair tube, inside-out)' },
    ],
  },
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
    // Visually verified islands the smoothness heuristic gets wrong (it rewards
    // flat regions; on GFL2 atlases the right one is often the detailed one).
    atlasOverrides: [
      { box: [-0.2, 1.40, -0.2, 0.2, 1.6, 0.2], uvBox: [0.63, 0.10, 0.89, 0.36], atlas: C1, why: 'cap crown (black panelled cap on cloth1)' },
      { box: [-0.05, 1.06, 0.0, 0.06, 1.10, 0.06], uvBox: [0.54, 0.0, 1.0, 0.045], atlas: C2, why: 'chest band outer (was the white strip of cloth1)' },
      { box: [-0.05, 0.72, -0.05, 0.06, 0.77, 0.05], uvBox: [0.0, 0.0, 0.55, 0.055], atlas: C1, why: 'jacket hem band (was the skin region of cloth2: bare lower back)' },
      { box: [-0.2, 1.18, -0.05, 0.2, 1.27, 0.03], uvBox: [0.33, 0.79, 0.73, 1.0], atlas: C1, why: 'off-shoulder hood/collar (light grey on cloth1)' },
      { box: [-0.11, 0.95, 0.08, 0.11, 1.20, 0.12], uvBox: [0.90, 0.08, 1.0, 0.62], atlas: C1, why: 'front zipper tapes' },
      { box: [-0.06, 1.04, 0.04, -0.03, 1.07, 0.06], uvBox: [0.05, 0.81, 0.49, 0.92], atlas: C2, why: 'torso straps (were the white sleeve of cloth1)' },
      { box: [0.12, 0.92, 0.02, 0.15, 0.99, 0.04], uvBox: [0.50, 0.81, 0.98, 0.91], atlas: C2, why: 'hip bag panels (grey, green N logo)' },
      { box: [0.10, 0.92, 0.11, 0.12, 0.94, 0.12], uvBox: [0.20, 0.75, 0.28, 0.82], atlas: C2, why: 'cat pouch face (black cat, yellow eyes)' },
      { box: [-0.09, 1.08, 0.095, 0.07, 1.12, 0.12], atlas: C2, why: 'chest band buckle box (grey plate; was white/purple on cloth1)' },
      { box: [-0.04, 1.23, 0.045, -0.03, 1.24, 0.055], uvBox: [0.96, 0.89, 1.0, 0.98], atlas: C1, why: 'strap piece behind the collar goggles (keep)' },
      { box: [-0.1, 1.225, 0.03, 0.1, 1.285, 0.1], atlas: C2, why: 'collar goggle unit: grey bracket with the logo box, light rims, dark lenses' },
      { box: [-0.15, -0.02, -0.12, 0.15, 0.13, 0.10], atlas: C2, why: 'shoes: black uppers, neon-green heel tabs and soles (were purple jacket fabric)' },
      { box: [0.02, 0.43, 0.025, 0.09, 0.52, 0.06], atlas: C2, why: 'knee pad shield plate (grey with gold screws; was a purple rim)' },
      { box: [-0.08, 0.66, -0.01, -0.03, 0.69, 0.08], atlas: C2, why: 'thigh strap panel (grey, cut-out pattern; was purple)' },
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

/**
 * Mixamo left `bone` unweighted (face, cap and bangs rode on the Neck, so head
 * motion in the clips was invisible). The build makes it a skin joint and moves
 * `from`'s weight onto it, blended over bind-pose height `blend` (metres).
 * Hair hanging behind the ear plane (the high ponytail and the side curtain; z below `behind.z`, OBJ
 * groups `behind.groups` only) moves a share of its spine-chain influences, fading over
 * the longer `behind.blend`, so head turns bend the tail like a rope instead of
 * shearing it at the nape (see lib/head-skin.mjs headShareFn).
 */
export const HEAD_SKIN = Object.freeze({
  bone: 'mixamorigHead',
  from: 'mixamorigNeck',
  blend: [1.27, 1.31],
  behind: Object.freeze({
    z: [0.04, -0.02],
    blend: [1.0, 1.42],
    groups: Object.freeze(['Klukai_Hair', 'Klukai_Face']),
    // only spine-chain weight moves: arm/shoulder weight is what drapes the side curtain over the shoulders
    joints: '^mixamorig(Hips|Spine\\d*|Neck)$',
  }),
});

/**
 * Cloth `_rmo` maps (R rough, G metal, B occlusion) → glTF ORM (R occlusion,
 * G roughness, B metallic) on the material whose base colour is the key.
 * Lossless webp: lossy chroma subsampling smears packed channels.
 */
export const ORM = Object.freeze({
  maxSize: 512,
  from: Object.freeze({ occlusion: 2, roughness: 0, metallic: 1 }),
  maps: Object.freeze({
    [C1]: 'c_ClukaySSR01_slg_cloth1_rmo.png',
    [C2]: 'c_ClukaySSR01_slg_cloth2_rmo.png',
  }),
});

/**
 * `_OUTLINE` vertex attribute for the viewer's inverted-hull outline (see
 * lib/outline.mjs). Face masks are bind-pose ellipsoids: width 0 inside, back
 * to 1 at (1 + feather)·radius.
 */
export const OUTLINE = Object.freeze({
  attribute: '_OUTLINE',
  quantum: 1e-5,
  minLength: 0.3,
  zeroRoles: Object.freeze(['overlay']),
  zeroTextures: Object.freeze(['c_Clukay_eye_d.png']),
  maskRoles: Object.freeze(['face']),
  faceMask: Object.freeze([
    { center: [-0.037, 1.386, 0.05], radius: [0.028, 0.025, 0.03], feather: 0.5, why: 'right eye, lids, lashes, brow' },
    { center: [0.037, 1.386, 0.05], radius: [0.028, 0.025, 0.03], feather: 0.5, why: 'left eye, lids, lashes, brow' },
    { center: [0.0, 1.339, 0.045], radius: [0.024, 0.014, 0.025], feather: 0.6, why: 'mouth' },
  ]),
  handJoints: '(Hand|Thumb|Index|Middle|Ring|Pinky)\\d*$',
  handWidth: 0.6,
});

/**
 * Optional build features. `enabled: true` makes one the default; turn one on
 * for a single build with AVATAR_FEATURES=openJacket,hairChains. All three ship
 * (2026-10-11); hairChains needs the viewer's spring physics (avatar3d.js
 * springs, same deploy), unsimulated chains swing the ponytail like a stick.
 */
export const FEATURES = Object.freeze(['openJacket', 'hairCopies', 'hairChains']);

/**
 * `Klukai_Face` carries, besides the face, a full copy of the hair (191 islands,
 * 10,417 faces, 0.6 mm from the real hair, sampling the FACE atlas at hair UVs):
 * it z-fights with the hair and doubles the blink-morph head mesh. Drop the
 * copies (islands with a near-twin in `twinsOf`).
 */
export const HAIR_COPIES = Object.freeze({ enabled: true, group: 'Klukai_Face', twinsOf: 'Klukai_Hair' });

/**
 * Open jacket: the dropped `Klukai_Cloth_1_(Astral_Luminous)` group is the same
 * jacket plus a black bodysuit bust and neckline that sit in front of the
 * jacket's chest (the in-game look: jacket open over a glossy black top). Its
 * faces are merged into `into`; the 294 islands it shares with Cloth_1 are exact
 * duplicate triangles and drop out in the dedup, so only its own pieces are
 * added (Cloth_1 keeps its legs, shoes, goggles, bag and pouch). `atlasOverrides`
 * are prepended to the target group's own.
 */
export const OPEN_JACKET = Object.freeze({
  enabled: true,
  into: 'Klukai_Cloth_1',
  from: Object.freeze(['Klukai_Cloth_1_(Astral_Luminous)']),
  atlasOverrides: Object.freeze([
    // Their own UVs land on cloth1's dark bodysuit cups (the smoothness score picked cloth2's grey).
    { box: [-0.1, 1.10, 0.04, 0.1, 1.25, 0.12], uvBox: [0.0, 0.43, 0.12, 0.62], atlas: C1, why: 'open jacket: black bodysuit bust and neckline' },
    // Keep the lower torso closed, but recess it under the jacket so animation
    // does not poke it through. Dropping these islands made the waist hollow
    // from oblique and side camera angles.
    { box: [-0.05, 0.8, -0.1, 0.05, 0.93, -0.02], uvBox: [0.35, 0.6, 0.67, 0.77], atlas: BODYSUIT, role: 'cloth', doubleSided: false, name: 'bodysuit', inset: 0.006, why: 'open jacket: bodysuit lower back (recessed under the jacket)' },
    { box: [-0.05, 0.8, 0.02, 0.05, 0.93, 0.1], uvBox: [0.74, 0.8, 0.98, 1.0], atlas: BODYSUIT, role: 'cloth', doubleSided: false, name: 'bodysuit', inset: 0.006, why: 'open jacket: bodysuit lower front (recessed under the jacket)' },
  ]),
});

/**
 * Hair bone chains for the viewer's spring physics (see lib/hair-chains.mjs).
 * Bones `hair_<chain>_<i>` (root → tip) hang under `parent`, one per polyline
 * segment fitted through the chain's hair. Seed islands are picked by their root
 * (top point); other hanging hair islands join the nearest chain within
 * `assignMaxDist`. `exclude` (island centroid boxes) keep their wave-1 weights.
 * Positions in bind-pose metres. Needs `hairCopies` (the face group's hair copy
 * would not follow the chains): the build turns it on with this.
 */
export const HAIR_CHAINS = Object.freeze({
  enabled: true,
  parent: 'mixamorigHead',
  groups: Object.freeze(['Klukai_Hair']),
  // fully on the head within radius[0] of the head centre, free beyond radius[1]
  head: Object.freeze({ center: [0, 1.40, -0.015], radius: [0.10, 0.15] }),
  rootBlend: 0.06,
  hangingBelowY: 1.30,
  assignMaxDist: 0.2,
  exclude: Object.freeze([
    { box: [0.05, 1.12, -0.06, 0.2, 1.28, 0.01], why: 'left shoulder curtain: lies on the shoulder, its arm weights drape it' },
    { box: [-0.2, 1.12, -0.06, -0.05, 1.28, 0.01], why: 'right shoulder curtain' },
  ]),
  chains: Object.freeze([
    { name: 'ponytail', bones: 5, seed: { topMinY: 1.44, topMaxZ: -0.12 } },
    { name: 'back', bones: 4, seed: { topMinY: 1.30, topMaxZ: -0.06 } },
    { name: 'sideL', bones: 3, yTop: 1.38, seed: { topMinZ: 0.0, side: 1 } },
    { name: 'sideR', bones: 3, yTop: 1.38, seed: { topMinZ: 0.0, side: -1 } },
  ]),
  // Collision shapes for the springs: spheres or capsules (`to`). `center` is a
  // bind-pose point turned into a bone-local offset; radius 'auto' = fitted
  // inside the body surface around the segment.
  colliders: Object.freeze([
    { name: 'head', bone: 'mixamorigHead', center: [0, 1.40, -0.015], radius: 0.10 },
    { name: 'neck', bone: 'mixamorigNeck', to: 'mixamorigSpine2', radius: 'auto' },
    { name: 'chest', bone: 'mixamorigSpine2', to: 'mixamorigSpine1', radius: 'auto' },
    { name: 'waist', bone: 'mixamorigSpine1', to: 'mixamorigHips', radius: 'auto' },
    { name: 'shoulderL', bone: 'mixamorigLeftArm', radius: 0.06 },
    { name: 'shoulderR', bone: 'mixamorigRightArm', radius: 0.06 },
    { name: 'thighL', bone: 'mixamorigLeftUpLeg', to: 'mixamorigLeftLeg', radius: 'auto' },
    { name: 'thighR', bone: 'mixamorigRightUpLeg', to: 'mixamorigRightLeg', radius: 'auto' },
  ]),
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
  headSkin: HEAD_SKIN,
  orm: ORM,
  outline: OUTLINE,
  openJacket: OPEN_JACKET,
  hairCopies: HAIR_COPIES,
  hairChains: HAIR_CHAINS,
});

/** Is a wave-2 feature on for this build (config `enabled` or listed in `features`)? */
export function featureOn(cfg, name, features = []) {
  return !!cfg[name] && (cfg[name].enabled === true || features.includes(name));
}

const isImage = (t) => typeof t === 'string' && /\.(png|jpe?g|webp)$/i.test(t);
const isBox = (b, n) => Array.isArray(b) && b.length === n && b.every(Number.isFinite) && b.slice(0, n / 2).every((v, i) => v <= b[i + n / 2]);

/** Problems with one material's `atlasOverrides` (pure, exported for tests). */
export function validateOverrides(name, m) {
  const errors = [];
  if (!m.atlasOverrides) return errors;
  if (!Array.isArray(m.atlasOverrides)) return [`material ${name} atlasOverrides is not a list`];
  const allowed = m.atlases ? m.atlases : [m.texture];
  m.atlasOverrides.forEach((o, i) => {
    const at = `material ${name} override ${i}${o?.why ? ` (${o.why})` : ''}`;
    if (!o || typeof o !== 'object') { errors.push(`${at} is not an object`); return; }
    if (o.box !== undefined && !isBox(o.box, 6)) errors.push(`${at} box must be [x0,y0,z0,x1,y1,z1] with min <= max`);
    if (o.uvBox !== undefined && !isBox(o.uvBox, 4)) errors.push(`${at} uvBox must be [u0,v0,u1,v1] with min <= max`);
    if (o.minUvSpan !== undefined && !(o.minUvSpan > 0 && o.minUvSpan <= 1)) errors.push(`${at} minUvSpan out of range`);
    if (!('atlas' in o)) errors.push(`${at} has no atlas (use null to drop)`);
    else if (o.atlas !== null && typeof o.atlas === 'object') {
      if (!/^#[0-9a-f]{6}$/i.test(o.atlas.color || '')) errors.push(`${at} colour must be '#rrggbb'`);
    } else if (o.atlas !== null && !allowed.includes(o.atlas)) errors.push(`${at} atlas ${o.atlas} is not one of the material's images`);
    if (o.role !== undefined && !ROLES.includes(o.role)) errors.push(`${at} has unknown role ${o.role}`);
    if (o.doubleSided !== undefined && typeof o.doubleSided !== 'boolean') errors.push(`${at} doubleSided must be boolean`);
    if (o.flip !== undefined && typeof o.flip !== 'boolean') errors.push(`${at} flip must be boolean`);
    if (o.flip && o.atlas === null) errors.push(`${at} flips a dropped island`);
    if (o.name !== undefined && !/^[a-z][a-z0-9_]*$/.test(o.name)) errors.push(`${at} name must be a lowercase slug`);
    if (o.atlas !== null && typeof o.atlas === 'object' && !o.name) errors.push(`${at} flat colour needs a name`);
  });
  return errors;
}

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
    if (!imgs.length || imgs.some((t) => !isImage(t))) errors.push(`material ${name} has no image texture`);
    if (m.texture && m.atlases) errors.push(`material ${name} sets both texture and atlases`);
    if (!ROLES.includes(m.role)) errors.push(`material ${name} has unknown role ${m.role}`);
    if (m.maxSize && m.maxSize > cfg.textureMaxSizeHard) errors.push(`material ${name} maxSize over hard cap`);
    if (m.shrink && !(m.shrink.amount > 0 && m.shrink.amount < 0.02)) errors.push(`material ${name} shrink amount out of range`);
    if (m.atlasDefault !== undefined && !imgs.includes(m.atlasDefault)) errors.push(`material ${name} atlasDefault is not one of its atlases`);
    errors.push(...validateOverrides(name, m));
  }
  if (!kept.some(([, m]) => m.role === 'face')) errors.push('a face material is required');
  if (cfg.textureMaxSize > 1024) errors.push('textureMaxSize must be <= 1024');
  if (!(cfg.budgetBytes > 0 && cfg.budgetBytes <= 15 * 1024 * 1024)) errors.push('budget must be <= 15 MB');
  if (cfg.headSkin) {
    const h = cfg.headSkin;
    if (!h.bone || !h.from || h.bone === h.from) errors.push('headSkin needs distinct bone and from joints');
    const band = (b) => Array.isArray(b) && b.length === 2 && b.every(Number.isFinite) && b[0] < b[1];
    if (!band(h.blend)) errors.push('headSkin blend must be [y0, y1] with y0 < y1');
    if (h.behind && !(band(h.behind.blend) && Array.isArray(h.behind.z) && h.behind.z.length === 2 && h.behind.z[1] < h.behind.z[0])) errors.push('headSkin behind needs blend [y0, y1] and z [zStart, zFull] with zFull < zStart');
  }
  if (cfg.orm) {
    const used = new Set(kept.flatMap(([, m]) => (m.atlases ? m.atlases : [m.texture])));
    if (!(cfg.orm.maxSize > 0 && cfg.orm.maxSize <= cfg.textureMaxSizeHard)) errors.push('orm maxSize out of range');
    const ch = Object.values(cfg.orm.from || {});
    if (ch.length !== 3 || new Set(ch).size !== 3 || ch.some((c) => !(c >= 0 && c <= 3))) errors.push('orm.from must map occlusion/roughness/metallic to 3 distinct source channels');
    for (const [diffuse, rmo] of Object.entries(cfg.orm.maps || {})) {
      if (!used.has(diffuse)) errors.push(`orm map for ${diffuse}, which no kept material uses`);
      if (!isImage(rmo)) errors.push(`orm source for ${diffuse} is not an image`);
    }
  }
  if (cfg.openJacket) {
    const oj = cfg.openJacket;
    if (!cfg.materials[oj.into] || cfg.materials[oj.into].drop) errors.push('openJacket.into must be a kept material');
    for (const g of oj.from || []) if (!(g in cfg.materials)) errors.push(`openJacket.from ${g} is not an OBJ material in the config`);
    errors.push(...validateOverrides(`${oj.into} (openJacket)`, { ...cfg.materials[oj.into], atlasOverrides: oj.atlasOverrides || [] }));
  }
  if (cfg.hairCopies) {
    for (const g of [cfg.hairCopies.group, cfg.hairCopies.twinsOf]) if (!cfg.materials[g] || cfg.materials[g].drop) errors.push(`hairCopies: ${g} must be a kept material`);
  }
  if (cfg.hairChains) {
    const h = cfg.hairChains;
    const names = (h.chains || []).map((c) => c.name);
    if (!names.length) errors.push('hairChains needs chains');
    if (new Set(names).size !== names.length) errors.push('hairChains chain names must be unique');
    for (const c of h.chains || []) {
      if (!/^[A-Za-z][A-Za-z0-9]*$/.test(c.name)) errors.push(`hairChains chain name ${c.name} must be alphanumeric`);
      if (!(Number.isInteger(c.bones) && c.bones >= 2 && c.bones <= 8)) errors.push(`hairChains ${c.name}: bones must be 2..8`);
      if (!c.seed || typeof c.seed !== 'object') errors.push(`hairChains ${c.name}: needs a seed rule`);
    }
    for (const g of h.groups || []) if (!cfg.materials[g] || cfg.materials[g].drop) errors.push(`hairChains group ${g} must be a kept material`);
    if (!(h.head && h.head.center?.length === 3 && h.head.radius?.length === 2 && h.head.radius[0] < h.head.radius[1])) errors.push('hairChains head needs center[3] and radius [rigid, free] with rigid < free');
    if (!(h.rootBlend > 0)) errors.push('hairChains rootBlend must be > 0');
    for (const e of h.exclude || []) if (!isBox(e.box, 6)) errors.push(`hairChains exclude ${e.why || '?'}: box must be [x0,y0,z0,x1,y1,z1]`);
    for (const c of h.colliders || []) {
      if (!c.bone) errors.push(`hairChains collider ${c.name || '?'} needs a bone`);
      if (!(c.radius === 'auto' ? !!c.to : c.radius > 0)) errors.push(`hairChains collider ${c.name || '?'}: radius must be > 0 (or 'auto' for capsules)`);
    }
  }
  if (cfg.outline) {
    const o = cfg.outline;
    if (!/^_[A-Z0-9_]+$/.test(o.attribute || '')) errors.push('outline attribute must be an application-specific glTF name (_UPPERCASE)');
    if (!(o.minLength > 0 && o.minLength < 1)) errors.push('outline minLength must be in (0, 1)');
    if (!(o.quantum > 0 && o.quantum <= 1e-3)) errors.push('outline quantum out of range');
    if (!(o.handWidth >= 0 && o.handWidth <= 1)) errors.push('outline handWidth must be in [0, 1]');
    for (const m of o.faceMask || []) {
      if (!(Array.isArray(m.center) && m.center.length === 3 && Array.isArray(m.radius) && m.radius.length === 3 && m.radius.every((r) => r > 0))) errors.push(`outline face mask ${m.why || '?'} needs center[3] and positive radius[3]`);
    }
    for (const r of [...(o.zeroRoles || []), ...(o.maskRoles || [])]) if (!ROLES.includes(r)) errors.push(`outline role ${r} unknown`);
  }
  return errors;
}
