// node --test tools/avatar/test/   (no source assets needed)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  STATES, STATE_CLIPS, moodToState, mappedMoods, moodCoverage, renderSettings, detectIOS, cameraFraming,
} from '../../../flutter_app/web/companion/avatar3d.js';
import { MOODS } from '../../../flutter_app/web/companion/logic.js';
import {
  CONFIG, CLIPS, REQUIRED_CLIPS, MATERIALS, clipNameForFile, validateConfig,
} from '../avatar.config.mjs';
import { uvIslands, rng } from '../lib/atlas-assign.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../../..');

/** Backend source of truth: docker/core/app/fact_extractor.py VALID_MOODS. */
function backendMoods() {
  const src = fs.readFileSync(path.join(repo, 'docker/core/app/fact_extractor.py'), 'utf8');
  const m = src.match(/VALID_MOODS\s*=\s*frozenset\(\{([\s\S]*?)\}\)/);
  assert.ok(m, 'VALID_MOODS not found in fact_extractor.py');
  return [...m[1].matchAll(/"([a-z_]+)"/g)].map((x) => x[1]);
}

// ── moods → states ─────────────────────────────────────────────────────────

test('every backend mood (all 50) maps to a valid viewer state', () => {
  const moods = backendMoods();
  assert.equal(moods.length, 50);
  for (const mood of moods) {
    const s = moodToState(mood);
    assert.ok(STATES.includes(s), `${mood} → ${s} is not a state`);
  }
});

test('the mapping is explicit for exactly the backend + logic.js mood lists', () => {
  const backend = new Set(backendMoods());
  assert.deepEqual(new Set(mappedMoods()), backend);
  assert.deepEqual(new Set(MOODS), backend, 'logic.js MOODS drifted from VALID_MOODS');
  assert.deepEqual(moodCoverage(), { missing: [], extra: [] });
});

test('mood mapping semantics', () => {
  const cases = {
    composed: 'idle', drowsy: 'sleep', grateful: 'happy', quietly_pleased: 'happy', flustered: 'flustered',
    embarrassed: 'flustered', shy: 'bashful', grieving: 'sad', melancholic: 'sad', curious: 'thinking',
    calculating: 'thinking', excited: 'excited', adrenaline: 'excited', scared: 'flustered',
  };
  for (const [mood, state] of Object.entries(cases)) assert.equal(moodToState(mood), state, mood);
});

test('moodToState is total and pure', () => {
  for (const bad of [undefined, null, '', 'nope', 42, {}, 'SMUG']) assert.equal(moodToState(bad), 'idle');
  assert.equal(moodToState('  Content '), 'happy');
  assert.equal(moodToState('content'), moodToState('content'));
});

test('every state is reachable from at least one mood or is an explicit app state', () => {
  const reached = new Set(backendMoods().map(moodToState));
  for (const s of STATES) {
    if (['talking', 'greet'].includes(s)) continue; // driven by the app, not moods
    assert.ok(reached.has(s), `state ${s} unreachable from moods`);
  }
});

// ── states → clips ─────────────────────────────────────────────────────────

test('each state plays only clips the build ships', () => {
  assert.deepEqual(Object.keys(STATE_CLIPS).sort(), [...STATES].sort());
  for (const [state, spec] of Object.entries(STATE_CLIPS)) {
    assert.ok(spec.clips.length > 0, state);
    for (const c of spec.clips) assert.ok(REQUIRED_CLIPS.includes(c), `${state} uses unknown clip ${c}`);
    if (spec.once) assert.equal(spec.loop, false, `${state} once implies non-looping`);
  }
  assert.equal(STATE_CLIPS.idle.clips[0], 'idle');
  assert.equal(STATE_CLIPS.talking.clips[0], 'talking');
});

// ── build config ───────────────────────────────────────────────────────────

test('clip file → name mapping', () => {
  assert.equal(clipNameForFile('Dismissing Gesture.fbx'), 'dismiss');
  assert.equal(clipNameForFile('Nervously Look Around.fbx'), 'nervous');
  assert.equal(clipNameForFile('Laying Sleeping.fbx'), 'sleep');
  assert.equal(clipNameForFile('Female Standing Pose.fbx'), 'stand');
  assert.equal(clipNameForFile('Kneeling Down.fbx'), 'kneel');
  assert.equal(clipNameForFile('Some New Clip.FBX'), 'some_new_clip');
  assert.deepEqual(CLIPS.map((c) => c.name).sort(), [...REQUIRED_CLIPS].sort());
  for (const c of CLIPS) assert.match(c.name, /^[a-z][a-z0-9_]*$/);
});

