// node --test tools/avatar/test/   (no source assets needed)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  STATES, STATE_CLIPS, moodToState, mappedMoods, moodCoverage, renderSettings, detectIOS, cameraFraming,
  iosMajorVersion,
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

test('renderSettings caps pixel ratio and keeps MSAA on, iOS included (outlines crawl without it)', () => {
  assert.deepEqual(renderSettings({ devicePixelRatio: 3, width: 390, height: 844, isIOS: true }), { pixelRatio: 1.75, antialias: true });
  assert.deepEqual(renderSettings({ devicePixelRatio: 3, width: 390, height: 844, isIOS: false }), { pixelRatio: 1.75, antialias: true });
  assert.deepEqual(renderSettings({ devicePixelRatio: 3, width: 1280, height: 800, isIOS: false }), { pixelRatio: 2, antialias: true });
  assert.deepEqual(renderSettings({ devicePixelRatio: 2, width: 1024, height: 1366, isIOS: true }), { pixelRatio: 2, antialias: true }); // iPad
  assert.equal(renderSettings({ devicePixelRatio: 1, width: 1280, height: 800 }).pixelRatio, 1);
  assert.equal(renderSettings({ devicePixelRatio: undefined }).pixelRatio, 1);
  // iOS 15/16 had ANGLE-Metal MSAA regressions: those keep the old no-AA, 1.5-cap settings.
  assert.deepEqual(renderSettings({ devicePixelRatio: 3, width: 390, height: 844, isIOS: true, iosMajor: 16 }), { pixelRatio: 1.5, antialias: false });
  assert.deepEqual(renderSettings({ devicePixelRatio: 3, width: 390, height: 844, isIOS: true, iosMajor: 17 }), { pixelRatio: 1.75, antialias: true });
  assert.deepEqual(renderSettings({ devicePixelRatio: 3, width: 390, height: 844, isIOS: false, iosMajor: 16 }), { pixelRatio: 1.75, antialias: true });
});

test('iosMajorVersion reads iPhone and desktop-mode iPad user agents', () => {
  assert.equal(iosMajorVersion('Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1'), 16);
  assert.equal(iosMajorVersion('Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/130.0 Mobile/15E148 Safari/604.1'), 18);
  assert.equal(iosMajorVersion('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15'), 17);
  assert.equal(iosMajorVersion('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'), 0);
  assert.equal(iosMajorVersion(''), 0);
});