test('shipped config validates', () => {
  assert.deepEqual(validateConfig(CONFIG), []);
});

test('config validation catches mistakes', () => {
  const bad = (patch) => validateConfig({ ...CONFIG, ...patch });
  assert.ok(bad({ clips: CLIPS.filter((c) => c.name !== 'idle') }).some((e) => e.includes('missing required clip: idle')));
  assert.ok(bad({ clips: [...CLIPS, { file: 'X.fbx', name: 'Idle' }] }).some((e) => e.includes('lowercase')));
  assert.ok(bad({ clips: [...CLIPS, { file: 'X.fbx', name: 'idle' }] }).some((e) => e.includes('duplicate')));
  assert.ok(bad({ textureMaxSize: 2048 }).some((e) => e.includes('textureMaxSize')));
  assert.ok(bad({ budgetBytes: 40 * 1024 * 1024 }).some((e) => e.includes('budget')));
  assert.ok(bad({ materials: { ...MATERIALS, Klukai_Face: { drop: true }, Klukai_Eyes: { drop: true } } }).some((e) => e.includes('face material')));
  assert.ok(bad({ materials: { ...MATERIALS, Klukai_Hair: { texture: 'hair.tga', role: 'hair' } } }).some((e) => e.includes('no image texture')));
  assert.ok(bad({ materials: { ...MATERIALS, Klukai_Hair: { texture: 'h.png', role: 'fur' } } }).some((e) => e.includes('unknown role')));
});

test('size budget is within the iOS limit (≤ 15 MB) and textures ≤ 1024 by default', () => {
  assert.ok(CONFIG.budgetBytes <= 15 * 1024 * 1024);
  assert.ok(CONFIG.textureMaxSize <= 1024);
  assert.ok(CONFIG.textureMaxSizeHard <= 2048);
});

// ── viewer pure helpers ────────────────────────────────────────────────────

test('renderSettings caps pixel ratio and disables AA on iOS', () => {
  assert.deepEqual(renderSettings({ devicePixelRatio: 3, width: 390, height: 844, isIOS: true }), { pixelRatio: 1.5, antialias: false });
  assert.deepEqual(renderSettings({ devicePixelRatio: 3, width: 1280, height: 800, isIOS: false }), { pixelRatio: 2, antialias: true });
  assert.equal(renderSettings({ devicePixelRatio: 1, width: 1280, height: 800 }).pixelRatio, 1);
  assert.equal(renderSettings({ devicePixelRatio: undefined }).pixelRatio, 1);
});