test('the renderer never asks for the high-performance GPU (WebKit AA bug; battery)', () => {
  const src = fs.readFileSync(path.join(repo, 'flutter_app/web/companion/avatar3d.js'), 'utf8')
    .replace(/\/\/.*$/gm, '').replace(/\/\*[\s\S]*?\*\//g, '');
  assert.match(src, /powerPreference: 'low-power'/);
  assert.doesNotMatch(src, /high-performance/);
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
  for (let aspect = 0.4; aspect < 0.8; aspect += 0.02) { // every portrait shape, not just two phones
    const full = cameraFraming({ framing: 'full', aspect });
    const fh = Math.tan((full.fov / 2) * Math.PI / 180) * full.dist;
    assert.ok(full.targetY + fh > 1.56 && full.targetY - fh < 0, `full body clipped at aspect ${aspect.toFixed(2)}`);
  }
});

const tanD = (deg) => Math.tan((deg * Math.PI) / 180);
// world y at a canvas fraction (0 = top) for a framing (camera ~level with its target)
const yAt = (f, frac) => f.targetY + (0.5 - frac) * 2 * f.dist * tanD(f.fov / 2);

test('camera framing without insets reproduces the original framing (22° FOV on phones, same crop)', () => {
  const land = cameraFraming({ framing: 'upper', aspect: 1280 / 800, headY: 1.36 });
  assert.equal(land.fov, 26);
  assert.ok(Math.abs(land.dist - 1.35) < 1e-9);
  assert.ok(Math.abs(land.targetY - (1.36 + 0.22 - 1.35 * tanD(13))) < 1e-9);
  assert.ok(Math.abs(land.camY - land.targetY - 0.06) < 1e-9);
  const phone = cameraFraming({ framing: 'upper', aspect: 390 / 682, headY: 1.36 });
  assert.equal(phone.fov, 22);
  assert.ok(Math.abs(phone.dist - 1.9 * tanD(13) / tanD(11)) < 1e-9, 'distance scaled by tan13/tan11');
  assert.ok(Math.abs(yAt(phone, 0) - (1.36 + 0.22)) < 1e-9, 'top of frame still at the cap + margin');
  assert.ok(Math.abs(yAt(phone, 1) - (1.36 + 0.22 - 2 * 1.9 * tanD(13))) < 1e-9, 'same visible height');
  for (const aspect of [390 / 682, 1.6]) {
    const full = cameraFraming({ framing: 'full', aspect });
    assert.equal(full.fov, 26, 'full framing keeps 26°');
    const sleep = cameraFraming({ state: 'sleep', aspect });
    assert.equal(sleep.fov, 26);
    assert.equal(sleep.side, true);
  }
  assert.deepEqual(cameraFraming({ aspect: 0.5, insets: { top: 0, bottom: 0 } }), cameraFraming({ aspect: 0.5 }));
  assert.deepEqual(cameraFraming({ aspect: 0.5, insets: { top: 'x', bottom: null } }), cameraFraming({ aspect: 0.5 }), 'bad insets = none');
});

test('camera framing with insets fits the design crop into the visible band', () => {
  // iPhone-ish stage, full-height canvas: topbar covers 95 px, dock 164 px of 844
  const H = 844; const top = 95 / H; const bottom = 164 / H; const aspect = 390 / H;
  const headY = 1.36;
  const f = cameraFraming({ framing: 'upper', aspect, headY, insets: { top, bottom } });
  const old = cameraFraming({ framing: 'upper', aspect: 390 / (H - 95 - 164), headY });
  assert.equal(f.fov, 22);
  // same size on screen as the old canvas-between-the-bars framing
  const band = yAt(f, top) - yAt(f, 1 - bottom);
  const oldScale = yAt(old, 0) - yAt(old, 1);
  assert.ok(Math.abs(band - oldScale) < 1e-6, `visible band ${band} m vs ${oldScale} m`);
  // the cap sits below the top inset, with headroom
  const capY = headY + 0.2;
  assert.ok(yAt(f, top) > capY, 'cap under the topbar');
  assert.ok(yAt(f, top) - capY < 0.12, 'but not lost in empty space');
  // the body keeps going below the dock edge (into the fade), not cut at it
  assert.ok(yAt(f, 1) < yAt(f, 1 - bottom) - 0.15);
  // full framing: the feet stay above the dock's fade
  const full = cameraFraming({ framing: 'full', aspect, insets: { top, bottom } });
  assert.ok(yAt(full, 1 - bottom - 0.05) < 0.0, 'feet clear of the dock');
  assert.ok(yAt(full, top) > 1.54, 'cap clear of the topbar');
  // Overlapping insets retain a finite projection instead of dividing by zero.
  const tiny = cameraFraming({ framing: 'upper', aspect, headY, insets: { top: 0.6, bottom: 0.6 } });
  assert.ok(Number.isFinite(tiny.dist) && tiny.dist > 0);
});

test('lookSplit clamps and splits the look correction over neck and head', () => {
  const d = Math.PI / 180;
  const j = lookSplit({ yaw: 20 * d, pitch: 10 * d, headJoint: true });
  assert.ok(Math.abs(j.neck.yaw - 8 * d) < 1e-9 && Math.abs(j.head.yaw - 12 * d) < 1e-9, 'neck 40 % / head 60 %');
  assert.ok(Math.abs(j.neck.pitch + j.head.pitch - 10 * d) < 1e-9);
  const big = lookSplit({ yaw: 90 * d, pitch: -60 * d, headJoint: true });
  assert.ok(Math.abs(big.neck.yaw + big.head.yaw - 35 * d) < 1e-9, 'yaw ±35°');
  assert.ok(Math.abs(big.neck.pitch + big.head.pitch + 15 * d) < 1e-9, 'pitch down 15°');
  const up = lookSplit({ pitch: 40 * d, headJoint: true });
  assert.ok(Math.abs(up.neck.pitch + up.head.pitch - 20 * d) < 1e-9, 'pitch up 20°');
  const n = lookSplit({ yaw: -90 * d, pitch: 40 * d, headJoint: false });
  assert.ok(Math.abs(n.neck.yaw + 25 * d) < 1e-9, 'neck only: ±25°');
  assert.ok(Math.abs(n.neck.pitch - 15 * d) < 1e-9);
  assert.deepEqual(n.head, { yaw: 0, pitch: 0 }, 'an unskinned Head gets nothing (it deforms nothing)');
  assert.deepEqual(lookSplit({ yaw: NaN }), { neck: { yaw: 0, pitch: 0 }, head: { yaw: 0, pitch: 0 } });
  for (const s of STATES) assert.ok(LOOK_WEIGHT[s] >= 0 && LOOK_WEIGHT[s] <= 1, `look weight for ${s}`);
  assert.equal(LOOK_WEIGHT.sleep, 0);
});

test('parseTint accepts numbers, hex, rgb()/rgba() and expression names; rejects junk', () => {
  assert.equal(parseTint(0xff9fbf), 0xff9fbf);
  assert.equal(parseTint('#FF9FBF'), 0xff9fbf);
  assert.equal(parseTint(' #8fd8ff '), 0x8fd8ff);
  assert.equal(parseTint('#abc'), 0xaabbcc);
  assert.equal(parseTint('rgba(232, 140, 165, 0.14)'), (232 << 16) | (140 << 8) | 165);
  assert.equal(parseTint('rgb(79,195,247)'), (79 << 16) | (195 << 8) | 247);
  assert.equal(parseTint('smile'), RIM_TINTS.smile);
  for (const bad of [undefined, null, '', 'red', '#12345', '#1234567', 'rgb(300,0,0)', -1, 0x1000000, 1.5, NaN, {}, 'constructor']) {
    assert.equal(parseTint(bad), null, String(bad));
  }
});

test('RIM_TINTS covers every portrait expression', async () => {
  const { moodToExpression } = await import('../../../flutter_app/web/companion/logic.js');
  for (const m of backendMoods()) assert.ok(RIM_TINTS[moodToExpression(m)] !== undefined, m);
});

test('mood hysteresis: mood-to-mood changes hold 4 s unless strong', () => {
  assert.equal(moodGate({ now: 1000, lastAt: 0, current: 'happy', next: 'happy' }), 'same');
  assert.equal(moodGate({ now: 1000, lastAt: 0, current: 'idle', next: 'happy' }), 'defer');
  assert.equal(moodGate({ now: 4000, lastAt: 0, current: 'idle', next: 'happy' }), 'apply');
  assert.equal(moodGate({ now: 1000, lastAt: 0, current: 'idle', next: 'flustered', strong: true }), 'apply');
  assert.equal(moodGate({ now: 1000, lastAt: 0, fromMood: false, current: 'thinking', next: 'happy' }), 'apply');
  for (const m of STRONG_MOODS) assert.ok(MOODS.includes(m), m);
  assert.equal(MOOD_HOLD_MS, 4000);
});

test('state machine: setState is immediate and a state set directly is never trapped by the mood hold', () => {
  let t = 0; const log = [];
  const sm = createStateMachine({ now: () => t, enter: (next, prev, src) => log.push(`${prev}>${next}:${src}`) });
  assert.equal(sm.setMood('content'), 'apply'); // first mood: nothing to hold
  assert.equal(sm.state, 'happy');
  t = 500;
  assert.equal(sm.setState('thinking'), true); // the stage: she is thinking
  assert.equal(sm.state, 'thinking');
  t = 900; // thinking ends 0.9 s after the last mood change: the mood must still apply at once
  assert.equal(sm.setMood('content'), 'apply');
  assert.equal(sm.state, 'happy');
  for (const s of ['talking', 'greet', 'sleep', 'excited', 'thinking']) {
    t += 100;
    sm.setState(s);
    assert.equal(sm.state, s, `setState(${s}) immediate`);
    assert.equal(sm.setMood('composed'), 'apply', `leaving ${s}`);
    assert.equal(sm.state, 'idle');
  }
  // mood → mood inside the window is held, then applied (latest wins) by tick()
  t += 100;
  assert.equal(sm.setMood('curious'), 'defer');
  assert.equal(sm.setMood('shy'), 'defer');
  assert.equal(sm.state, 'idle');
  assert.equal(sm.tick(), null);
  t += 4000;
  assert.equal(sm.tick(), 'apply');
  assert.equal(sm.state, 'bashful');
  assert.equal(sm.pending, null);
  // strong moods cut through the hold
  t += 10;
  assert.equal(sm.setMood('terrified'), 'apply');
  assert.equal(sm.state, 'flustered');
  // setState drops a held mood
  t += 10;
  sm.setMood('content');
  assert.equal(sm.pending, 'content');
  sm.setState('thinking');
  assert.equal(sm.pending, null);
  t += 5000;
  assert.equal(sm.tick(), null);
  assert.equal(sm.state, 'thinking');
  // a one-shot ending settles without re-entering and keeps the direct source
  sm.setState('greet'); sm.settle('idle');
  assert.equal(sm.state, 'idle'); assert.equal(sm.source, 'state');
  assert.equal(sm.setMood('content'), 'apply');
  assert.equal(sm.setState('nope'), false);
  assert.ok(log.length > 10);
});

test('speech envelope: a syllable onset fires one nod, steady speech and silence fire none', () => {
  let env; let onsets = 0;
  for (let i = 0; i < 60; i++) { env = stepEnvelope(env, 0, 1 / 60); if (env.onset) onsets++; }
  assert.equal(onsets, 0, 'silence');
  let first = -1;
  for (let i = 0; i < 30; i++) { env = stepEnvelope(env, 0.8, 1 / 60); if (env.onset) { onsets++; if (first < 0) first = i; } }
  assert.equal(onsets, 1, 'one onset per rising edge');
  assert.ok(first >= 0 && first < 6, 'within ~100 ms');
  for (let i = 0; i < 180; i++) { env = stepEnvelope(env, 0.8, 1 / 60); if (env.onset) onsets++; }
  assert.equal(onsets, 1, 'steady level: no new nods');
  // bursts every 150 ms: the 280 ms refractory keeps nods spaced out
  onsets = 0;
  for (let i = 0; i < 120; i++) { env = stepEnvelope(env, (i % 9) < 3 ? 0.9 : 0.05, 1 / 60); if (env.onset) onsets++; }
  assert.ok(onsets >= 3 && onsets <= 8, `bursts: ${onsets}`);
});

test('nod and blink curves', () => {
  assert.equal(nodCurve(-0.1, 1), 0);
  assert.equal(nodCurve(NaN, 1), 0);
  assert.ok(Math.abs(nodCurve(0.1, 0.07) - 0.07) < 1e-12, 'peak = amp at tau');
  assert.ok(nodCurve(0.05, 1) < 1 && nodCurve(0.2, 1) < 1);
  assert.ok(nodCurve(0.6, 1) < 0.05, 'mostly gone by 0.6 s');
  assert.equal(nodCurve(1, 1), 0);
  assert.equal(blinkCurve(0), 0);
  assert.equal(blinkCurve(60), 1);
  assert.equal(blinkCurve(171), -1, 'a blink takes 170 ms');
  assert.ok(blinkCurve(171, true) > 0, 'a slow blink is still closing/opening');
  assert.equal(blinkCurve(380, true), -1, 'slow blink ≈ 0.37 s');
});

test('eye guard soft limit: passes below start, eases toward max, never exceeds it, continuous', () => {
  assert.equal(softLimit(-5, -3, 1), -5);
  assert.equal(softLimit(-3, -3, 1), -3);
  for (const x of [-2, 0, 1, 3, 10, 40, 90]) {
    const y = softLimit(x, -3, 1);
    assert.ok(y <= 1 + 1e-12 && y <= x, `${x} → ${y}`);
    assert.ok(y > -3, 'monotone above start');
  }
  assert.ok(softLimit(40, -3, 1) > softLimit(3, -3, 1), 'monotone');
  assert.ok(Math.abs(softLimit(-3 + 1e-4, -3, 1) - (-3 + 1e-4)) < 1e-7, 'slope 1 at start (no kink)');
  assert.equal(softLimit(5, 2, 2), 2, 'degenerate span: hard limit');
  assert.equal(softLimit(NaN, -3, 1), NaN);
  // calibration (tools/avatar/debug/eyes.py): irises clear the brim at ≤ 0°, half hidden by 3–5°
  assert.ok(EYE_GUARD.max <= 2 && EYE_GUARD.start < EYE_GUARD.max && EYE_GUARD.side > 0);
});

// ── spring-bone hair (pure step) ──────────────────────────────────────────────
const near = (a, b, eps = 1e-9) => a.every((v, i) => Math.abs(v - b[i]) < eps);
const len3 = (a, b = [0, 0, 0]) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
const base = (o = {}) => ({ head: [0, 0, 0], len: 1, restDir: [0, -1, 0], stiffness: 0, drag: 0, gravity: 0, dt: 1 / 60, ...o });

test('spring step: at rest with no forces the tail stays put (and on the bone length)', () => {
  const out = springStep([0, 0, 0], base({ cur: [0, -1, 0], prev: [0, -1, 0] }));
  assert.ok(near(out, [0, -1, 0]));
  const o2 = springStep([0, 0, 0], base({ cur: [0, -2, 0], prev: [0, -2, 0] })); // off-length → put back on it
  assert.ok(Math.abs(len3(o2) - 1) < 1e-12);
});

test('spring step: inertia carries the tail on, drag eats velocity', () => {
  const free = springStep([0, 0, 0], base({ cur: [0.1, -1, 0], prev: [0, -1, 0] }));
  assert.ok(free[0] > 0.1, 'keeps moving in +x');
  const damped = springStep([0, 0, 0], base({ cur: [0.1, -1, 0], prev: [0, -1, 0], drag: 0.5 }));
  assert.ok(damped[0] > 0.1 && damped[0] < free[0], 'half the velocity');
  const stopped = springStep([0, 0, 0], base({ cur: [0.1, -1, 0], prev: [0, -1, 0], drag: 1 }));
  assert.ok(Math.abs(stopped[0] - 0.1 / Math.hypot(0.1, 1)) < 1e-9, 'drag 1: no inertia, only re-lengthened');
  for (const o of [free, damped, stopped]) assert.ok(Math.abs(len3(o) - 1) < 1e-12);
});

test('spring step: stiffness pulls toward the rest direction, gravity pulls down', () => {
  const pulled = springStep([0, 0, 0], base({ cur: [0, -1, 0], prev: [0, -1, 0], restDir: [1, 0, 0], stiffness: 3 }));
  assert.ok(pulled[0] > 0 && pulled[1] < 0, 'swings toward +x');
  let p = [1, 0, 0]; let q = [1, 0, 0]; // horizontal, released: falls under gravity
  for (let i = 0; i < 240; i++) { const n = springStep([0, 0, 0], base({ cur: p, prev: q, restDir: [1, 0, 0], gravity: 4, drag: 0.3 })); q = p; p = n; }
  assert.ok(p[1] < -0.5, `hangs down (${p[1].toFixed(2)})`);
  // fixed point: along stiffness·rest + gravity·down
  const s = 1; const g = 1; let a = [0, -1, 0]; let b = [0, -1, 0];
  for (let i = 0; i < 2000; i++) { const n = springStep([0, 0, 0], base({ cur: a, prev: b, restDir: [1, 0, 0], stiffness: s, gravity: g, drag: 0.4 })); b = a; a = n; }
  const want = springHang([0, 0, 0], base({ restDir: [1, 0, 0], stiffness: s, gravity: g }));
  assert.ok(near(a, want, 1e-4), `settles at the hang direction ${a.map((v) => v.toFixed(3))} vs ${want.map((v) => v.toFixed(3))}`);
  assert.ok(near(want, [Math.SQRT1_2, -Math.SQRT1_2, 0], 1e-12));
});

test('spring step: the cone keeps the tail within maxAngle of the rest direction', () => {
  const out = springStep([0, 0, 0], base({ cur: [1, 0, 0], prev: [1, 0, 0], restDir: [0, -1, 0], maxAngle: Math.PI / 6 }));
  const ang = Math.acos(-out[1] / len3(out));
  assert.ok(Math.abs(ang - Math.PI / 6) < 1e-9, `${(ang * 180 / Math.PI).toFixed(2)}°`);
  assert.ok(Math.abs(len3(out) - 1) < 1e-12);
  const inside = [0.1, -1, 0]; const before = inside.slice();
  assert.equal(coneClamp(inside, [0, 0, 0], 1, [0, -1, 0], Math.PI / 4), false);
  assert.ok(near(inside, before));
  const back = [0, 1, 0]; // straight against the rest direction: any side, still on the cone
  assert.equal(coneClamp(back, [0, 0, 0], 1, [0, -1, 0], Math.PI / 6), true);
  assert.ok(Math.abs(Math.acos(-back[1]) - Math.PI / 6) < 1e-9);
  // the same for any rest direction (lying down in sleep: rest is horizontal)
  for (const rest of [[1, 0, 0], [0, 0, 1], [0, 1, 0], [Math.SQRT1_2, 0, -Math.SQRT1_2]]) {
    const p = rest.map((v) => -v * 0.7); // exactly opposite, off-length
    assert.equal(coneClamp(p, [0, 0, 0], 0.7, rest, Math.PI / 6), true);
    assert.ok(Math.abs(len3(p) - 0.7) < 1e-12, `on the bone length for rest ${rest}`);
    const c = (p[0] * rest[0] + p[1] * rest[1] + p[2] * rest[2]) / 0.7;
    assert.ok(Math.abs(Math.acos(Math.min(1, c)) - Math.PI / 6) < 1e-9, `on the cone for rest ${rest}`);
  }
});

test('spring colliders: sphere, capsule and floor push the tail out (plus the hair radius), length kept', () => {
  const sph = { c: [0, -1, 0], r: 0.3 };
  const args = { cur: [0.05, -0.95, 0], prev: [0.05, -0.95, 0], hitRadius: 0.02 };
  const out = springStep([0, 0, 0], base({ ...args, colliders: [sph] }));
  const free = springStep([0, 0, 0], base({ ...args, colliders: [] }));
  // One push per step then the bone length is restored, so a sphere sitting on the
  // tail's own circle can't be cleared in one step; the push must still do most of it.
  const gap = (o) => len3(o, sph.c) - 0.32;
  assert.ok(gap(free) < -0.25, `without the collider the tail is deep inside (${gap(free).toFixed(3)})`);
  assert.ok(gap(out) > -0.05, `the collider pushes it (nearly) out (${gap(out).toFixed(3)})`);
  assert.ok(Math.abs(len3(out) - 1) < 1e-12);
  // a sphere off to the side is cleared completely
  const side = { c: [0.3, -0.85, 0], r: 0.2 };
  const o3 = springStep([0, 0, 0], base({ cur: [0.12, -0.99, 0], prev: [0.12, -0.99, 0], colliders: [side] }));
  assert.ok(len3(o3, side.c) >= 0.2 - 1e-9 || Math.abs(len3(o3) - 1) > 1e-9, 'out of the side sphere');
  const p = [0.05, -0.5, 0];
  assert.equal(collidePoint(p, { a: [0, 0, 0], b: [0, -1, 0], r: 0.1 }, 0.02), true);
  assert.ok(Math.abs(Math.hypot(p[0], p[2]) - 0.12) < 1e-12 && Math.abs(p[1] + 0.5) < 1e-12, 'radially out of the capsule');
  const end = [0, -1.05, 0]; // past the capsule's end cap: pushed along the axis
  collidePoint(end, { a: [0, 0, 0], b: [0, -1, 0], r: 0.1 });
  assert.ok(Math.abs(end[1] + 1.1) < 1e-12);
  const floor = [0.3, -0.2, 0.1];
  assert.equal(collidePoint(floor, { floorY: 0 }, 0.01), true);
  assert.ok(near(floor, [0.3, 0.01, 0.1]));
  assert.equal(collidePoint([0, 0.5, 0], { floorY: 0 }), false);
  assert.equal(collidePoint([0, -1, 0], { c: [0, -1, 0], r: 0.3 }), false, 'dead centre: no direction, left alone');
  assert.ok(near(closestOnSegment([0, 0, 0], [5, 5, 0], [0, 0, 0], [0, 1, 0]), [0, 1, 0]));
  assert.ok(near(closestOnSegment([0, 0, 0], [5, 0.5, 0], [0, 0, 0], [0, 1, 0]), [0, 0.5, 0]));
});

test('spring reset: a hang start has no velocity, so the first step barely moves; sub-steps are fixed', () => {
  const h = springHang([0, 0, 0], base({ restDir: [1, 0, 0], stiffness: 3, gravity: 0.5 }));
  const cur = h.slice(); const prev = h.slice(); // what a reset writes: cur = prev = hang
  const out = springStep([0, 0, 0], base({ cur, prev, restDir: [1, 0, 0], stiffness: 3, gravity: 0.5, drag: 0.5 }));
  assert.ok(len3(out, h) < 1e-9, 'equilibrium: no jump after a reset');
  const hang2 = springHang([0, 0, 0], base({ restDir: [1, 0, 0], stiffness: 0, gravity: 0 }));
  assert.ok(near(hang2, [1, 0, 0]), 'no forces: hang = rest direction');
  // the hang respects the cone the step enforces: no pop when the sim takes over
  const coned = { restDir: [1, 0, 0], stiffness: 1.5, gravity: 0.5, maxAngle: 15 * Math.PI / 180 };
  const h3 = springHang([0, 0, 0], base(coned)); // unclamped it would sit 18.4° below rest
  assert.ok(Math.abs(Math.acos(h3[0]) - coned.maxAngle) < 1e-9, 'hang clamped to the cone');
  const s3 = springStep([0, 0, 0], base({ ...coned, cur: h3.slice(), prev: h3.slice(), drag: 0.5 }));
  assert.ok(len3(s3, h3) < 1e-9, 'first simulated step after a coned hang: no jump');
  assert.deepEqual(springSubsteps(0, 1 / 60), { n: 1, acc: 0 });
  let r = springSubsteps(0, 1 / 120); assert.equal(r.n, 0); r = springSubsteps(r.acc, 1 / 120); assert.equal(r.n, 1, '120 Hz: a step every other frame');
  assert.equal(springSubsteps(0, 5).n, 6, 'dt clamped to 0.1 s → at most 6 steps');
  assert.equal(springSubsteps(0, -1).n, 0);
  assert.equal(springSubsteps(0, NaN).n, 0);
  assert.equal(poseJumped({ moved: 0.01, turned: 0.1 }), false);
  assert.equal(poseJumped({ moved: 0.5 }), true, 'teleport');
  assert.equal(poseJumped({ turned: 1.5 }), true, 'snap turn');
  // limits scale with long frames: a 10 fps phone's fast turn (0.98 rad in one frame) still simulates
  assert.equal(poseJumped({ turned: 1.2, dt: 0.1 }), false);
  assert.equal(poseJumped({ moved: 0.4, dt: 0.1 }), false);
  assert.equal(poseJumped({ turned: 7, dt: 0.1 }), true, 'a real snap is still caught');
  assert.ok(SPRING.long.maxAngle > 0 && SPRING.long.maxAngle <= 20 && SPRING.lock.maxAngle > 0, 'cones on (no fanning)');
});

test('pickFidget never repeats the previous fidget', () => {
  const names = ['a', 'b', 'c'];
  for (let i = 0; i < 50; i++) {
    const r = () => (i % 10) / 10;
    assert.notEqual(pickFidget(names, 'b', r), 'b');
  }
  assert.equal(pickFidget(['a'], 'a'), 'a');
  assert.equal(pickFidget([], null), null);
  assert.equal(pickFidget(names, null, () => 0.999999), 'c');
});

test('adaptive quality: resolution first, outlines last; outlines come back first', () => {
  assert.deepEqual(nextQuality({ avgFrameMs: 40, ratio: 1.75, cap: 1.75 }), { ratio: 1.5, outlines: true });
  assert.deepEqual(nextQuality({ avgFrameMs: 60, ratio: 1.75, cap: 1.75 }), { ratio: 1.25, outlines: true }, 'very slow: big step');
  assert.deepEqual(nextQuality({ avgFrameMs: 60, ratio: 1.25, cap: 1.75 }), { ratio: 1, outlines: true });
  assert.deepEqual(nextQuality({ avgFrameMs: 40, ratio: 1, cap: 1.75 }), { ratio: 1, outlines: false });
  assert.deepEqual(nextQuality({ avgFrameMs: 40, ratio: 1, cap: 1.75, outlines: false }), { ratio: 1, outlines: false });
  assert.deepEqual(nextQuality({ avgFrameMs: 8, ratio: 1, cap: 1.75, outlines: false }), { ratio: 1, outlines: true });
  assert.deepEqual(nextQuality({ avgFrameMs: 8, ratio: 1, cap: 1.75, outlines: true }), { ratio: 1.25, outlines: true });
  assert.deepEqual(nextQuality({ avgFrameMs: 16.7, ratio: 1.5, cap: 2 }), { ratio: 1.5, outlines: true });
  assert.deepEqual(nextQuality({ avgFrameMs: 8, ratio: 1, cap: 2, outlines: false, hasOutlines: false }), { ratio: 1.25, outlines: false });
});

test('the stub (no model / no WebGL) has the whole API, setTint and setInsets included', async () => {
  const errors = [];
  const av = await createAvatar3D({}, { onError: (e) => errors.push(e.message) });
  assert.equal(av.ready, false);
  assert.equal(errors.length, 1);
  for (const m of ['setState', 'setMood', 'setSpeaking', 'setFraming', 'setPaused', 'setTint', 'setInsets', 'resize', 'dispose']) {
    assert.equal(typeof av[m], 'function', m);
    assert.doesNotThrow(() => av[m]('x'), m);
  }
  assert.equal(av.state, 'idle');
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

import {
  nextPixelRatio, warmAvatar3D, nextQuality, lookSplit, LOOK_WEIGHT, parseTint, RIM_TINTS, moodGate, STRONG_MOODS, MOOD_HOLD_MS,
  createStateMachine, stepEnvelope, nodCurve, blinkCurve, pickFidget, createAvatar3D, softLimit, EYE_GUARD,
  springStep, springHang, springSubsteps, poseJumped, collidePoint, closestOnSegment, coneClamp, SPRING,
} from '../../../flutter_app/web/companion/avatar3d.js';
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