test('detectIOS', () => {
  assert.equal(detectIOS({ userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)' }), true);
  assert.equal(detectIOS({ userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)', maxTouchPoints: 5 }), true); // iPadOS
  assert.equal(detectIOS({ userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)', maxTouchPoints: 0 }), false);
  assert.equal(detectIOS({ userAgent: 'Mozilla/5.0 (X11; Linux x86_64)' }), false);
  assert.equal(detectIOS(undefined), false);
});

test('camera framing keeps the cap in frame and finds her when asleep', () => {
  for (const aspect of [390 / 844, 1280 / 800]) {
    const f = cameraFraming({ framing: 'upper', aspect, headY: 1.36 });
    const half = Math.tan((f.fov / 2) * Math.PI / 180) * f.dist;
    assert.ok(f.targetY + half >= 1.36 + 0.2, `cap clipped at aspect ${aspect}`);
    assert.ok(f.targetY - half < 1.2, 'upper framing shows at least the chest');
    const full = cameraFraming({ framing: 'full', aspect, headY: 1.36 });
    const fh = Math.tan((full.fov / 2) * Math.PI / 180) * full.dist;
    assert.ok(full.targetY + fh > 1.54 && full.targetY - fh < 0.05, `full body clipped at aspect ${aspect}`); // model top ≈ 1.524 m
  }
  assert.ok(cameraFraming({ state: 'sleep', aspect: 1.6 }).targetY < 0.5);
});

// ── build helpers ──────────────────────────────────────────────────────────

test('uvIslands splits on UV seams, not on shared UV values', () => {
  // two triangles sharing an edge (same positions + UVs) + one far triangle that
  // reuses a UV index (Blender OBJ de-dups identical UV coords) but no edge.
  const obj = {
    faceV: Int32Array.from([0, 1, 2, 1, 3, 2, 4, 5, 6]),
    faceT: Int32Array.from([0, 1, 2, 1, 3, 2, 0, 4, 5]),
  };
  const isl = uvIslands(obj, [0, 1, 2]).map((a) => a.sort()).sort((a, b) => a[0] - b[0]);
  assert.deepEqual(isl, [[0, 1], [2]]);
});

test('rng is deterministic (reproducible builds)', () => {
  const a = rng(416); const b = rng(416);
  for (let i = 0; i < 5; i++) assert.equal(a(), b());
  const c = rng(417);
  assert.notEqual(rng(416)(), c());
});

// ── hollow / perf regressions ──────────────────────────────────────────────

import { nextPixelRatio, warmAvatar3D } from '../../../flutter_app/web/companion/avatar3d.js';
import { coreNamesUsed, ADDONS, BUNDLE } from '../vendor.mjs';

test('viewer never uses polygonOffset (it made double-sided cloth look hollow on Safari/Metal)', () => {
  const src = fs.readFileSync(path.join(repo, 'flutter_app/web/companion/avatar3d.js'), 'utf8')
    .replace(/\/\/.*$/gm, '').replace(/\/\*[\s\S]*?\*\//g, '');
  assert.doesNotMatch(src, /polygonOffset/);
});

test('adaptive resolution steps between 1 and the device cap', () => {
  assert.equal(nextPixelRatio({ avgFrameMs: 40, ratio: 1.5, cap: 1.5 }), 1.25);
  assert.equal(nextPixelRatio({ avgFrameMs: 40, ratio: 1, cap: 1.5 }), 1);
  assert.equal(nextPixelRatio({ avgFrameMs: 8, ratio: 1, cap: 1.5 }), 1.25);
  assert.equal(nextPixelRatio({ avgFrameMs: 8, ratio: 2, cap: 2 }), 2);
  assert.equal(nextPixelRatio({ avgFrameMs: 16.7, ratio: 1.25, cap: 2 }), 1.25); // 60 fps: hold
});

test('warmAvatar3D never throws, even without arguments', () => {
  assert.doesNotThrow(() => warmAvatar3D());
  assert.doesNotThrow(() => warmAvatar3D({ loadModel: () => Promise.reject(new Error('offline')) }));
});

test('vendored bundle exports everything the viewer uses', () => {
  const bundle = fs.readFileSync(path.join(repo, 'flutter_app/web/companion/vendor', BUNDLE), 'utf8');
  const exported = new Set([...bundle.matchAll(/export\s*\{([^}]*)\}/g)].flatMap((m) => m[1].split(',').map((x) => x.trim().split(/\s+as\s+/).pop())));
  for (const name of [...coreNamesUsed(), ...Object.keys(ADDONS)]) assert.ok(exported.has(name), `bundle lacks ${name} — run node tools/avatar/vendor.mjs`);
  assert.doesNotMatch(bundle, /https?:\/\/(cdn|unpkg|jsdelivr)/, 'no CDN references');
});

test('build config: compression settings and budget', () => {
  assert.ok(['medium', 'high'].includes(CONFIG.meshoptLevel));
  assert.ok(CONFIG.resampleTolerance > 0 && CONFIG.resampleTolerance <= 2e-3, 'resample tolerance keeps rotation error ≲0.1°');
});

test('built GLB structure (skipped when assets/build/klukai.glb is absent)', async (t) => {
  const glbPath = path.join(repo, 'assets/build/klukai.glb');
  if (!fs.existsSync(glbPath)) { t.skip('no local build (assets are personal-use, never in CI)'); return; }
  const { NodeIO } = await import('@gltf-transform/core');
  const { ALL_EXTENSIONS } = await import('@gltf-transform/extensions');
  const { MeshoptDecoder } = await import('meshoptimizer');
  await MeshoptDecoder.ready;
  const io = new NodeIO().registerExtensions(ALL_EXTENSIONS).registerDependencies({ 'meshopt.decoder': MeshoptDecoder });
  assert.ok(fs.statSync(glbPath).size <= CONFIG.budgetBytes);
  const doc = await io.read(glbPath);
  const r = doc.getRoot();
  assert.equal(r.listSkins().length, 1, 'body and head share one skin');
  const withTargets = r.listMeshes().filter((m) => m.listPrimitives().some((p) => p.listTargets().length));
  assert.equal(withTargets.length, 1, 'blink morph lives on the head mesh only');
  assert.deepEqual(withTargets[0].getExtras().targetNames, ['blink']);
  assert.deepEqual(r.listAnimations().map((a) => a.getName()).sort(), [...REQUIRED_CLIPS].sort());
  const prims = r.listMeshes().reduce((n, m) => n + m.listPrimitives().length, 0);
  assert.ok(prims <= 8, `draw calls ${prims}`);
  for (const tex of r.listTextures()) assert.equal(tex.getMimeType(), 'image/webp');
});
