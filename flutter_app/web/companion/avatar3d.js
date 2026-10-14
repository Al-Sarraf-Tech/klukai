// Klukai 3D avatar window: her rigged model idling and reacting while you chat.
//
// ES module, no bundler. three.js is vendored as ./vendor/three-avatar.min.js (no CDN — privacy,
// Brave shields, offline). The GLB is personal-use, game-derived content: it is
// never committed and only reaches the page through `loadModel()` (the app's
// authenticated API), never a public URL.
//
//   import { createAvatar3D, moodToState } from './avatar3d.js';
//   const av = await createAvatar3D(el, { loadModel: () => api.avatarGlb(), onReady, onError });
//   av.setMood('quietly_pleased'); av.setSpeaking(level); av.setState('greet');
//   av.setTint('#ffb3c8');            // mood rim light colour (eases over ~1.2 s)
//   av.setInsets({ top, bottom });    // CSS px of the canvas hidden under the topbar / dock
//   av.zoomBy(0.85); av.rotateBy(Math.PI / 12); av.resetView();
//   av.setPaused(true); // parked behind another view: no render loop until setPaused(false)
//
// Look: two-band tinted toon (per-role shadow colours, baked AO from the GLB's
// occlusion map), face normals blended toward a sphere, a key-side rim plus a
// mood-tinted back rim, a hair "angel ring", and an inverted-hull outline drawn
// by a second SkinnedMesh per part (shared geometry, skeleton and blink morph).
// Life: look-at (camera, pointer on desktop), breathing, idle arm relax,
// speech-driven nods, blinks on gaze shifts and phrase ends, idle fidgets,
// mood hysteresis, eased camera, a contact shadow in full framing.
//
// The pure parts (moodToState, STATE_CLIPS, cameraFraming, lookSplit, …)
// import nothing heavy, so they run under `node --test` (tools/avatar/test).
// three.js is loaded lazily.

import { MOODS, nextBlink } from './logic.js';
import { createModelPrefetch, awaitWithAbort } from './appearance.js';

/** Viewer states. */
export const STATES = Object.freeze([
  'idle', 'talking', 'thinking', 'happy', 'bashful', 'sad', 'greet', 'sleep', 'excited', 'flustered',
]);

/**
 * State → GLB clip names (first = primary; extra entries are variants picked at
 * random for variety). `once: true` plays one-shot then settles back to idle.
 */
export const STATE_CLIPS = Object.freeze({
  idle: { clips: ['idle'], loop: true },
  talking: { clips: ['talking'], loop: true },
  thinking: { clips: ['thinking'], loop: true },
  happy: { clips: ['happy', 'thankful'], loop: true },
  bashful: { clips: ['bashful'], loop: true },
  sad: { clips: ['defeated'], loop: true },
  greet: { clips: ['salute'], loop: false, once: true },
  sleep: { clips: ['sleep'], loop: true },
  excited: { clips: ['excited'], loop: true },
  flustered: { clips: ['nervous', 'bashful'], loop: true },
});

// Every one of her moods → a viewer state. Kept explicit (not a fallback) so a
// test can assert each backend mood was considered.
const MOOD_STATE = Object.freeze({
  composed: 'idle',
  focused: 'thinking',
  prideful: 'idle',
  exasperated: 'idle',
  protective: 'idle',
  quietly_pleased: 'happy',
  competitive: 'excited',
  tender: 'bashful',
  longing: 'sad',
  battle_ready: 'idle',
  flustered: 'flustered',
  affectionate: 'happy',
  shy: 'bashful',
  yearning: 'bashful',
  devoted: 'happy',
  passionate: 'excited',
  jealous: 'idle',
  possessive: 'idle',
  smitten: 'bashful',
  infatuated: 'bashful',
  vigilant: 'thinking',
  calculating: 'thinking',
  hunting: 'thinking',
  adrenaline: 'excited',
  scared: 'flustered',
  terrified: 'flustered',
  panicked: 'flustered',
  desperate: 'sad',
  relieved: 'happy',
  content: 'happy',
  playful: 'happy',
  drowsy: 'sleep',
  amused: 'happy',
  bored: 'idle',
  excited: 'excited',
  melancholic: 'sad',
  haunted: 'sad',
  conflicted: 'thinking',
  guilty: 'sad',
  determined: 'idle',
  grieving: 'sad',
  furious: 'idle',
  nostalgic: 'thinking',
  curious: 'thinking',
  irritated: 'idle',
  defiant: 'idle',
  vulnerable: 'bashful',
  grateful: 'happy',
  worried: 'flustered',
  embarrassed: 'flustered',
});

/** Pure: one of her moods → viewer state (unknown/empty → 'idle'). */
export function moodToState(mood) {
  if (typeof mood !== 'string') return 'idle';
  return MOOD_STATE[mood.trim().toLowerCase()] || 'idle';
}

/** Pure: moods the mapping covers (for tests / diagnostics). */
export function mappedMoods() {
  return Object.keys(MOOD_STATE);
}

/** Pure: does the shared MOODS list (logic.js) match this mapping exactly? */
export function moodCoverage() {
  const mapped = new Set(Object.keys(MOOD_STATE));
  return {
    missing: MOODS.filter((m) => !mapped.has(m)),
    extra: [...mapped].filter((m) => !MOODS.includes(m)),
  };
}

const DEG = Math.PI / 180;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const smoothstep = (a, b, x) => { const t = clamp((x - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };
const wrapPi = (a) => Math.atan2(Math.sin(a), Math.cos(a));

/**
 * Pure: device-dependent renderer settings.
 *
 * MSAA is on everywhere, iOS included: Apple GPUs resolve 4× MSAA on-tile, so it
 * costs far less than more pixels, and the 1–1.3 px outlines crawl without it
 * (the old iOS opt-out dates from the iOS 15/16.4 ANGLE-Metal regressions and the
 * `high-performance` + AA WebKit bug, fixed in 2024; we stay on `low-power`).
 * Small screens cap at 1.75: with the full-height stage canvas a 390×844 phone
 * renders 683×1477 (~1.0 MP, vs 0.6 MP before); 1.75 + MSAA beats 2.0 without,
 * and the adaptive step can still go down to 1.
 */
export function renderSettings({ devicePixelRatio = 1, width = 1024, height = 768, isIOS = false, iosMajor = 0 } = {}) {
  const small = Math.min(width, height) < 500;
  // MSAA is resolved on-tile on Apple GPUs and thin outlines crawl without it.
  // iOS 15/16 had ANGLE-Metal MSAA regressions (black canvas, context loss):
  // keep those on the old no-AA, lower-resolution settings.
  const oldIOS = isIOS && iosMajor > 0 && iosMajor < 17;
  return {
    pixelRatio: Math.min(devicePixelRatio || 1, small ? (oldIOS ? 1.5 : 1.75) : 2),
    antialias: !oldIOS,
  };
}

/** Pure: the iOS/iPadOS major version from a user agent (0 = unknown / not iOS). */
export function iosMajorVersion(ua = globalThis.navigator?.userAgent || '') {
  const m = /\bOS (\d+)_\d+/.exec(ua) || (/Macintosh/.test(ua) && /\bVersion\/(\d+)/.exec(ua));
  return m ? Number(m[1]) : 0;
}

const BASE_FOV = 26;
const UPPER_PORTRAIT_FOV = 22; // telephoto close-up on phones: flatter, rounder anime face

// Framing as it was designed when the canvas was exactly the visible band
// between the topbar and the dock (at BASE_FOV).
function bandFraming({ framing, state, aspect, headY }) {
  const half = Math.tan((BASE_FOV / 2) * DEG); // visible half-height per metre
  const portrait = aspect < 0.8;
  if (state === 'sleep') { // lying on her back along −z (head at z≈−0.45): view from her side
    const dist = portrait ? 4.4 : 2.7;
    return { targetY: 0.15, camY: portrait ? 1.5 : 1.05, dist, side: true, targetZ: 0.1 };
  }
  if (framing === 'full') {
    if (!portrait) return { targetY: 0.79, camY: 0.95, dist: 3.35 };
    // ≥ 3.55 m: −0.02…1.62 m in view (the old 3.2 m floor cut her cap or feet at aspects 0.55–0.8)
    return { targetY: 0.8, camY: 0.95, dist: Math.max(3.55, 3.7 / Math.max(aspect / 0.5, 0.7)) };
  }
  // upper body: head + chest (landscape) / head to hips (portrait)
  const dist = portrait ? 1.9 : 1.35;
  const top = headY + 0.22; // cap crown + margin
  const targetY = top - dist * half;
  return { targetY, camY: targetY + 0.06, dist };
}

/**
 * Pure: camera placement for a framing. Units are metres; she stands ~1.52 m
 * tall with the head bone at ~1.33-1.38 m. Leaves headroom for the cap.
 *
 * `insets` = { top, bottom } as FRACTIONS of the canvas height that the topbar
 * and dock cover. The design framing is fitted into the visible band between
 * them (same size on screen as when the canvas stopped at the dock), the cap
 * sits a little below the top inset, and in upper framing the body keeps going
 * down under the dock's fade instead of stopping at a hard edge. Full framing
 * and sleep keep their feet clear of the fade. No insets → the old framing.
 * Upper framing on a portrait screen uses a 22° FOV, with the distance scaled
 * so the crop is unchanged.
 */
export function cameraFraming({ framing = 'upper', state = 'idle', aspect = 1, headY = 1.36, insets = null } = {}) {
  let top = clamp(Number(insets?.top) || 0, 0, 0.45);
  let bottom = clamp(Number(insets?.bottom) || 0, 0, 0.45);
  const lowBody = framing === 'full' || state === 'sleep';
  if (lowBody && bottom > 0) bottom = Math.min(0.5, bottom + 0.08);
  let band = 1 - top - bottom;
  if (band < 0.4) { const s = 0.6 / (top + bottom); top *= s; bottom *= s; band = 0.4; }
  const b = bandFraming({ framing, state, aspect: aspect / band, headY });
  const portrait = aspect / band < 0.8;
  const fov = !lowBody && portrait ? UPPER_PORTRAIT_FOV : BASE_FOV;
  const t = Math.tan((fov / 2) * DEG);
  const dist = (b.dist * Math.tan((BASE_FOV / 2) * DEG)) / (band * t);
  const half = dist * t; // half the canvas height in metres at the target
  const centre = (top + 1 - bottom) / 2; // band centre, 0 = canvas top
  const pad = !lowBody && top > 0 ? 0.03 * 2 * half : 0; // headroom under the topbar
  const targetY = b.targetY + (centre - 0.5) * 2 * half + pad;
  const out = { fov, targetY, camY: targetY + (b.camY - b.targetY) * (dist / b.dist), dist };
  if (b.side) { out.side = true; out.targetZ = b.targetZ; }
  return out;
}

/** User inspection offsets, relative to the animated automatic framing.
 *  Absorb only changes since the last camera placement: speech push-in and
 *  resize/state framing must never feed back into the user's zoom or orbit. */
export function createCameraView() {
  const maxYaw = 120 * DEG;
  const minZoom = 0.6, maxZoom = 1.8;
  const minPitch = -0.22, maxPitch = 0.12;
  let dTheta = 0, dPhi = 0, zoom = 1;
  let applied = null;
  return {
    get state() { return { dTheta, dPhi, zoom }; },
    absorb(observed) {
      if (!applied || !Number.isFinite(observed.radius) || observed.radius <= 0
          || !Number.isFinite(observed.theta) || !Number.isFinite(observed.phi)) return;
      dTheta = clamp(dTheta + wrapPi(observed.theta - applied.theta), -maxYaw, maxYaw);
      dPhi = clamp(dPhi + observed.phi - applied.phi, minPitch, maxPitch);
      zoom = clamp(zoom * observed.radius / applied.radius, minZoom, maxZoom);
      applied = { radius: observed.radius, theta: observed.theta, phi: observed.phi };
    },
    place(base, pushIn = 0) {
      const radius = base.radius * (1 - 0.03 * clamp(pushIn, 0, 1));
      applied = { radius: radius * zoom, theta: base.theta + dTheta, phi: base.phi + dPhi };
      return {
        ...applied,
        minDistance: radius * minZoom, maxDistance: radius * maxZoom,
        minAzimuthAngle: base.theta - maxYaw, maxAzimuthAngle: base.theta + maxYaw,
        minPolarAngle: base.phi + minPitch, maxPolarAngle: base.phi + maxPitch,
      };
    },
    zoomBy(factor) { if (Number.isFinite(factor) && factor > 0) zoom = clamp(zoom * factor, minZoom, maxZoom); },
    rotateBy(radians) { if (Number.isFinite(radians)) dTheta = clamp(dTheta + radians, -maxYaw, maxYaw); },
    reset() { dTheta = 0; dPhi = 0; zoom = 1; applied = null; },
  };
}

export function detectIOS(nav = globalThis.navigator) {
  if (!nav) return false;
  const ua = nav.userAgent || '';
  return /iP(hone|ad|od)/.test(ua) || (/Macintosh/.test(ua) && (nav.maxTouchPoints || 0) > 1);
}

/** Pure: adaptive resolution step. Returns the next pixel ratio given the
 *  recent average frame time (ms), the current ratio and the device cap. */
export function nextPixelRatio({ avgFrameMs, ratio, cap, min = 1 }) {
  if (avgFrameMs > 24 && ratio > min) return Math.max(min, +(ratio - 0.25).toFixed(2)); // < ~42 fps: drop
  if (avgFrameMs < 12 && ratio < cap) return Math.min(cap, +(ratio + 0.25).toFixed(2)); // lots of headroom: raise
  return ratio;
}

/** Pure: adaptive quality step. Resolution goes first; outlines are dropped only
 *  once the ratio is at its floor and frames are still slow, and come back first
 *  when there is headroom again. */
export function nextQuality({ avgFrameMs, ratio, cap, min = 1, outlines = true, hasOutlines = true }) {
  const on = hasOutlines && outlines;
  if (avgFrameMs > 24) {
    if (ratio > min && avgFrameMs > 45) return { ratio: Math.max(min, +(ratio - 0.5).toFixed(2)), outlines: on }; // < ~22 fps: big step
    if (ratio > min) return { ratio: nextPixelRatio({ avgFrameMs, ratio, cap, min }), outlines: on };
    return { ratio, outlines: false }; // at the floor and still slow
  }
  if (avgFrameMs < 12 && hasOutlines && !outlines) return { ratio, outlines: true }; // headroom: outlines first
  return { ratio: nextPixelRatio({ avgFrameMs, ratio, cap, min }), outlines: on };
}

/** Per-state weight of the look-at layer (1 = fully toward the camera). Shy and
 *  nervous states keep most of the clip's own glances away. */
export const LOOK_WEIGHT = Object.freeze({
  idle: 1, talking: 0.8, happy: 0.7, thinking: 0.35, bashful: 0.4, sad: 0.3, greet: 0.6, sleep: 0, excited: 0.6, flustered: 0.3,
});

/**
 * Pure: clamp a look correction (radians; yaw + = toward +x, pitch + = up) and
 * split it over the bones. With a skinned Head joint: neck 40 % / head 60 %,
 * yaw ±35°, pitch −15°…+20°. Without one (the head mesh rides on the Neck):
 * everything on the neck, yaw ±25°, pitch −15°…+15°.
 */
export function lookSplit({ yaw = 0, pitch = 0, headJoint = false } = {}) {
  const yawMax = (headJoint ? 35 : 25) * DEG;
  const up = (headJoint ? 20 : 15) * DEG;
  const down = 15 * DEG;
  const y = clamp(Number(yaw) || 0, -yawMax, yawMax);
  const p = clamp(Number(pitch) || 0, -down, up);
  if (headJoint) return { neck: { yaw: y * 0.4, pitch: p * 0.4 }, head: { yaw: y * 0.6, pitch: p * 0.6 } };
  return { neck: { yaw: y, pitch: p }, head: { yaw: 0, pitch: 0 } };
}

/** Eye guard (degrees): camera elevation above the face's own plane where the
 *  lift starts, and the soft ceiling it eases toward (irises stay under the brim);
 *  both come down by `side` when he sees her face from the side (≥ 70° azimuth).
 *  Calibrated with tools/avatar/debug/eyes.py: ≤ 0° both irises clear the brim,
 *  3–5° the brim covers their upper half, ≥ 8° they are gone. */
export const EYE_GUARD = Object.freeze({ start: -3, max: 1, side: 6 });

/** Pure: soft ceiling. x ≤ start passes through; above it the value eases toward
 *  `max` (tanh), with a continuous slope at `start`. */
export function softLimit(x, start, max) {
  if (!(x > start)) return x;
  const span = max - start;
  return span > 0 ? start + span * Math.tanh((x - start) / span) : start;
}

/** Saturated rim colours per portrait expression (app.js `moodToExpression`). */
export const RIM_TINTS = Object.freeze({ base: 0x8fd8ff, smile: 0xffb3c8, blush: 0xff9fbf, annoyed: 0xffb27a });

/** Pure: a tint → 0xRRGGBB, or null if it isn't one. Accepts a number, '#rrggbb',
 *  '#rgb', 'rgb()/rgba()' (alpha ignored) or a RIM_TINTS key ('smile'). */
export function parseTint(c) {
  if (typeof c === 'number') return Number.isInteger(c) && c >= 0 && c <= 0xffffff ? c : null;
  if (typeof c !== 'string') return null;
  const s = c.trim().toLowerCase();
  if (Object.hasOwn(RIM_TINTS, s)) return RIM_TINTS[s];
  let m = s.match(/^#([0-9a-f]{6})$/);
  if (m) return parseInt(m[1], 16);
  m = s.match(/^#([0-9a-f]{3})$/);
  if (m) return parseInt([...m[1]].map((x) => x + x).join(''), 16);
  m = s.match(/^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(?:,\s*[\d.]+%?\s*)?\)$/);
  if (m) {
    const [r, g, b] = m.slice(1, 4).map(Number);
    if (r <= 255 && g <= 255 && b <= 255) return (r << 16) | (g << 8) | b;
  }
  return null;
}

/** Moods that switch her state at once, even inside the hysteresis window. */
export const STRONG_MOODS = Object.freeze([
  'scared', 'terrified', 'panicked', 'furious', 'adrenaline', 'desperate', 'grieving', 'flustered', 'embarrassed',
]);
export const MOOD_HOLD_MS = 4000;

/**
 * Pure: mood hysteresis. The backend changes mood often; a mood-driven state
 * holds for MOOD_HOLD_MS unless the new mood is strong. A state set directly
 * (setState: thinking, talking, greet, …) is never held: the next mood always
 * leaves it at once. → 'same' | 'apply' | 'defer'.
 */
export function moodGate({ now, lastAt = -Infinity, fromMood = true, current, next, strong = false, holdMs = MOOD_HOLD_MS }) {
  if (next === current) return 'same';
  if (!fromMood || strong) return 'apply';
  return now - lastAt >= holdMs ? 'apply' : 'defer';
}

/**
 * The viewer's state bookkeeping (pure; the 3D view supplies `enter`).
 * setState(name) is always immediate and marks the state as set directly;
 * setMood(mood) goes through moodGate(), so mood-to-mood changes may be held
 * (the latest held mood is applied by tick() when the hold ends), but a state
 * set directly — thinking, talking, greet, … — is always left at once.
 * `enter(next, prev, source)` runs on every real change.
 */
export function createStateMachine({ enter = () => {}, now = () => Date.now(), holdMs = MOOD_HOLD_MS } = {}) {
  let state = 'idle';
  let source = 'mood';
  let lastMoodAt = -Infinity;
  let pending = null;
  let mood = '';
  const go = (next, src) => {
    const prev = state;
    state = next; source = src;
    if (src === 'mood') lastMoodAt = now();
    enter(next, prev, src);
  };
  const sm = {
    get state() { return state; },
    get source() { return source; },
    get mood() { return mood; },
    get pending() { return pending; },
    setState(name) {
      if (!STATES.includes(name)) return false;
      pending = null;
      go(name, 'state');
      return true;
    },
    setMood(m) {
      mood = typeof m === 'string' ? m.trim().toLowerCase() : '';
      const next = moodToState(m);
      const d = moodGate({ now: now(), lastAt: lastMoodAt, fromMood: source === 'mood', current: state, next, strong: STRONG_MOODS.includes(mood), holdMs });
      if (d === 'defer') { pending = m; return d; }
      pending = null;
      if (d === 'same') source = 'mood';
      else go(next, 'mood');
      return d;
    },
    /** Apply a held mood once its hold is over. */
    tick() {
      if (pending === null || now() - lastMoodAt < holdMs) return null;
      const m = pending; pending = null;
      return sm.setMood(m);
    },
    /** A one-shot clip ended: she is now `name` (no enter(), keeps the source). */
    settle(name) { if (STATES.includes(name)) state = name; },
  };
  return sm;
}

/**
 * Pure: speech envelope follower. `fast` tracks syllables, `slow` the phrase
 * level; an onset — a rising edge well above the phrase level — triggers a
 * nod. One per edge (re-armed once the level falls back toward the phrase
 * level) and at most one per 280 ms. Returns the next state.
 */
export function stepEnvelope(env, level, dt) {
  const prev = env || { fast: 0, slow: 0, since: 1, armed: true };
  const l = clamp(Number(level) || 0, 0, 1);
  const k = (tc) => 1 - Math.exp(-Math.max(0, dt) / tc);
  const fast = prev.fast + (l - prev.fast) * k(l > prev.fast ? 0.03 : 0.12);
  const slow = prev.slow + (l - prev.slow) * k(0.45);
  const since = prev.since + Math.max(0, dt);
  const rise = fast - slow;
  const onset = prev.armed !== false && since > 0.28 && fast > 0.12 && rise > 0.08;
  const armed = onset ? false : prev.armed !== false || rise < 0.03;
  return { fast, slow, since: onset ? 0 : since, onset, armed };
}

/** Pure: nod impulse — rises to `amp` at t = tau (s), then falls off fast
 *  (~17 % at 3·tau, gone by ~5·tau). */
export function nodCurve(t, amp, tau = 0.1) {
  if (!(t >= 0)) return 0;
  const x = t / tau;
  return x > 8 ? 0 : amp * x * x * Math.exp(2 * (1 - x));
}

/** Pure: eyelid 0..1 over a blink, t in ms (fast close, slower open); -1 when done.
 *  `slow` is the ~0.3 s tender blink. */
export function blinkCurve(t, slow = false) {
  const k = slow ? 2.2 : 1;
  const close = 55 * k; const hold = 25 * k; const open = 90 * k;
  if (t < 0) return 0;
  if (t < close) return t / close;
  if (t < close + hold) return 1;
  if (t < close + hold + open) return 1 - (t - close - hold) / open;
  return -1;
}

// ── spring-bone hair (three-vrm VRMSpringBoneJoint on plain bones) ─────────
// Vectors are [x, y, z] arrays in centre space (the character root), metres.

/** Pure: closest point to `p` on segment a–b, written into `out`. */
export function closestOnSegment(out, p, a, b) {
  const ab0 = b[0] - a[0]; const ab1 = b[1] - a[1]; const ab2 = b[2] - a[2];
  const l2 = ab0 * ab0 + ab1 * ab1 + ab2 * ab2;
  let t = l2 > 1e-12 ? ((p[0] - a[0]) * ab0 + (p[1] - a[1]) * ab1 + (p[2] - a[2]) * ab2) / l2 : 0;
  t = t < 0 ? 0 : t > 1 ? 1 : t;
  out[0] = a[0] + ab0 * t; out[1] = a[1] + ab1 * t; out[2] = a[2] + ab2 * t;
  return out;
}

const _cp = [0, 0, 0];
/** Pure: push point `p` (in place) out of a sphere { c, r }, capsule { a, b, r } or
 *  floor { floorY }, plus `hit` (the hair's own thickness). Returns true when it moved. */
export function collidePoint(p, col, hit = 0) {
  if (col.floorY !== undefined) {
    if (p[1] >= col.floorY + hit) return false;
    p[1] = col.floorY + hit;
    return true;
  }
  const c = col.b ? closestOnSegment(_cp, p, col.a, col.b) : (col.c || col.a);
  const d0 = p[0] - c[0]; const d1 = p[1] - c[1]; const d2 = p[2] - c[2];
  const r = col.r + hit;
  const dist = Math.hypot(d0, d1, d2);
  if (dist >= r || dist < 1e-9) return false;
  const k = r / dist;
  p[0] = c[0] + d0 * k; p[1] = c[1] + d1 * k; p[2] = c[2] + d2 * k;
  return true;
}

/** Pure: keep a tail within `maxAngle` (rad) of the rest direction, on the bone
 *  length (in place). Long hair can't fold back or twist over on a fast turn. */
export function coneClamp(p, head, len, restDir, maxAngle) {
  if (!(maxAngle > 0) || maxAngle >= Math.PI) return false;
  let d0 = p[0] - head[0]; let d1 = p[1] - head[1]; let d2 = p[2] - head[2];
  const l = Math.hypot(d0, d1, d2);
  if (l < 1e-9) return false;
  d0 /= l; d1 /= l; d2 /= l;
  const c = d0 * restDir[0] + d1 * restDir[1] + d2 * restDir[2];
  const cMax = Math.cos(maxAngle);
  if (c >= cMax) return false;
  // component of d orthogonal to rest, normalised; then rest·cos + ortho·sin
  let o0 = d0 - restDir[0] * c; let o1 = d1 - restDir[1] * c; let o2 = d2 - restDir[2] * c;
  let ol = Math.hypot(o0, o1, o2);
  if (ol < 1e-9) { // straight back: any side, as long as it is orthogonal to rest
    const ax = Math.abs(restDir[1]) < 0.9 ? [0, 1, 0] : [1, 0, 0];
    o0 = restDir[1] * ax[2] - restDir[2] * ax[1]; o1 = restDir[2] * ax[0] - restDir[0] * ax[2]; o2 = restDir[0] * ax[1] - restDir[1] * ax[0];
    ol = Math.hypot(o0, o1, o2);
  }
  const sMax = Math.sin(maxAngle);
  p[0] = head[0] + (restDir[0] * cMax + (o0 / ol) * sMax) * len;
  p[1] = head[1] + (restDir[1] * cMax + (o1 / ol) * sMax) * len;
  p[2] = head[2] + (restDir[2] * cMax + (o2 / ol) * sMax) * len;
  return true;
}

function keepLength(p, head, len) {
  const d0 = p[0] - head[0]; const d1 = p[1] - head[1]; const d2 = p[2] - head[2];
  const l = Math.hypot(d0, d1, d2);
  if (l < 1e-9) return;
  const k = len / l;
  p[0] = head[0] + d0 * k; p[1] = head[1] + d1 * k; p[2] = head[2] + d2 * k;
}

/**
 * Pure: one fixed step of a spring bone's tail (three-vrm VRMSpringBoneJoint):
 * inertia (cur − prev)·(1 − drag) + stiffness·dt toward the rest direction +
 * gravity·dt, then the bone length is restored, the tail is kept within
 * `maxAngle` of the rest direction (0 = no limit) and pushed out of every
 * collider (length restored again). Writes and returns `out`.
 */
export function springStep(out, { cur, prev, head, len, restDir, stiffness, drag, gravity, gravityDir = [0, -1, 0], dt, colliders = [], hitRadius = 0, maxAngle = 0 }) {
  const keep = 1 - drag;
  for (let k = 0; k < 3; k++) {
    out[k] = cur[k] + (cur[k] - prev[k]) * keep + restDir[k] * stiffness * dt + gravityDir[k] * gravity * dt;
  }
  keepLength(out, head, len);
  coneClamp(out, head, len, restDir, maxAngle);
  for (const c of colliders) if (collidePoint(out, c, hitRadius)) keepLength(out, head, len);
  return out;
}

/** Pure: where the tail settles with no motion (the step's fixed point):
 *  along stiffness·restDir + gravity·down, out of the colliders. For resets and
 *  the no-physics fallback (reduced motion), so chains hang instead of sticking out. */
export function springHang(out, { head, len, restDir, stiffness, gravity, gravityDir = [0, -1, 0], colliders = [], hitRadius = 0, maxAngle = 0 }) {
  for (let k = 0; k < 3; k++) out[k] = head[k] + (restDir[k] * stiffness + gravityDir[k] * gravity);
  keepLength(out, head, len);
  if (Math.hypot(out[0] - head[0], out[1] - head[1], out[2] - head[2]) < 1e-9) for (let k = 0; k < 3; k++) out[k] = head[k] + restDir[k] * len;
  coneClamp(out, head, len, restDir, maxAngle); // same limit as springStep: no pop when the sim takes over
  for (const c of colliders) if (collidePoint(out, c, hitRadius)) keepLength(out, head, len);
  return out;
}

/** Pure: fixed 1/60 s sub-steps for a frame. dt is clamped to 0.1 s and at most
 *  `max` steps run; the remainder carries over in `acc`. */
export function springSubsteps(acc, dt, h = 1 / 60, max = 6) {
  let a = acc + Math.min(Math.max(Number(dt) || 0, 0), 0.1);
  let n = Math.floor(a / h + 1e-9);
  a -= n * h;
  if (n > max) { n = max; a = 0; }
  return { n, acc: a };
}

/** Pure: did the head jump (teleport, resumed tab, state snap) so far in one frame
 *  that the springs must be reset rather than simulated? The limits are per 60 Hz
 *  frame and scale with longer frames, so a slow phone's fast turn still simulates. */
export function poseJumped({ moved = 0, turned = 0, dt = 1 / 60 } = {}) {
  const f = Math.max(1, (Number(dt) || 0) * 60);
  return moved > 0.25 * f || turned > 1.0 * f;
}

const SPRING_H = 1 / 60; // fixed spring sub-step (s)

/** Per-chain settings (stiffness root → tip; maxAngle in degrees from the rest
 *  direction). Ponytail and back hair overlap, so they share one set. The cone is per
 *  joint, so a 5-bone ponytail can still curl 5×; softer, less damped or wider (0.9→0.35
 *  stiffness, 0.42 drag, 35° tried) fans the whole back mass out on nervous' 560°/s turns. */
export const SPRING = Object.freeze({
  long: { stiffness: [3.0, 1.5], gravity: 0.5, drag: 0.5, hitRadius: 0.03, maxAngle: 15 }, // ponytail, back
  lock: { stiffness: [2.0, 1.2], gravity: 0.3, drag: 0.5, hitRadius: 0.015, maxAngle: 25 }, // side locks
});

/** Pure: next idle fidget, never the same one twice in a row. */
export function pickFidget(names, prev, rand = Math.random) {
  const pool = names.length > 1 ? names.filter((n) => n !== prev) : names.slice();
  if (!pool.length) return null;
  return pool[Math.min(pool.length - 1, Math.floor(rand() * pool.length))];
}

let libsPromise = null;
function importLibs(withOrbit = true) {
  if (!libsPromise) {
    // One tree-shaken bundle (tools/avatar/vendor.mjs): three core subset +
    // GLTFLoader + MeshoptDecoder + OrbitControls.
    libsPromise = import('./vendor/three-avatar.min.js')
      .then((m) => ({ THREE: m, GLTFLoader: m.GLTFLoader, MeshoptDecoder: m.MeshoptDecoder, OrbitControls: m.OrbitControls }));
    libsPromise.catch(() => { libsPromise = null; });
  }
  void withOrbit;
  return libsPromise;
}

const modelPrefetch = createModelPrefetch();
/**
 * Optional: call as soon as the 3D window is the selected mode (e.g. at page
 * init when the saved preference is 3D) to start the three.js imports and the
 * model download while the rest of the page boots. The next createAvatar3D()
 * reuses both. Safe to call repeatedly; never throws.
 */
export function warmAvatar3D({ loadModel, modelKey = 'default' } = {}) {
  importLibs().catch(() => {});
  if (typeof loadModel === 'function') modelPrefetch.warm(modelKey, loadModel);
}

// Idle fidgets: a short one-shot after a full idle cycle (35 %), never the
// same twice in a row. Sub-clips of the expressive loops, at full weight.
const FIDGETS = Object.freeze([
  { name: 'thankful' },
  { name: 'fidget_happy', from: 'happy', start: 1.0, end: 4.0 },
  { name: 'fidget_think', from: 'thinking', start: 0.2, end: 3.2 },
]);

const ROLE_FALLBACK = (name) => (/hair/i.test(name) ? 'hair' : /face|eye/i.test(name) ? 'face' : /cloth/i.test(name) ? 'cloth' : 'skin');

// ── look constants (tuned on the dark stage; see tools/avatar/README) ───────
// Two-band toon: shadow = uShadowTint × key (linear), threshold on half-Lambert × AO.
const SHADE = Object.freeze({
  // skin/face: soft peach-rose shadows that keep the lit hue (the official art's skin shadow
  // is a lighter, less saturated version of the lit tone); R1's 0.62/0.40/0.38 read brick-red
  // on the dark stage
  skin: { tint: [0.70, 0.62, 0.60], th: 0.50, soft: 0.03, rim: 0.4 },
  face: { tint: [0.80, 0.70, 0.70], th: 0.30, soft: 0.10, rim: 0.2 },
  hair: { tint: [0.48, 0.54, 0.70], th: 0.50, soft: 0.025, rim: 1.0 }, // silver, not slate (R1 had 0.40/0.46/0.62)
  cloth: { tint: [0.36, 0.33, 0.48], th: 0.50, soft: 0.025, rim: 1.0 },
});
// Inverted hull: width in CSS px, view-space push (m), colour = texture × tint.
const OUTLINE = Object.freeze({
  hair: { px: 1.3, z: 0.004, tint: 0x4a5a78 },
  cloth: { px: 1.3, z: 0.002, tint: 0x3a3048 },
  skin: { px: 1.0, z: 0.002, tint: 0x7a5a58 }, // muted rose-brown (0x8a4a48 read as red lines)
  face: { px: 0.9, z: 0.003, tint: 0x7a5a58 },
});
const LIGHT = Object.freeze({
  sky: 0xe6eaff, ground: 0x3b3550, hemi: 1.0, // fill
  key: 0xffffff, keyIntensity: 2.45, keyPos: [-1.4, 2.25, 1.8], // neutral (warm read peachy on her pale skin), front-left, high
  rimDir: [0.6, 0.5, -0.6], rimStrength: 0.75, keyRim: 0.55, // mood rim from back-right-top
});

// ── shader patches (identical code for every toon material; features via defines) ──
const SWAY_VERT_PARS = `
#ifdef KL_SWAY
uniform float uTime; uniform vec2 uSway; uniform vec4 uSwayBox; uniform float uSwayAmp;
#endif`;
const TOON_VERT_PARS = `
uniform vec3 uHeadCenter;
#ifdef KL_RING
varying vec3 vHeadDir;
#endif
${SWAY_VERT_PARS}`;
const TOON_VERT_NORMAL = `
#ifdef KL_RING
vHeadDir = position - uHeadCenter;
#endif
#ifdef KL_FACE
objectNormal = normalize( mix( objectNormal, normalize( ( position - uHeadCenter ) * vec3( 1.0, 1.0, 0.8 ) ), 0.65 ) );
#endif`;
const TOON_VERT_SWAY = `
#ifdef KL_SWAY
{
  float sw = 1.0 - smoothstep( uSwayBox.y, uSwayBox.x, position.y ); // 0 above the neck, 1 at the tips
  sw *= sw;
  float back = 1.0 - smoothstep( uSwayBox.w, uSwayBox.z, position.z ); // hair behind her back only
  transformed.x += sw * ( uSway.x + uSwayAmp * sin( uTime * 1.3 + position.y * 9.0 ) );
  transformed.z += sw * back * ( min( 0.0, uSway.y ) - uSwayAmp * ( 0.5 + 0.5 * sin( uTime * 0.9 + position.y * 7.0 ) ) );
}
#endif`;
const TOON_FRAG_PARS = `
uniform vec3 uShadowTint; uniform float uTh; uniform float uSoft;
uniform vec3 uRimColor; uniform float uRimStrength; uniform vec3 uRimDir; uniform float uKeyRim; uniform float uRimMul;
uniform vec2 uKeySide;
#ifdef KL_RING
varying vec3 vHeadDir;
#endif
#ifdef KL_AO
uniform sampler2D uAoMap;
#endif
#ifdef KL_METAL
uniform sampler2D uMetalMap;
#endif
float gAo = 1.0;`;
const TOON_LIGHTS = `
varying vec3 vViewPosition;
struct ToonMaterial { vec3 diffuseColor; };
void RE_Direct_Toon( const in IncidentLight directLight, const in vec3 geometryPosition, const in vec3 geometryNormal, const in vec3 geometryViewDir, const in vec3 geometryClearcoatNormal, const in ToonMaterial material, inout ReflectedLight reflectedLight ) {
  float nl = ( dot( geometryNormal, directLight.direction ) * 0.5 + 0.5 ) * gAo;
  float s = max( uSoft, fwidth( nl ) );
  float band = smoothstep( uTh - s, uTh + s, nl );
  reflectedLight.directDiffuse += mix( uShadowTint, vec3( 1.0 ), band ) * directLight.color * BRDF_Lambert( material.diffuseColor );
}
void RE_IndirectDiffuse_Toon( const in vec3 irradiance, const in vec3 geometryPosition, const in vec3 geometryNormal, const in vec3 geometryViewDir, const in vec3 geometryClearcoatNormal, const in ToonMaterial material, inout ReflectedLight reflectedLight ) {
  reflectedLight.indirectDiffuse += irradiance * mix( 1.0, gAo, 0.6 ) * BRDF_Lambert( material.diffuseColor );
}
#define RE_Direct RE_Direct_Toon
#define RE_IndirectDiffuse RE_IndirectDiffuse_Toon`;
const TOON_FRAG_AO = `
#ifdef KL_AO
  gAo = mix( 1.0, texture2D( uAoMap, vMapUv ).r, 0.85 );
#endif`;
const TOON_FRAG_FX = `
#if NUM_DIR_LIGHTS > 0
{
  vec3 Lv = directionalLights[ 0 ].direction;
  vec3 Lc = directionalLights[ 0 ].color;
  float facing = 1.0 - saturate( dot( normal, geometryViewDir ) );
  // key-side rim: stepped, only where the key comes from, tinted by albedo
  float side = saturate( dot( normalize( normal.xy + 1e-4 ), uKeySide ) * 1.5 - 0.2 );
  outgoingLight += smoothstep( 0.62, 0.67, facing ) * side * uKeyRim * uRimMul * diffuseColor.rgb * Lc * RECIPROCAL_PI;
  // mood rim from behind (back-right-top), stepped, coloured by setTint(); both rims fade toward
  // the face (uRimMul per role: GFL2 keeps material detail off the head)
  float rimM = smoothstep( 0.55, 0.61, facing ) * smoothstep( -0.05, 0.45, dot( normal, uRimDir ) );
  outgoingLight += rimM * uRimStrength * uRimMul * uRimColor * ( 0.35 + 0.65 * diffuseColor.rgb );
  #ifdef KL_RING
  {
    // angel ring: a latitude band on a sphere around the head (bind space, rides with the head),
    // sliding with camera elevation, made jagged by the strand texture
    float r = length( vHeadDir );
    float lat = vHeadDir.y / max( r, 1e-4 );
    float nearHead = 1.0 - smoothstep( 0.20, 0.27, r );
    vec3 upV = normalize( ( viewMatrix * vec4( 0.0, 1.0, 0.0, 0.0 ) ).xyz );
    float c = 0.32 - 0.25 * dot( geometryViewDir, upV );
    float x = lat + ( dot( diffuseColor.rgb, vec3( 0.333 ) ) - 0.7 ) * 0.45;
    float ring = smoothstep( c - 0.09, c - 0.05, x ) * ( 1.0 - smoothstep( c + 0.02, c + 0.06, x ) );
    ring *= nearHead * saturate( dot( normal, geometryViewDir ) * 2.5 ) * saturate( dot( normal, Lv ) * 2.0 + 0.6 );
    outgoingLight = outgoingLight * mix( 0.84, 1.0, ring ) + ring * vec3( 0.22, 0.24, 0.30 );
  }
  #endif
  #ifdef KL_METAL
  {
    // buckles and zips (metal mask): darker body, a hard stylised glint
    float mm = smoothstep( 0.45, 0.6, texture2D( uMetalMap, vMapUv ).b );
    vec3 H = normalize( Lv + geometryViewDir );
    float sp = smoothstep( 0.5, 0.58, pow( saturate( dot( normal, H ) ), 40.0 ) );
    outgoingLight = outgoingLight * ( 1.0 - 0.18 * mm ) + mm * sp * ( 0.3 + 0.7 * diffuseColor.rgb ) * Lc * 0.25;
  }
  #endif
}
#endif`;
const OUTLINE_VERT_PARS = `
attribute vec4 _outline;
uniform vec2 uRes; uniform vec2 uWClamp; uniform float uDpr; uniform float uPx; uniform float uZ;
#ifdef KL_FACING
varying float vFacing;
#endif`;
const OUTLINE_VERT_PROJECT = `
vec3 nV = normalize( normalMatrix * objectNormal ); // not transformedNormal: FLIP_SIDED negates it for BackSide
#ifdef KL_FACING
vFacing = dot( nV, normalize( -mvPosition.xyz ) );
#endif
mvPosition.xyz += normalize( mvPosition.xyz ) * uZ; // view-space push away from the camera (no polygonOffset)
gl_Position = projectionMatrix * mvPosition;
vec2 oDir = ( projectionMatrix * vec4( nV, 0.0 ) ).xy * uRes;
oDir = dot( oDir, oDir ) > 1e-10 ? normalize( oDir ) : vec2( 0.0 );
gl_Position.xy += oDir * ( 2.0 * uPx * uDpr * _outline.w / uRes ) * clamp( gl_Position.w, uWClamp.x, uWClamp.y );`;
// Skin and face are open meshes (skin under clothes is cut away in the build): through
// such a hole a BackSide hull shows the INSIDE of the far wall as a flat outline-
// coloured panel. Those faces look away from the camera; silhouette rims don't.
const OUTLINE_FRAG_PARS = `
#ifdef KL_FACING
varying float vFacing;
#endif`;
const OUTLINE_FRAG_CUT = `
#ifdef KL_FACING
if ( vFacing < -0.45 ) discard;
#endif`;
const SHADOW_VERT = `
varying vec2 vXZ;
void main() { vec4 wp = modelMatrix * vec4( position, 1.0 ); vXZ = wp.xz; gl_Position = projectionMatrix * viewMatrix * wp; }`;
const SHADOW_FRAG = `
uniform vec4 uHip; uniform vec4 uFeet; uniform vec4 uW; uniform vec3 uColor; uniform float uAlpha;
varying vec2 vXZ;
float blob( vec2 c, float r ) { vec2 d = ( vXZ - c ) / r; return exp( -2.0 * dot( d, d ) ); }
void main() {
  float a = uW.x * blob( uHip.xy, 0.28 ) + uW.y * blob( uHip.zw, 0.22 ) + uW.z * blob( uFeet.xy, 0.075 ) + uW.w * blob( uFeet.zw, 0.075 );
  gl_FragColor = vec4( uColor, min( a, 0.8 ) * uAlpha );
}`;

/**
 * Create the avatar view inside `container`.
 * @param {HTMLElement} container
 * @param {{ loadModel: () => Promise<ArrayBuffer>, onReady?: Function, onError?: Function,
 *           framing?: 'upper'|'full', orbit?: boolean, adaptiveResolution?: boolean,
 *           life?: boolean, outlines?: boolean, reducedMotion?: boolean }} opts
 *   `life: false` turns off the procedural layers (look-at, breathing, arms, nods,
 *   fidgets, drift, sway) for stable inspection renders.
 */
export async function createAvatar3D(container, opts = {}) {
  const { loadModel, onReady, onError } = opts;
  let framing = opts.framing === 'full' ? 'full' : 'upper';
  const fail = (err) => {
    try { onError?.(err instanceof Error ? err : new Error(String(err))); } catch { /* caller's handler threw */ }
  };
  // Returned whenever the view can't be built; `ready` is false so the caller
  // can tell it from a live avatar (and try again later instead of keeping it).
  const stub = {
    setState() {}, setMood() {}, setSpeaking() {}, setFraming() {}, setPaused() {}, setTint() {}, setInsets() {},
    zoomBy() {}, rotateBy() {}, resetView() {},
    resize() {}, dispose() {},
    get ready() { return false; }, get state() { return 'idle'; },
  };
  if (typeof loadModel !== 'function') { fail(new Error('createAvatar3D: loadModel() is required')); return stub; }

  const stats = { t0: performance.now() };
  const mark = (k) => { stats[k] = Math.round(performance.now() - stats.t0); };
  // Model download and library imports run in parallel (and may already be
  // in flight from warmAvatar3D()).
  const modelPromise = modelPrefetch.take(opts.modelKey || 'default', loadModel);
  modelPromise.then(() => mark('fetchMs'), () => {});
  let THREE; let GLTFLoader; let MeshoptDecoder; let OrbitControls;
  try {
    ({ THREE, GLTFLoader, MeshoptDecoder, OrbitControls } = await importLibs());
    if (opts.orbit === false) OrbitControls = null;
    mark('importMs');
  } catch (e) { modelPromise.catch(() => {}); fail(e); return stub; }

  // A superseded or stalled download must not retain a WebGL context.
  let modelBuffer;
  try { modelBuffer = await awaitWithAbort(modelPromise, opts.signal); }
  catch (e) { fail(e); return stub; }
  if (opts.signal?.aborted) return stub;
  if (!hasWebGL()) { fail(new Error('WebGL is not available')); return stub; }

  const isIOS = detectIOS();
  const size = () => ({ w: Math.max(1, container.clientWidth || 1), h: Math.max(1, container.clientHeight || 1) });
  let { w, h } = size();
  const iosMajor = isIOS ? iosMajorVersion() : 0;
  let rs = renderSettings({ devicePixelRatio: globalThis.devicePixelRatio, width: w, height: h, isIOS, iosMajor });

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: rs.antialias, alpha: true, premultipliedAlpha: true, powerPreference: 'low-power' });
  } catch (e) { fail(e); return stub; }
  renderer.setPixelRatio(rs.pixelRatio);
  renderer.setSize(w, h);
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace; // NoToneMapping (default): albedo 1:1 at "fully lit"
  const canvas = renderer.domElement;
  canvas.style.display = 'block';
  canvas.style.width = '100%';
  canvas.style.height = '100%';
  canvas.style.touchAction = 'pan-y';
  canvas.setAttribute('aria-label', 'Klukai');
  container.appendChild(canvas);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(BASE_FOV, w / h, 0.05, 30);
  // Two lights only (cheaper per pixel on phones), calibrated so the lit side
  // is ~1.0× albedo: a dim sky/ground fill and one warm key from the front-left
  // (keeps her face in its lit band). The back rim is a shader term (uRimDir).
  const hemi = new THREE.HemisphereLight(LIGHT.sky, LIGHT.ground, LIGHT.hemi);
  scene.add(hemi);
  const key = new THREE.DirectionalLight(LIGHT.key, LIGHT.keyIntensity);
  key.position.set(...LIGHT.keyPos);
  scene.add(key);
  const skyBase = new THREE.Color(LIGHT.sky);

  // ── state ────────────────────────────────────────────────────────────────
  let disposed = false;
  let ready = false;
  let paused = false;
  let userPaused = false; // api.setPaused(): parked behind another view
  let contextLost = false;
  // Assigned in the lifecycle section below, after the model loads — but
  // teardown() also runs from the load `catch`. Declared here (not `const`
  // down there) or that call hit the temporal dead zone, threw, and the
  // renderer/canvas were never disposed: one leaked WebGL context per retry.
  let onVisibility = null;
  let onLost = null;
  let onRestored = null;
  let onPointer = null;
  let onPointerLeave = null;
  let onMotionPref = null;
  let onControlStart = null;
  let onControlEnd = null;
  let ro = null;
  let rafId = 0;
  let frameAcc = 0; let frameN = 0; let fpsAvg = 0; let lastNow = 0; // adaptive resolution
  let root = null;
  let mixer = null;
  let skinned = [];
  let outlineMeshes = [];
  let outlinesOn = opts.outlines !== false;
  let head = null;
  let neck = null;
  let headJoint = false;
  const bones = {};
  const actions = new Map();
  const fidgetActions = new Map();
  let current = null; // { state, action, once, fidget }
  let talkAction = null;
  let talkWeight = 0;
  let speaking = 0;
  let speakingTarget = 0;
  let blinkIdx = -1;
  let blinkT = -1; // ms since blink start; <0 = waiting
  let blinkSlow = false;
  let lastBlinkAt = 0;
  let nextBlinkAt = 0;
  let pendingDouble = false;
  let controls = null;
  let stateName = 'idle'; // mirrors sm.state (createStateMachine below)
  let insetsPx = { top: 0, bottom: 0 };
  let shadowMesh = null;
  let springs = null; // hair spring chains (buildSprings), null without chains
  let simClock = performance.now() / 1000; // debug.run()'s synthetic clock (s)
  const timer = new THREE.Timer();
  timer.connect?.(document);
  const ownedTextures = new Set();
  const life = opts.life !== false;
  const motionMq = typeof globalThis.matchMedia === 'function' ? globalThis.matchMedia('(prefers-reduced-motion: reduce)') : null;
  let reducedMotion = opts.reducedMotion === true || Boolean(motionMq?.matches);

  // ── materials ────────────────────────────────────────────────────────────
  const shared = {
    uHeadCenter: { value: new THREE.Vector3(0, 1.38, 0) },
    uRimColor: { value: new THREE.Color(RIM_TINTS.base) },
    uRimStrength: { value: LIGHT.rimStrength },
    uRimDir: { value: new THREE.Vector3(...LIGHT.rimDir).normalize() },
    uKeySide: { value: new THREE.Vector2(-1, 0) }, // key light direction on screen (view xy), per frame
    uKeyRim: { value: LIGHT.keyRim },
    uTime: { value: 0 },
    uSway: { value: new THREE.Vector2() },
    uSwayBox: { value: new THREE.Vector4(1, 0, 0, -1) },
    uSwayAmp: { value: 0 },
  };
  const outlineU = {
    uRes: { value: new THREE.Vector2(1, 1) },
    uWClamp: { value: new THREE.Vector2(1.2, 2.6) }, // constant px width at upper-body distances, thinner far away
    uDpr: { value: 1 },
  };
  // scratch objects (no per-frame allocation) and rest-pose references
  const _v1 = new THREE.Vector3(); const _v2 = new THREE.Vector3(); const _v3 = new THREE.Vector3();
  const _vRay = new THREE.Vector3(); // lookTarget's pointer ray (its `out` is _v3)
  const _q1 = new THREE.Quaternion(); const _q2 = new THREE.Quaternion(); const _q3 = new THREE.Quaternion();
  const _sph = new THREE.Spherical();
  const AX = { x: new THREE.Vector3(1, 0, 0), y: new THREE.Vector3(0, 1, 0), z: new THREE.Vector3(0, 0, 1) };
  const _sph2 = new THREE.Spherical(); const _f1 = new THREE.Vector3(); const _f2 = new THREE.Vector3();
  const restFwd = new THREE.Vector3(0, 0, 1); // head's local forward (face direction at rest)
  const hipsFwd = new THREE.Vector3(0, 0, 1);
  const restUp = new THREE.Vector3(0, 1, 0); // face bone's local "up" (crown direction at rest)
  const eyeLocal = new THREE.Vector3(0, 0.1, 0.05); // eye level in the face bone's local space (m)
  let faceBone = null;
  let clean = []; // { bone, q }: bones the procedural layers rotate, with their clean clip pose
  let headRestY = 1.36; // standing head height (rest pose) for framing — never the live pose
  const rimGoal = new THREE.Color(RIM_TINTS.base);
  const rimDirWorld = new THREE.Vector3(...LIGHT.rimDir).normalize();
  let unitsPerMetre = 1; // GLB positions are quantized: shader maths on `position` uses these units
  let hasHeadCenter = false;
  let hasChains = false; // the GLB carries hair bone chains (spring physics) → no vertex sway

  function toonFrom(src, role) {
    if (role === 'overlay') {
      return new THREE.MeshBasicMaterial({
        name: src.name, map: src.map, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false,
      });
    }
    const p = SHADE[role] || SHADE.cloth;
    const m = new THREE.MeshToonMaterial({
      name: src.name,
      map: src.map || null,
      color: src.map ? 0xffffff : src.color,
      side: src.side,
    });
    // No polygonOffset: on double-sided cloth it is slope-scaled and GPU-
    // dependent (Safari/Metal applies far more bias), which pulled the jacket's
    // inner/back layers through the front — she looked "hollow". Skin that
    // z-fought under clothes is removed/shrunk in the build instead.
    const u = {
      uShadowTint: { value: new THREE.Vector3(...p.tint) }, uTh: { value: p.th }, uSoft: { value: p.soft },
      uRimMul: { value: p.rim },
    };
    const defs = {};
    const sameUv = (t) => t && src.map && (t.channel ?? 0) === (src.map.channel ?? 0);
    if (role === 'face' && hasHeadCenter && !/eye/i.test(src.name)) defs.KL_FACE = ''; // not the iris discs
    if (role === 'hair') { if (hasHeadCenter) defs.KL_RING = ''; if (!hasChains) defs.KL_SWAY = ''; } // chains: real bones
    if (sameUv(src.aoMap)) { defs.KL_AO = ''; u.uAoMap = { value: src.aoMap }; ownedTextures.add(src.aoMap); }
    if (role === 'cloth' && sameUv(src.metalnessMap)) {
      defs.KL_METAL = ''; u.uMetalMap = { value: src.metalnessMap }; ownedTextures.add(src.metalnessMap);
    }
    m.defines = { ...(m.defines || {}), ...defs };
    m.userData.klRole = role;
    m.userData.klUniforms = u;
    m.onBeforeCompile = (shader) => {
      Object.assign(shader.uniforms, shared, u);
      shader.vertexShader = shader.vertexShader
        .replace('#include <common>', `#include <common>\n${TOON_VERT_PARS}`)
        .replace('#include <beginnormal_vertex>', `#include <beginnormal_vertex>\n${TOON_VERT_NORMAL}`)
        .replace('#include <skinning_vertex>', `${TOON_VERT_SWAY}\n#include <skinning_vertex>`);
      shader.fragmentShader = shader.fragmentShader
        .replace('#include <common>', `#include <common>\n${TOON_FRAG_PARS}`)
        .replace('#include <lights_toon_pars_fragment>', TOON_LIGHTS)
        .replace('#include <map_fragment>', `#include <map_fragment>\n${TOON_FRAG_AO}`)
        .replace('#include <opaque_fragment>', `${TOON_FRAG_FX}\n#include <opaque_fragment>`);
    };
    // Same patch code for every toon part; the features differ only by defines,
    // which are part of the key (three adds them too), so programs never collide.
    m.customProgramCacheKey = () => `klukai-toon2:${Object.keys(m.defines).sort().join(',')}`;
    return m;
  }

  // Head-sphere centre in the GLB's (quantized) mesh space, from the skin's
  // inverse bind matrices: Head joint + (0, 5.1, −2.5) cm when Head is skinned,
  // else Neck + 12 cm up (the same point). Never metres: positions are
  // int16-quantized (~0.78 m/unit).
  function headCenterFromSkin(sk) {
    const find = (n) => sk.bones.findIndex((b) => b.name === n);
    const hi = find('mixamorigHead');
    const ni = find('mixamorigNeck');
    const i = hi >= 0 ? hi : ni;
    if (i < 0) return null;
    const bw = sk.boneInverses[i].clone().invert(); // bone bind pose in mesh space
    const upm = bw.getMaxScaleOnAxis(); // mesh units per metre
    const c = new THREE.Vector3().setFromMatrixPosition(bw);
    if (hi >= 0) c.add(new THREE.Vector3(0, 0.051 * upm, -0.025 * upm)); else c.add(new THREE.Vector3(0, 0.12 * upm, 0));
    return { center: c, upm };
  }

  // Smoothed normals for the hull where the build didn't bake `_outline`:
  // average normals of coincident positions across every outlined part (both
  // meshes share one quantized space), w = width mask.
  function bakeOutlineNormals(meshes) {
    // quantized (normalized int8/16) positions → one exact numeric key, no string per vertex
    const scaleOf = (P) => {
      const arr = P.array || P.data?.array;
      if (!P.normalized) return 0;
      return arr instanceof Int16Array ? 32767 : arr instanceof Uint16Array ? 65535 : arr instanceof Int8Array ? 127 : arr instanceof Uint8Array ? 255 : 0;
    };
    const keyOf = (P, i, q) => {
      if (q) { // each component + 32768 fits in 17 bits → < 2^51, exact in a double
        const c = (v) => Math.round(v * q) + 32768;
        return (c(P.getX(i)) * 131072 + c(P.getY(i))) * 131072 + c(P.getZ(i));
      }
      return `${Math.round(P.getX(i) * 1e5)},${Math.round(P.getY(i) * 1e5)},${Math.round(P.getZ(i) * 1e5)}`;
    };
    const acc = new Map();
    for (const o of meshes) {
      const P = o.geometry.attributes.position; const N = o.geometry.attributes.normal;
      if (!N) continue;
      const q = scaleOf(P);
      for (let i = 0; i < P.count; i++) {
        const k = keyOf(P, i, q); const a = acc.get(k);
        const nx = N.getX(i); const ny = N.getY(i); const nz = N.getZ(i);
        if (a) { a[0] += nx; a[1] += ny; a[2] += nz; } else acc.set(k, [nx, ny, nz]);
      }
    }
    for (const o of meshes) {
      const g = o.geometry;
      if (g.getAttribute('_outline')) continue;
      const P = g.attributes.position; const N = g.attributes.normal;
      const out = new Float32Array(P.count * 4);
      const face = o.material.userData.klRole === 'face';
      const q = scaleOf(P);
      for (let i = 0; i < P.count; i++) {
        let nx = N ? N.getX(i) : 0; let ny = N ? N.getY(i) : 1; let nz = N ? N.getZ(i) : 0;
        const a = acc.get(keyOf(P, i, q));
        const len = a ? Math.hypot(a[0], a[1], a[2]) : 0;
        if (len > 0.3) { nx = a[0] / len; ny = a[1] / len; nz = a[2] / len; }
        // face: no hull lines on the front (nose, mouth corners, eye sockets), only on the contour
        const wMask = face ? 1 - smoothstep(0.55, 0.85, N ? N.getZ(i) : 0) : 1;
        out.set([nx, ny, nz, wMask], i * 4);
      }
      g.setAttribute('_outline', new THREE.BufferAttribute(out, 4));
    }
  }

  function outlineMaterial(src, role) {
    const p = OUTLINE[role];
    const color = new THREE.Color(p.tint);
    if (!src.map && src.color) color.multiply(src.color);
    const mat = new THREE.MeshBasicMaterial({ name: `${src.name}_outline`, map: src.map || null, color, side: THREE.BackSide });
    // discard only where needed: it costs early-z, so cloth and hair hulls stay without it
    if (role === 'skin' || role === 'face') mat.defines = { ...(mat.defines || {}), KL_FACING: '' };
    // the hair hull must sway with the hair it outlines, or its lines float beside the tips
    if (role === 'hair' && !hasChains) mat.defines = { ...(mat.defines || {}), KL_SWAY: '' };
    const u = { uPx: { value: p.px }, uZ: { value: p.z } };
    mat.userData.klUniforms = u;
    mat.onBeforeCompile = (shader) => {
      Object.assign(shader.uniforms, outlineU, u);
      if (role === 'hair') {
        for (const k of ['uTime', 'uSway', 'uSwayBox', 'uSwayAmp']) shader.uniforms[k] = shared[k];
      }
      shader.vertexShader = shader.vertexShader
        .replace('#include <common>', `#include <common>\n${OUTLINE_VERT_PARS}\n${SWAY_VERT_PARS}`)
        .replace('#include <beginnormal_vertex>', '#include <beginnormal_vertex>\nobjectNormal = normalize( _outline.xyz + vec3( 0.0, 1e-5, 0.0 ) );')
        .replace('#include <skinning_vertex>', `${TOON_VERT_SWAY}\n#include <skinning_vertex>`)
        .replace('#include <project_vertex>', `#include <project_vertex>\n${OUTLINE_VERT_PROJECT}`);
      shader.fragmentShader = shader.fragmentShader
        .replace('#include <common>', `#include <common>\n${OUTLINE_FRAG_PARS}`)
        .replace('#include <clipping_planes_fragment>', `${OUTLINE_FRAG_CUT}\n#include <clipping_planes_fragment>`);
    };
    mat.customProgramCacheKey = () => `klukai-outline4:${Object.keys(mat.defines || {}).sort().join(',')}`;
    return mat;
  }

  function addOutlines() {
    const list = skinned.filter((o) => {
      const mat = o.material;
      if (Array.isArray(mat) || !OUTLINE[mat.userData.klRole] || /eye/i.test(mat.name)) return false;
      // a flat near-black part (no texture) gains nothing from a dark hull: save the draw call
      return Boolean(mat.map) || mat.color.r + mat.color.g + mat.color.b > 0.06;
    });
    if (list.some((o) => !o.geometry.getAttribute('_outline'))) bakeOutlineNormals(list);
    for (const src of list) {
      const o = new THREE.SkinnedMesh(src.geometry, outlineMaterial(src.material, src.material.userData.klRole));
      o.name = `${src.name}_outline`;
      o.bind(src.skeleton, src.bindMatrix);
      o.position.copy(src.position); o.quaternion.copy(src.quaternion); o.scale.copy(src.scale);
      o.morphTargetInfluences = src.morphTargetInfluences; // same array → blink follows
      o.morphTargetDictionary = src.morphTargetDictionary;
      o.frustumCulled = false;
      o.renderOrder = 1; // after the opaques: early-z rejects hidden hull pixels
      o.visible = outlinesOn;
      o.userData.klOutline = true;
      o.userData.role = src.material.userData.klRole;
      src.parent.add(o);
      outlineMeshes.push(o);
    }
  }

  function setOutlines(on) {
    outlinesOn = Boolean(on) && outlineMeshes.length > 0;
    for (const o of outlineMeshes) o.visible = outlinesOn;
  }

  // ── load ─────────────────────────────────────────────────────────────────
  const onAbort = () => teardown();
  opts.signal?.addEventListener('abort', onAbort, { once: true });
  try {
    const buf = modelBuffer;
    if (disposed) return stub;
    if (!(buf instanceof ArrayBuffer) && !ArrayBuffer.isView(buf)) throw new Error('loadModel() must resolve to an ArrayBuffer');
    const data = ArrayBuffer.isView(buf) ? buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) : buf;
    const loader = new GLTFLoader().setMeshoptDecoder(MeshoptDecoder);
    const gltf = await new Promise((resolve, reject) => loader.parse(data, '', resolve, reject));
    mark('parseMs');
    if (disposed) { disposeObject(gltf.scene); return stub; }
    root = gltf.scene;
    if (!opts.framing && root.userData?.preferredFraming === 'full') framing = 'full';
    root.traverse((o) => { if (o.isSkinnedMesh) skinned.push(o); });
    const chainData = Array.isArray(root.userData?.springChains) ? root.userData.springChains : [];
    hasChains = chainData.some(chainUsable); // same rule buildSprings applies: never sway off with no springs
    const skin = skinned[0]?.skeleton;
    const hc = skin ? headCenterFromSkin(skin) : null;
    if (hc) { shared.uHeadCenter.value.copy(hc.center).applyMatrix4(skinned[0].bindMatrixInverse); unitsPerMetre = hc.upm; hasHeadCenter = true; }
    const hairGeos = [];
    root.traverse((o) => {
      if (!o.isMesh) return;
      o.frustumCulled = false; // skinned bounds are bind-pose; avoid popping
      const mats = Array.isArray(o.material) ? o.material : [o.material];
      const next = mats.map((m) => {
        const role = m.userData?.role || ROLE_FALLBACK(m.name);
        const t = toonFrom(m, role);
        if (role === 'hair' && !Array.isArray(o.material)) hairGeos.push(o.geometry);
        m.dispose();
        return t;
      });
      o.material = Array.isArray(o.material) ? next : next[0];
    });
    setSwayBox(hairGeos);
    scene.add(root);
    head = root.getObjectByName('mixamorigHead');
    neck = root.getObjectByName('mixamorigNeck');
    headJoint = Boolean(head && skin?.bones.includes(head));
    for (const n of ['Hips', 'Spine1', 'Spine2', 'LeftShoulder', 'RightShoulder', 'LeftArm', 'RightArm', 'LeftForeArm', 'RightForeArm', 'LeftToeBase', 'RightToeBase', 'LeftFoot', 'RightFoot']) {
      bones[n] = root.getObjectByName(`mixamorig${n}`) || null;
    }
    for (const m of skinned) {
      const idx = m.morphTargetDictionary?.blink;
      if (idx !== undefined) blinkIdx = idx;
    }
    addOutlines();
    captureRest();
    springs = hasChains ? buildSprings(root.userData) : null;
    mixer = new THREE.AnimationMixer(root);
    for (const clip of gltf.animations) {
      const a = mixer.clipAction(clip);
      a.enabled = true;
      actions.set(clip.name, a);
    }
    buildFidgets(gltf.animations);
    mixer.addEventListener('finished', onFinished);
    mixer.addEventListener('loop', onLoop);
    talkAction = actions.get('talking') || null;
    if (talkAction) { talkAction.setEffectiveWeight(0); talkAction.play(); }
    shadowMesh = makeContactShadow();
  } catch (e) {
    fail(e);
    teardown();
    return stub;
  }

  // Hair sway bounds from all hair geometry together (one shared uniform; quantized units, never metres).
  function setSwayBox(geos) {
    const bb = new THREE.Box3();
    for (const g of geos) {
      if (!g.boundingBox) g.computeBoundingBox();
      if (g.boundingBox) bb.union(g.boundingBox);
    }
    if (bb.isEmpty() || !Number.isFinite(bb.min.y)) return;
    const hcZ = shared.uHeadCenter.value.z;
    const upm = unitsPerMetre;
    // sway starts at about neck height and is full at the tips; z: only hair behind her back
    shared.uSwayBox.value.set(bb.min.y + 0.67 * (bb.max.y - bb.min.y), bb.min.y, hcZ - 0.12 * upm, hcZ - 0.2 * upm);
  }

  // ── rest-pose references for the procedural layers ───────────────────────
  // faceBone: the bone that actually carries the face — Head when it is a skin
  // joint, else Neck (the head mesh rides on it; the clips' Head rotation is
  // then invisible and must not count as "where she looks").
  function captureRest() {
    faceBone = headJoint ? head : neck;
    clean = [neck, head, ...['Spine1', 'Spine2', 'LeftShoulder', 'RightShoulder', 'LeftArm', 'RightArm', 'LeftForeArm', 'RightForeArm'].map((n) => bones[n])]
      .filter(Boolean).map((bone) => ({ bone, q: bone.quaternion.clone() }));
    root.updateMatrixWorld(true);
    if (head) headRestY = head.getWorldPosition(new THREE.Vector3()).y;
    const framingHeadY = root.userData?.framingHeadY;
    if (Number.isFinite(framingHeadY) && framingHeadY >= 0.5 && framingHeadY <= 3) headRestY = framingHeadY;
    forwardOf(faceBone, restFwd);
    forwardOf(bones.Hips, hipsFwd);
    forwardOf(faceBone, restUp, AX.y);
    // eyes ≈ the head-sphere centre, moved 2 cm forward; into the face bone's local space
    const sk = skinned[0]?.skeleton;
    const i = sk && faceBone ? sk.bones.indexOf(faceBone) : -1;
    if (i >= 0 && hasHeadCenter) {
      const mesh = shared.uHeadCenter.value.clone().applyMatrix4(skinned[0].bindMatrix);
      mesh.z += 0.02 * unitsPerMetre;
      eyeLocal.copy(mesh).applyMatrix4(sk.boneInverses[i]);
    }
  }
  // Bone-local direction of "forward" (+z: she is modelled facing +z) in the
  // bind pose, from the skin's inverse bind matrix (the node rest pose may differ).
  function forwardOf(bone, out, dir = AX.z) {
    if (!bone) return;
    const sk = skinned[0]?.skeleton;
    const i = sk ? sk.bones.indexOf(bone) : -1;
    const q = new THREE.Quaternion();
    if (i >= 0) sk.boneInverses[i].clone().invert().decompose(new THREE.Vector3(), q, new THREE.Vector3());
    else bone.getWorldQuaternion(q);
    out.copy(dir).applyQuaternion(q.invert());
  }

  // ── camera framing (eased) + persistent user zoom/orbit ──────────────────
  const camGoal = { target: new THREE.Vector3(), sph: new THREE.Spherical(2, Math.PI / 2, 0), fov: BASE_FOV };
  const camCur = { target: new THREE.Vector3(), sph: new THREE.Spherical(2, Math.PI / 2, 0), fov: BASE_FOV };
  const orbit = createCameraView();
  let orbitDragging = false;
  let camSnap = true;
  const drift = new THREE.Vector3();
  let pushIn = 0;
  let follow = 0; // slow horizontal follow of her hips (the idle clip drifts ~10 cm sideways)
  let sideView = false;

  function frame({ snap = false } = {}) {
    if (!root) return;
    // Standing head height from the rest pose: the live pose would aim the camera at
    // the floor when she gets up from `sleep` (and jitter with the clip).
    const insets = { top: insetsPx.top / h, bottom: insetsPx.bottom / h };
    const f = cameraFraming({ framing, state: stateName, aspect: w / h, headY: headRestY, insets });
    const tz = f.targetZ || 0;
    sideView = Boolean(f.side);
    camGoal.target.set(sideView ? 0 : follow, f.targetY, tz);
    const pos = f.side ? new THREE.Vector3(f.dist, f.camY, tz) : new THREE.Vector3(0, f.camY, f.dist);
    camGoal.sph.setFromVector3(pos.sub(camGoal.target));
    camGoal.fov = f.fov;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    if (snap) camSnap = true;
    placeCamera(0);
  }

  function absorbCameraInput() {
    if (controls) {
      _sph2.setFromVector3(_v1.subVectors(camera.position, controls.target));
      orbit.absorb(_sph2);
    }
  }
  function placeCamera(dt, absorb = true) {
    // 1. Include pointer-handler and damping updates since our last placement,
    //    even when resize/insets snap the automatic framing this frame.
    if (absorb) absorbCameraInput();
    // 2. ease the framing (~0.8 s, frame-rate independent)
    const k = camSnap ? 1 : 1 - Math.exp(-dt * 3.0);
    camCur.target.lerp(camGoal.target, k);
    camCur.sph.radius += (camGoal.sph.radius - camCur.sph.radius) * k;
    camCur.sph.phi += (camGoal.sph.phi - camCur.sph.phi) * k;
    camCur.sph.theta += wrapPi(camGoal.sph.theta - camCur.sph.theta) * k;
    const fov = camCur.fov + (camGoal.fov - camCur.fov) * k;
    if (Math.abs(fov - camera.fov) > 1e-4) { camera.fov = fov; camera.updateProjectionMatrix(); }
    camCur.fov = fov;
    camSnap = false;
    // 3. Compose the persistent view with the automatic 3% speech push-in.
    const view = orbit.place(camCur.sph, pushIn);
    camera.position.setFromSpherical(_sph.set(view.radius, view.phi, view.theta)).add(camCur.target);
    if (controls) {
      controls.target.copy(camCur.target);
      controls.minDistance = view.minDistance; controls.maxDistance = view.maxDistance;
      controls.minAzimuthAngle = view.minAzimuthAngle; controls.maxAzimuthAngle = view.maxAzimuthAngle;
      controls.minPolarAngle = view.minPolarAngle; controls.maxPolarAngle = view.maxPolarAngle;
      controls.update();
    } else {
      camera.lookAt(camCur.target);
    }
  }
  function changeView(change) {
    if (disposed || !root) return;
    absorbCameraInput();
    // Drain the controls' residual rotation through their public API, then
    // replace that camera position. Otherwise a Reset during damping drifts.
    if (controls) {
      const damping = controls.enableDamping;
      controls.enableDamping = false;
      controls.update();
      controls.enableDamping = damping;
    }
    change();
    placeCamera(0, false);
    if (paused) renderer.render(scene, camera);
  }
  if (OrbitControls) {
    controls = new OrbitControls(camera, canvas);
    controls.enablePan = false;
    controls.enableZoom = true;
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.rotateSpeed = isIOS || globalThis.matchMedia?.('(pointer: coarse)').matches ? 0.7 : 0.5;
    onControlStart = () => { orbitDragging = true; };
    onControlEnd = () => { orbitDragging = false; };
    controls.addEventListener('start', onControlStart);
    controls.addEventListener('end', onControlEnd);
  }

  // ── animation control ────────────────────────────────────────────────────
  const FADE = 0.45;
  let idleStartedAt = 0;
  let lastFidget = null;

  function pickClip(state) {
    const spec = STATE_CLIPS[state] || STATE_CLIPS.idle;
    const avail = spec.clips.filter((c) => actions.has(c));
    if (!avail.length) return null;
    return avail[Math.floor(Math.random() * avail.length)];
  }

  function play(state, { fade = FADE } = {}) {
    const spec = STATE_CLIPS[state] || STATE_CLIPS.idle;
    const clipName = pickClip(state) || pickClip('idle');
    const next = clipName ? actions.get(clipName) : null;
    if (!next) return;
    if (current?.action === next && next.isRunning()) {
      // Already on screen. If it was playing as a one-shot fidget (thankful is
      // both a fidget and a happy clip), adopt it in place with this state's
      // loop mode: no restart, and its end is no longer taken for a fidget's.
      if (current.fidget || current.once !== !!spec.once) {
        next.setLoop(spec.loop ? THREE.LoopRepeat : THREE.LoopOnce, spec.loop ? Infinity : 1);
        next.clampWhenFinished = !spec.loop;
        current = { state, action: next, once: !!spec.once };
      } else current.state = state;
      return;
    }
    next.reset();
    next.setLoop(spec.loop ? THREE.LoopRepeat : THREE.LoopOnce, spec.loop ? Infinity : 1);
    next.clampWhenFinished = !spec.loop;
    next.setEffectiveTimeScale(1);
    next.setEffectiveWeight(1);
    if (clipName === 'idle') { // never start the idle from the same frame
      next.time = Math.random() * next.getClip().duration;
      idleStartedAt = performance.now();
    }
    next.play();
    if (current?.action && current.action !== next) {
      current.action.crossFadeTo(next, fade, false);
    } else {
      next.fadeIn(fade);
    }
    current = { state, action: next, once: !!spec.once };
  }

  function buildFidgets(clips) {
    const byName = new Map(clips.map((c) => [c.name, c]));
    for (const f of FIDGETS) {
      try {
        const src = byName.get(f.from || f.name);
        if (!src) continue;
        const clip = f.from ? THREE.AnimationUtils.subclip(src, f.name, Math.round(f.start * 30), Math.round(Math.min(f.end, src.duration) * 30), 30) : src;
        if (!clip.tracks.length || clip.duration < 0.5) continue;
        fidgetActions.set(f.name, f.from ? mixer.clipAction(clip) : actions.get(f.name));
      } catch { /* a missing or odd clip only means one fidget fewer */ }
    }
  }
  function playFidget() {
    const name = pickFidget([...fidgetActions.keys()], lastFidget);
    const next = name && fidgetActions.get(name);
    if (!next || !current?.action || current.action === next) return;
    lastFidget = name;
    next.reset();
    next.setLoop(THREE.LoopOnce, 1);
    next.clampWhenFinished = true;
    next.setEffectiveTimeScale(1);
    next.setEffectiveWeight(1);
    next.play();
    current.action.crossFadeTo(next, 0.6, false);
    current = { state: 'idle', action: next, once: true, fidget: true };
  }
  function onLoop(e) {
    if (!life || reducedMotion || !current || e.action !== current.action || current.fidget) return;
    if (stateName !== 'idle' || e.action !== actions.get('idle') || speakingTarget > 0.04) return;
    if (performance.now() - idleStartedAt < e.action.getClip().duration * 1000 * 0.98) return;
    if (Math.random() < 0.35) playFidget();
  }
  function onFinished(e) {
    if (!current || e.action !== current.action || !current.once) return;
    const wasFidget = current.fidget;
    if (!wasFidget && stateName === 'greet') { sm.settle('idle'); stateName = 'idle'; }
    play(stateName === 'idle' ? 'idle' : stateName, { fade: wasFidget ? 0.6 : FADE });
  }

  const sm = createStateMachine({
    now: () => performance.now(),
    enter(name, prev) {
      if (prev === 'talking' && name !== 'talking') talkWeight = 1; // fade the layer out, no pop
      stateName = name;
      play(name);
      if ((prev === 'sleep') !== (name === 'sleep')) { frame(); resetSprings(700); } // lying down / getting up: hang through the crossfade
    },
  });

  play('idle', { fade: 0 });
  stateName = 'idle';
  frame({ snap: true });

  // ── procedural layers ────────────────────────────────────────────────────
  // Rotate a bone about a WORLD axis (whatever its local axes are):
  // q_local' = parentInv · R · parent · q_local
  function rotWorld(bone, axis, angle) {
    if (!bone || !angle) return;
    bone.parent.getWorldQuaternion(_q1);
    _q2.setFromAxisAngle(axis, angle);
    _q3.copy(_q1).invert().multiply(_q2).multiply(_q1);
    bone.quaternion.premultiply(_q3);
  }

  // Idle arm relax: the idle clip holds the arms tight to the body.
  let armW = 0;
  function applyArms(dt, t) {
    const idleClip = current?.action === actions.get('idle') && !current.fidget;
    const goal = life && idleClip && stateName === 'idle' ? 1 - Math.min(1, talkWeight) : 0;
    armW += (goal - armW) * (1 - Math.exp(-dt / 0.4));
    if (armW < 1e-3) return;
    const swing = (reducedMotion ? 0 : 1.5 * DEG) * Math.sin(t * 2 * Math.PI * 0.11);
    rotWorld(bones.LeftArm, AX.z, 0.10 * armW);
    rotWorld(bones.RightArm, AX.z, -0.10 * armW);
    rotWorld(bones.LeftArm, AX.x, -swing * armW);
    rotWorld(bones.RightArm, AX.x, swing * armW);
    rotWorld(bones.LeftForeArm, AX.x, -0.17 * armW);
    rotWorld(bones.RightForeArm, AX.x, -0.17 * armW);
  }

  // Breathing: chest ±0.6°, shoulders ±0.4°, 4.2 s (the CSS `breathe` period).
  let breathPhase = 0;
  function applyBreath(dt) {
    if (!life) return;
    const sleep = stateName === 'sleep';
    breathPhase += (dt / (sleep ? 5.5 : 4.2)) * 2 * Math.PI;
    const amp = stateName === 'excited' || stateName === 'flustered' ? 1.3 : 1;
    const s = Math.sin(breathPhase);
    rotWorld(bones.Spine1, AX.x, -0.6 * DEG * amp * s * 0.5);
    rotWorld(bones.Spine2, AX.x, -0.6 * DEG * amp * s * 0.5);
    if (!sleep) {
      rotWorld(bones.LeftShoulder, AX.z, 0.4 * DEG * amp * s);
      rotWorld(bones.RightShoulder, AX.z, -0.4 * DEG * amp * s);
    }
  }

  // Look-at: toward the camera (and the pointer on desktop), damped, clamped,
  // weighted by state and by how far the clip already turns her head.
  const look = { yaw: 0, pitch: 0, w: 0, sYaw: 0, sPitch: 0, nextSacc: 0, gazeAt: 0, gYaw: 0, gPitch: 0 };
  const pointer = { x: 0, y: 0, until: 0 };
  function lookTarget(out, headPos) {
    out.copy(camera.position);
    if (performance.now() < pointer.until) {
      _vRay.set(pointer.x, pointer.y, 0.5).unproject(camera).sub(camera.position).normalize();
      out.addScaledVector(_vRay, camera.position.distanceTo(headPos));
    }
    return out;
  }
  function applyLookAt(dt, now) {
    const bone = faceBone;
    if (!bone || !neck) return;
    bone.getWorldPosition(_v1);
    bone.getWorldQuaternion(_q1);
    const fwd = _v2.copy(restFwd).applyQuaternion(_q1);
    const dir = lookTarget(_v3, _v1).sub(_v1).normalize();
    let dy = wrapPi(Math.atan2(dir.x, dir.z) - Math.atan2(fwd.x, fwd.z));
    let dp = Math.asin(clamp(dir.y, -1, 1)) - Math.asin(clamp(fwd.y, -1, 1));
    // micro-saccades: small target jumps, at most every 0.5 s (~5 % per 60 Hz frame)
    if (!reducedMotion && now >= look.nextSacc && Math.random() < 1 - Math.pow(0.95, dt * 60)) {
      look.sYaw = (Math.random() * 2 - 1) * 0.8 * DEG; look.sPitch = (Math.random() * 2 - 1) * 0.6 * DEG;
      look.nextSacc = now + 500;
    }
    dy += look.sYaw; dp += look.sPitch;
    // the clip's own head angle (vs the body) lowers the weight: big gestures win
    let clipAngle = 0;
    if (bones.Hips) { bones.Hips.getWorldQuaternion(_q2); clipAngle = fwd.angleTo(_v1.copy(hipsFwd).applyQuaternion(_q2)); }
    // a target behind her (orbit, inspection cameras) would clamp to ±max and flip sides as she
    // turns, snapping the head: let go of it instead
    const behind = 1 - smoothstep(90 * DEG, 130 * DEG, Math.abs(dy));
    const wGoal = life ? (LOOK_WEIGHT[stateName] ?? 0.6) * (1 - smoothstep(30 * DEG, 90 * DEG, clipAngle)) * behind : 0;
    look.w += (wGoal - look.w) * (1 - Math.exp(-dt * 2.5));
    const lim = lookSplit({ yaw: dy, pitch: dp, headJoint }); // clamp first …
    const cy = (lim.neck.yaw + lim.head.yaw) * look.w;
    const cp = (lim.neck.pitch + lim.head.pitch) * look.w;
    const k = 1 - Math.exp(-dt * 4); // … then damp (ChatVRM smoothFactor 4)
    look.yaw += (cy - look.yaw) * k;
    look.pitch += (cp - look.pitch) * k;
    // a gaze shift over 15° comes with a blink
    if (now - look.gazeAt > 250) {
      if (Math.hypot(cy - look.gYaw, cp - look.gPitch) > 15 * DEG) triggerBlink(now);
      look.gYaw = cy; look.gPitch = cp; look.gazeAt = now;
    }
    if (look.w < 1e-3 && Math.abs(look.yaw) < 1e-4 && Math.abs(look.pitch) < 1e-4) return;
    const split = lookSplit({ yaw: look.yaw, pitch: look.pitch, headJoint });
    const pitchAxis = _v1.crossVectors(fwd, AX.y);
    if (pitchAxis.lengthSq() < 1e-6) return;
    pitchAxis.normalize();
    rotWorld(neck, AX.y, split.neck.yaw);
    rotWorld(neck, pitchAxis, split.neck.pitch);
    if (headJoint) { rotWorld(head, AX.y, split.head.yaw); rotWorld(head, pitchAxis, split.head.pitch); }
  }

  // Talk layer: nods on speech onsets, a slow yaw drift, a small roll at phrase ends.
  let env = { fast: 0, slow: 0, since: 1, armed: true };
  const nods = [];
  let talkPresence = 0;
  let lastLoudAt = -Infinity;
  let inPhrase = false;
  let rollAt = -Infinity;
  function applyTalk(dt, now) {
    env = stepEnvelope(env, speakingTarget, dt);
    if (env.onset && life) {
      nods.push({ t0: now, amp: (3 + 2 * Math.min(1, speakingTarget)) * DEG });
      if (nods.length > 3) nods.shift();
    }
    if (speakingTarget > 0.08) { lastLoudAt = now; inPhrase = true; } else if (inPhrase && now - lastLoudAt > 350) {
      inPhrase = false; rollAt = now; triggerBlink(now); // phrase end
    }
    talkPresence += ((inPhrase ? 1 : 0) - talkPresence) * (1 - Math.exp(-dt * 2.2));
    pushIn = life && !reducedMotion ? talkPresence : 0;
    if (!life) return;
    let nod = 0;
    for (const n of nods) nod += nodCurve((now - n.t0) / 1000, n.amp);
    while (nods.length && now - nods[0].t0 > 1500) nods.shift();
    const roll = nodCurve((now - rollAt) / 1000, 1 * DEG, 0.25);
    const yaw = 1.5 * DEG * talkPresence * Math.sin((now / 1000) * 2 * Math.PI * 0.23);
    if (!nod && !roll && Math.abs(yaw) < 1e-5) return;
    const bone = faceBone;
    if (!bone) return;
    bone.getWorldQuaternion(_q1);
    const fwd = _v2.copy(restFwd).applyQuaternion(_q1);
    const pitchAxis = _v1.crossVectors(fwd, AX.y);
    if (pitchAxis.lengthSq() < 1e-6) return;
    pitchAxis.normalize();
    rotWorld(bone, pitchAxis, -nod);
    rotWorld(bone, AX.y, yaw);
    rotWorld(bone, fwd.normalize(), roll);
  }

  // Eye guard: she never hides her eyes from him under the cap brim. Measure how far
  // the camera sits above the face's own horizontal plane (head space, from the
  // eyes) and, past GUARD.start, lift the head-carrying bone about its own right
  // axis so that elevation eases toward GUARD.max (soft limit) instead of growing.
  // Pitch only: yaw, roll and the rest of the clip's character stay.
  const GUARD = { start: EYE_GUARD.start * DEG, max: EYE_GUARD.max * DEG, side: EYE_GUARD.side * DEG, on: true };
  const guard = { w: 1, e0: 0, e1: 0, lift: 0, az: 0 };
  function eyeElevation(bone) {
    bone.getWorldQuaternion(_q1);
    const fwd = _v2.copy(restFwd).applyQuaternion(_q1);
    const up = _v3.copy(restUp).applyQuaternion(_q1);
    const eye = _f1.copy(eyeLocal).applyMatrix4(bone.matrixWorld);
    const c = _f2.subVectors(camera.position, eye).normalize();
    // camera azimuth around the face (0 = straight in front): from the side the brim
    // covers the near eye sooner, so the limit comes down as she turns away
    const right = _f1.crossVectors(fwd, up);
    guard.az = Math.atan2(Math.abs(c.dot(right)), c.dot(fwd));
    return Math.asin(clamp(c.dot(up), -1, 1)); // + = camera above the face's plane (brim side)
  }
  function applyEyeGuard(dt) {
    const bone = faceBone;
    guard.w += ((stateName === 'sleep' ? 0.2 : 1) - guard.w) * (1 - Math.exp(-dt * 3));
    guard.lift = 0;
    if (!bone || !life) return;
    const e = eyeElevation(bone);
    guard.e0 = e; guard.e1 = e;
    const side = GUARD.side * smoothstep(20 * DEG, 70 * DEG, guard.az);
    if (!GUARD.on || e <= GUARD.start - side) return;
    const goal = e - guard.w * (e - softLimit(e, GUARD.start - side, GUARD.max - side));
    for (let i = 0; i < 2; i++) { // second pass corrects for yaw (the lift then acts partly sideways)
      const cur = i ? eyeElevation(bone) : e;
      const lift = cur - goal;
      if (lift <= 1e-4) break;
      const axis = _v1.crossVectors(_v2, _v3); // fwd × up (both set by eyeElevation): + lifts the face
      if (axis.lengthSq() < 1e-8) break;
      rotWorld(bone, axis.normalize(), lift);
      guard.lift += lift;
    }
    guard.e1 = eyeElevation(bone);
  }

  // Blinks: random gaps (logic.js nextBlink) + on gaze shifts and phrase ends;
  // slow in tender moods, a little lid droop when she's sad.
  const SLOW_BLINK_MOODS = new Set(['tender', 'affectionate', 'drowsy']);
  let lid = 0;
  function scheduleBlink(now) {
    const b = nextBlink();
    nextBlinkAt = now + b.delay;
    pendingDouble = b.double;
  }
  function triggerBlink(now) {
    if (blinkIdx < 0 || blinkT >= 0 || stateName === 'sleep' || now - lastBlinkAt < 700) return;
    blinkT = 0; blinkSlow = SLOW_BLINK_MOODS.has(sm.mood); pendingDouble = false; lastBlinkAt = now;
  }
  function applyBlink(dt, now) {
    if (blinkIdx < 0) return;
    lid += ((stateName === 'sad' ? 0.18 : 0) - lid) * (1 - Math.exp(-dt * 2));
    let v = 0;
    if (stateName === 'sleep') v = 1;
    else if (blinkT >= 0) {
      blinkT += dt * 1000;
      v = blinkCurve(blinkT, blinkSlow);
      if (v < 0) {
        v = 0;
        if (pendingDouble) { pendingDouble = false; blinkT = -120; } else { blinkT = -1; scheduleBlink(now); }
      }
    } else if (blinkT < -1) { // gap before the second of a double blink
      blinkT += dt * 1000;
      if (blinkT >= -1) blinkT = 0;
    } else if (now >= nextBlinkAt) { blinkT = 0; blinkSlow = SLOW_BLINK_MOODS.has(sm.mood); lastBlinkAt = now; }
    if (stateName !== 'sleep') v = lid + (1 - lid) * v;
    for (const m of skinned) if (m.morphTargetInfluences) m.morphTargetInfluences[blinkIdx] = v;
  }

  // Hair springs (when the GLB has chains): three-vrm's VRMSpringBoneJoint on plain
  // bones. Tails live in centre space (the character root), so root or camera moves
  // don't throw the hair; fixed 1/60 s sub-steps; roots before children; after the
  // mixer, look-at and eye guard. "Hang" mode (the step's fixed point, no inertia)
  // covers resets, sleep transitions, big pose jumps and prefers-reduced-motion.
  const _rootInv = new THREE.Matrix4();
  const _rootQ = new THREE.Quaternion();
  const _sv = new THREE.Vector3(); const _sh = new THREE.Vector3();
  const _sq = new THREE.Quaternion(); const _sqa = new THREE.Quaternion(); const _sScale = new THREE.Vector3();
  const _tmp = [0, 0, 0];
  // A chain the springs can run: every bone present, every segment (and the tail) non-zero.
  function chainUsable(ch) {
    const list = (Array.isArray(ch?.bones) ? ch.bones : []).map((n) => root.getObjectByName(n));
    if (!list.length || list.some((b) => !b)) return false;
    const tail = Array.isArray(ch.tail) ? ch.tail : [0, -0.1, 0];
    return list.every((b, i) => (i < list.length - 1 ? list[i + 1].position.length() : Math.hypot(...tail)) > 1e-4);
  }
  function buildSprings(ud) {
    const chains = [];
    for (const ch of ud.springChains || []) {
      if (!chainUsable(ch)) continue;
      const list = ch.bones.map((n) => root.getObjectByName(n));
      const set = /side|lock|front/i.test(ch.name || '') ? SPRING.lock : SPRING.long;
      const joints = list.map((bone, i) => {
        const tail = i < list.length - 1 ? list[i + 1].position.clone() : new THREE.Vector3().fromArray(ch.tail || [0, -0.1, 0]);
        const f = list.length > 1 ? i / (list.length - 1) : 0;
        const j = {
          bone, initQ: bone.quaternion.clone(), axis: tail.clone().normalize(), len: tail.length(),
          stiffness: set.stiffness[0] + (set.stiffness[1] - set.stiffness[0]) * f, drag: set.drag, gravity: set.gravity, hit: set.hitRadius,
          maxAngle: (set.maxAngle || 0) * DEG, cur: [0, 0, 0], prev: [0, 0, 0], head: [0, 0, 0], rest: [0, 0, 0],
        };
        // one params object per joint, reused every step (no per-frame allocation)
        j.p = { cur: j.cur, prev: j.prev, head: j.head, len: j.len, restDir: j.rest, stiffness: 0, drag: 0, gravity: 0, gravityDir: [0, -1, 0], dt: SPRING_H, colliders: null, hitRadius: 0, maxAngle: 0 };
        return j;
      });
      if (joints.every((j) => j.len > 1e-4)) chains.push({ name: ch.name, joints, lock: set === SPRING.lock });
    }
    if (!chains.length) return null;
    const colliders = [];
    for (const c of ud.springColliders || []) {
      const bone = root.getObjectByName(c?.bone);
      const to = c?.to ? root.getObjectByName(c.to.bone) : null;
      if (!bone || !(c.radius > 0) || (c.to && !to)) continue;
      colliders.push({
        bone, off: new THREE.Vector3().fromArray(c.offset || [0, 0, 0]), to, toOff: to ? new THREE.Vector3().fromArray(c.to.offset || [0, 0, 0]) : null,
        shape: to ? { a: [0, 0, 0], b: [0, 0, 0], r: c.radius } : { c: [0, 0, 0], r: c.radius },
      });
    }
    // + the floor (she lies down in `sleep`; crouches): hair rests on it instead of sinking
    const shapes = [...colliders.map((c) => c.shape), { floorY: 0 }];
    // side locks hang in front: only head, neck, chest and shoulders can reach them
    const near = shapes.filter((sh, i) => i >= colliders.length || /Head|Neck|Spine2|Arm$/.test(colliders[i].bone.name));
    for (const ch of chains) for (const j of ch.joints) j.p.colliders = ch.lock ? near : shapes;
    // bones whose world matrices the springs read, parents first (not the whole scene graph)
    const need = new Set();
    const addUp = (b) => { for (let x = b; x && x !== root; x = x.parent) need.add(x); };
    if (head) addUp(head);
    for (const c of colliders) { addUp(c.bone); if (c.to) addUp(c.to); }
    for (const ch of chains) addUp(ch.joints[0].bone.parent);
    const order = [];
    root.traverse((o) => { if (need.has(o)) order.push(o); }); // traverse = parents before children
    return { chains, colliders, shapes, order, acc: 0, hold: 0, primed: false, lastPos: new THREE.Vector3(), lastQ: new THREE.Quaternion(), ms: 0, mode: 'hang' };
  }
  function toCentre(v, out) { v.applyMatrix4(_rootInv); out[0] = v.x; out[1] = v.y; out[2] = v.z; }
  // one joint: rest direction from the (already updated) parent, then sim / hang / aim, then rotate the bone at its tail
  function solveJoint(j, mode) {
    const parent = j.bone.parent;
    _sq.setFromRotationMatrix(parent.matrixWorld).multiply(j.initQ); // bone's rest frame, world
    _sh.copy(j.bone.position).applyMatrix4(parent.matrixWorld); // joint origin, world
    toCentre(_sv.copy(_sh), j.head);
    _sv.copy(j.axis).applyQuaternion(_sq).applyQuaternion(_rootQ); // rest direction, centre space
    j.rest[0] = _sv.x; j.rest[1] = _sv.y; j.rest[2] = _sv.z;
    const p = j.p;
    p.stiffness = j.stiffness; p.drag = j.drag; p.gravity = j.gravity; p.hitRadius = j.hit; p.maxAngle = j.maxAngle;
    if (mode === 'sim') {
      springStep(_tmp, p);
      j.prev[0] = j.cur[0]; j.prev[1] = j.cur[1]; j.prev[2] = j.cur[2];
      j.cur[0] = _tmp[0]; j.cur[1] = _tmp[1]; j.cur[2] = _tmp[2];
    } else if (mode === 'hang') {
      springHang(j.cur, p);
      j.prev[0] = j.cur[0]; j.prev[1] = j.cur[1]; j.prev[2] = j.cur[2];
    }
    // aim: tail direction in the bone's rest frame → rotation from the bind axis
    _sv.set(j.cur[0], j.cur[1], j.cur[2]).applyMatrix4(root.matrixWorld).sub(_sh).applyQuaternion(_sq.invert());
    if (_sv.lengthSq() < 1e-12) return;
    _sqa.setFromUnitVectors(j.axis, _sv.normalize());
    j.bone.quaternion.copy(j.initQ).multiply(_sqa);
    j.bone.updateMatrix();
    j.bone.matrixWorld.multiplyMatrices(parent.matrixWorld, j.bone.matrix);
  }
  function updateSprings(dt, now) {
    const S = springs;
    if (!S) return;
    const t0 = performance.now();
    // the procedural layers moved bones: refresh just the matrices the springs read
    root.updateWorldMatrix(true, false);
    for (const b of S.order) { b.updateMatrix(); b.matrixWorld.multiplyMatrices(b.parent.matrixWorld, b.matrix); }
    _rootInv.copy(root.matrixWorld).invert();
    _rootQ.setFromRotationMatrix(_rootInv);
    for (const c of S.colliders) {
      if (c.to) {
        toCentre(_sv.copy(c.off).applyMatrix4(c.bone.matrixWorld), c.shape.a);
        toCentre(_sv.copy(c.toOff).applyMatrix4(c.to.matrixWorld), c.shape.b);
      } else toCentre(_sv.copy(c.off).applyMatrix4(c.bone.matrixWorld), c.shape.c);
    }
    let jumped = !S.primed;
    if (head) {
      head.matrixWorld.decompose(_sv, _sq, _sScale); // refreshed above (head is in S.order)
      if (S.primed) jumped = poseJumped({ moved: _sv.distanceTo(S.lastPos), turned: S.lastQ.angleTo(_sq), dt });
      S.lastPos.copy(_sv); S.lastQ.copy(_sq);
    }
    S.primed = true;
    S.hold = Math.max(0, S.hold - Math.max(0, dt)); // seconds left to hang (clock-free: works for debug.run too)
    const hang = jumped || !life || reducedMotion || S.hold > 0;
    if (hang) {
      S.acc = 0; S.mode = 'hang';
      for (const ch of S.chains) for (const j of ch.joints) solveJoint(j, 'hang');
    } else {
      const st = springSubsteps(S.acc, dt, SPRING_H);
      S.acc = st.acc; S.mode = 'sim';
      if (!st.n) for (const ch of S.chains) for (const j of ch.joints) solveJoint(j, 'aim'); // keep tails world-steady
      for (let i = 0; i < st.n; i++) for (const ch of S.chains) for (const j of ch.joints) solveJoint(j, 'sim');
    }
    S.ms = S.ms ? S.ms * 0.95 + (performance.now() - t0) * 0.05 : performance.now() - t0;
  }
  // smallest gap between any spring tail and any collider surface (debug; < 0 = inside)
  function springClearance() {
    if (!springs) return null;
    let min = Infinity; let at = '';
    const nameOf = new Map(springs.colliders.map((c) => [c.shape, c.bone.name]));
    for (const ch of springs.chains) for (const j of ch.joints) for (const c of j.p.colliders) {
      let g;
      if (c.floorY !== undefined) g = j.cur[1] - c.floorY;
      else {
        const q = c.b ? closestOnSegment([0, 0, 0], j.cur, c.a, c.b) : c.c;
        g = Math.hypot(j.cur[0] - q[0], j.cur[1] - q[1], j.cur[2] - q[2]) - c.r;
      }
      if (g < min) { min = g; at = `${ch.name}→${nameOf.get(c) || 'floor'}`; }
    }
    return { min, at };
  }
  const resetSprings = (holdMs = 0) => {
    if (!springs) return;
    springs.primed = false;
    springs.hold = Math.max(springs.hold, (Number(holdMs) || 0) / 1000);
  };

  // Hair: a damped spring on the torso's motion drives a vertex-shader sway
  // (interim, and the fallback for GLBs without hair chains), plus a faint breeze.
  const sway = { x: 0, vx: 0, z: 0, vz: 0, prev: new THREE.Vector3(), prevYaw: 0, fresh: true };
  function updateSway(dt, t) {
    shared.uTime.value = t;
    const on = life && !reducedMotion && !hasChains;
    shared.uSwayAmp.value = on ? 0.004 * unitsPerMetre : 0;
    const sp = bones.Spine2;
    if (!on || !sp || dt <= 0) { sway.fresh = true; shared.uSway.value.set(0, 0); return; }
    sp.getWorldPosition(_v1);
    bones.Hips?.getWorldQuaternion(_q1);
    const bf = _v2.copy(hipsFwd).applyQuaternion(_q1);
    const yaw = Math.atan2(bf.x, bf.z);
    if (sway.fresh) { sway.prev.copy(_v1); sway.prevYaw = yaw; sway.fresh = false; return; }
    const vx = (_v1.x - sway.prev.x) / dt; const vz = (_v1.z - sway.prev.z) / dt;
    const yr = wrapPi(yaw - sway.prevYaw) / dt;
    sway.prev.copy(_v1); sway.prevYaw = yaw;
    const tx = clamp(-vx * 0.08 - yr * 0.01, -0.03, 0.03);
    const tzz = clamp(-vz * 0.08, -0.03, 0.03);
    const w0 = 7; const zeta = 0.35; const h2 = Math.min(dt, 1 / 30);
    sway.vx += (w0 * w0 * (tx - sway.x) - 2 * zeta * w0 * sway.vx) * h2; sway.x = clamp(sway.x + sway.vx * h2, -0.03, 0.03);
    sway.vz += (w0 * w0 * (tzz - sway.z) - 2 * zeta * w0 * sway.vz) * h2; sway.z = clamp(sway.z + sway.vz * h2, -0.03, 0.03);
    shared.uSway.value.set(sway.x * unitsPerMetre, sway.z * unitsPerMetre);
  }

  // Contact shadow: one soft blob quad (hips + chest when lying + feet), only in
  // full framing and asleep. No shadow map, no extra skinning pass.
  let shadowA = 0;
  function makeContactShadow() {
    const mat = new THREE.ShaderMaterial({
      uniforms: {
        uHip: { value: new THREE.Vector4() }, uFeet: { value: new THREE.Vector4() }, uW: { value: new THREE.Vector4() },
        uColor: { value: new THREE.Color(0x05070c) }, uAlpha: { value: 0 },
      },
      vertexShader: SHADOW_VERT, fragmentShader: SHADOW_FRAG, transparent: true, depthWrite: false,
    });
    const m = new THREE.Mesh(new THREE.PlaneGeometry(2.6, 2.6), mat);
    m.rotation.x = -Math.PI / 2;
    m.position.y = 0.002;
    m.frustumCulled = false;
    m.visible = false;
    m.name = 'klukai_contact_shadow';
    scene.add(m);
    return m;
  }
  function updateShadow(dt) {
    if (!shadowMesh || !bones.Hips) return;
    const want = framing === 'full' || stateName === 'sleep' ? 1 : 0;
    shadowA += (want - shadowA) * (1 - Math.exp(-dt * 3));
    shadowMesh.visible = shadowA > 0.01;
    if (!shadowMesh.visible) return;
    const u = shadowMesh.material.uniforms;
    const hp = bones.Hips.getWorldPosition(_v1);
    const cp = (bones.Spine2 || bones.Hips).getWorldPosition(_v2);
    u.uHip.value.set(hp.x, hp.z, cp.x, cp.z);
    const low = (y, a, b) => 1 - smoothstep(a, b, y);
    const lf = (bones.LeftToeBase || bones.LeftFoot)?.getWorldPosition(_f1);
    const rf = (bones.RightToeBase || bones.RightFoot)?.getWorldPosition(_f2);
    u.uFeet.value.set(lf?.x ?? 0, lf?.z ?? 0, rf?.x ?? 0, rf?.z ?? 0);
    u.uW.value.set(0.3, 0.35 * low(cp.y, 0.2, 0.6), lf ? 0.45 * low(lf.y, 0.04, 0.14) : 0, rf ? 0.45 * low(rf.y, 0.04, 0.14) : 0);
    u.uAlpha.value = shadowA;
    shadowMesh.position.x = hp.x; shadowMesh.position.z = hp.z;
  }

  // Mood rim colour (and ≤10 % of the sky fill) eases toward setTint().
  function updateTint(dt) {
    const k = 1 - Math.exp(-dt * 3.2); // ~1.2 s, like the stage's CSS background transition
    shared.uRimColor.value.lerp(rimGoal, k);
    hemi.color.copy(skyBase).lerp(shared.uRimColor.value, 0.08);
    camera.updateMatrixWorld();
    shared.uRimDir.value.copy(rimDirWorld).transformDirection(camera.matrixWorldInverse);
    _v1.copy(key.position).sub(key.target.position).transformDirection(camera.matrixWorldInverse);
    shared.uKeySide.value.set(_v1.x, _v1.y).normalize();
  }

  // ── loop ─────────────────────────────────────────────────────────────────
  scheduleBlink(performance.now());

  // Adaptive quality: every ~2 s compare the average frame time against the
  // budget and step the pixel ratio (between 1 and the device cap); at the
  // floor, drop the outlines rather than the frame rate.
  const adaptive = opts.adaptiveResolution !== false;
  let adaptWindows = 0;
  function syncOutlineRes() {
    renderer.getDrawingBufferSize(outlineU.uRes.value);
    outlineU.uDpr.value = renderer.getPixelRatio();
  }
  function adapt(now) {
    if (lastNow) { frameAcc += now - lastNow; frameN++; }
    lastNow = now;
    if (frameAcc < (adaptWindows < 3 ? 1000 : 2000)) return; // react quickly right after start
    adaptWindows++;
    const avg = frameAcc / frameN;
    fpsAvg = 1000 / avg;
    frameAcc = 0; frameN = 0;
    if (!adaptive) return;
    const cur = renderer.getPixelRatio();
    const q = nextQuality({ avgFrameMs: avg, ratio: cur, cap: rs.pixelRatio, outlines: outlinesOn, hasOutlines: outlineMeshes.length > 0 && opts.outlines !== false });
    if (q.ratio !== cur) { renderer.setPixelRatio(q.ratio); renderer.setSize(w, h, false); syncOutlineRes(); }
    if (q.outlines !== outlinesOn) setOutlines(q.outlines);
  }

  function tick() {
    rafId = 0;
    if (disposed || paused) return;
    timer.update();
    const dt = Math.min(timer.getDelta(), 0.1);
    const now = performance.now();
    const t = now / 1000;
    adapt(now);
    if (!disposed) sm.tick(); // a mood held by the hysteresis
    step(dt, now, dt);
    if (life && !reducedMotion) { // slow figure-eight drift, this frame only
      drift.set(Math.sin(t * 0.43) * 0.004, Math.sin(t * 0.29) * 0.003, 0);
      camera.position.add(drift);
    } else drift.set(0, 0, 0);
    renderer.render(scene, camera);
    camera.position.sub(drift);
    rafId = requestAnimationFrame(tick);
  }

  // One frame of animation + procedural layers + camera (no render). `mixerDt` 0
  // holds the clip still while the layers settle (debug.poseAt).
  function step(dt, now, mixerDt) {
    const t = now / 1000;
    // speaking: smooth the level, layer the talking clip over the base state
    speaking += (speakingTarget - speaking) * Math.min(1, dt * 12);
    if (talkAction && stateName !== 'talking' && stateName !== 'sleep') {
      const goal = speaking > 0.04 ? Math.min(0.85, 0.35 + speaking) : 0;
      talkWeight += (goal - talkWeight) * Math.min(1, dt * 4);
      talkAction.setEffectiveWeight(talkWeight);
    } else if (talkAction) {
      talkWeight = 0;
      if (stateName !== 'talking') talkAction.setEffectiveWeight(0);
    }
    // three's PropertyMixer only writes a bone whose mixed value CHANGED since the last
    // update, so on a flat stretch of a track (or a held pose) the bone would keep last
    // frame's procedural rotation and every layer below would pile up on it. Put the
    // clean clip pose back first; the mixer then overwrites it whenever it has news.
    for (const c of clean) c.bone.quaternion.copy(c.q);
    mixer.update(mixerDt);
    for (const c of clean) c.q.copy(c.bone.quaternion);
    // procedural layers on top of the clip pose: arms → breathing → look-at → nods → eye guard → blink
    applyArms(dt, t);
    applyBreath(dt);
    applyLookAt(dt, now);
    applyTalk(dt, now);
    applyEyeGuard(dt);
    applyBlink(dt, now);
    updateSprings(dt, now);
    updateSway(dt, t);
    updateShadow(dt);

    if (bones.Hips && !sideView) { // keep her centred on a narrow phone without chasing every sway
      follow += (bones.Hips.getWorldPosition(_v1).x * 0.85 - follow) * (1 - Math.exp(-dt * 0.8));
      camGoal.target.x = follow;
    }
    placeCamera(dt);
    updateTint(dt);
  }
  function start() { if (!rafId && !disposed && !paused) { timer.reset(); sway.fresh = true; resetSprings(); rafId = requestAnimationFrame(tick); } }
  function stop() { if (rafId) cancelAnimationFrame(rafId); rafId = 0; lastNow = 0; frameAcc = 0; frameN = 0; }

  // ── lifecycle ────────────────────────────────────────────────────────────
  // One rule for the render loop: it runs only while the page is visible, the
  // GL context is live, and the caller hasn't parked the view (setPaused).
  function syncPaused() {
    paused = (typeof document !== 'undefined' && document.hidden) || contextLost || userPaused;
    if (paused) stop(); else start();
  }
  onVisibility = syncPaused;
  onLost = (e) => { e.preventDefault(); contextLost = true; syncPaused(); };
  onRestored = () => { contextLost = false; resetSprings(); frame({ snap: true }); syncPaused(); };
  onPointer = (e) => {
    if (e.pointerType !== 'mouse') return; // touch: she looks at the camera
    const r = canvas.getBoundingClientRect();
    if (!r.width || !r.height) return;
    pointer.x = ((e.clientX - r.left) / r.width) * 2 - 1;
    pointer.y = -((e.clientY - r.top) / r.height) * 2 + 1;
    pointer.until = performance.now() + 2500;
  };
  onPointerLeave = () => { pointer.until = 0; };
  onMotionPref = () => { reducedMotion = opts.reducedMotion === true || Boolean(motionMq?.matches); };
  document.addEventListener('visibilitychange', onVisibility);
  canvas.addEventListener('webglcontextlost', onLost, false);
  canvas.addEventListener('webglcontextrestored', onRestored, false);
  canvas.addEventListener('pointermove', onPointer, { passive: true });
  canvas.addEventListener('pointerleave', onPointerLeave, { passive: true });
  motionMq?.addEventListener?.('change', onMotionPref);
  if (typeof ResizeObserver === 'function') { ro = new ResizeObserver(() => api.resize()); ro.observe(container); }
  syncOutlineRes();

  function disposeObject(obj) {
    obj?.traverse?.((o) => {
      if (o.geometry) o.geometry.dispose();
      const mats = o.material ? (Array.isArray(o.material) ? o.material : [o.material]) : [];
      for (const m of mats) {
        for (const k of Object.keys(m)) { const v = m[k]; if (v && v.isTexture) v.dispose(); }
        m.dispose();
      }
    });
  }
  function teardown() {
    if (disposed) return;
    disposed = true;
    opts.signal?.removeEventListener('abort', onAbort);
    stop();
    if (onVisibility) document.removeEventListener('visibilitychange', onVisibility);
    if (onLost) canvas.removeEventListener('webglcontextlost', onLost);
    if (onRestored) canvas.removeEventListener('webglcontextrestored', onRestored);
    if (onPointer) canvas.removeEventListener('pointermove', onPointer);
    if (onPointerLeave) canvas.removeEventListener('pointerleave', onPointerLeave);
    if (onMotionPref) motionMq?.removeEventListener?.('change', onMotionPref);
    ro?.disconnect();
    if (controls) {
      if (onControlStart) controls.removeEventListener('start', onControlStart);
      if (onControlEnd) controls.removeEventListener('end', onControlEnd);
      controls.dispose();
    }
    timer.dispose?.();
    if (mixer) {
      mixer.removeEventListener('finished', onFinished);
      mixer.removeEventListener('loop', onLoop);
      mixer.stopAllAction(); mixer.uncacheRoot(root);
    }
    disposeObject(root); // outline meshes live in the model tree (shared geometry: a second dispose is a no-op)
    disposeObject(shadowMesh);
    shadowMesh?.removeFromParent();
    for (const t of ownedTextures) t.dispose();
    renderer.dispose();
    try { renderer.forceContextLoss(); } catch { /* already lost */ }
    canvas.remove();
    actions.clear(); fidgetActions.clear(); skinned = []; outlineMeshes = []; root = null; mixer = null;
  }

  const api = {
    get ready() { return ready; },
    get state() { return stateName; },
    /** Load timings (ms since create) + live render stats, for logging/diagnostics. */
    get stats() {
      const i = renderer.info;
      return {
        ...stats, t0: undefined, pixelRatio: renderer.getPixelRatio(), drawCalls: i.render.calls, triangles: i.render.triangles,
        programs: i.programs?.length ?? 0, textures: i.memory.textures, fps: Math.round(fpsAvg), outlines: outlinesOn, headJoint,
        springMs: springs ? +springs.ms.toFixed(3) : null,
      };
    },
    /** Set a state directly (thinking, talking, greet, …): always immediate. */
    setState(name) {
      if (disposed) return;
      sm.setState(name);
    },
    /** Follow one of her moods. Mood-to-mood changes hold for 4 s (strong moods
     *  excepted); a state set with setState() is always left at once. */
    setMood(mood) {
      if (disposed) return;
      sm.setMood(mood);
    },
    setSpeaking(level) {
      const v = Number(level);
      speakingTarget = Number.isFinite(v) ? Math.min(1, Math.max(0, v)) : 0;
    },
    setFraming(f) { framing = f === 'full' ? 'full' : 'upper'; if (root) frame(); },
    /** Distance multiplier: below 1 zooms in, above 1 zooms out. */
    zoomBy(factor) { changeView(() => orbit.zoomBy(factor)); },
    /** Horizontal camera angle in radians, relative to the current view. */
    rotateBy(radians) { changeView(() => orbit.rotateBy(radians)); },
    resetView() { changeView(() => orbit.reset()); },
    /** Mood rim colour: 0xRRGGBB, '#rrggbb' (or a RIM_TINTS key); eases over ~1.2 s. */
    setTint(color) {
      const c = parseTint(color);
      if (c === null || disposed) return;
      rimGoal.setHex(c);
      if (paused) { shared.uRimColor.value.copy(rimGoal); hemi.color.copy(skyBase).lerp(rimGoal, 0.08); }
    },
    /** CSS px of the canvas covered by the topbar (top) and the dock (bottom). */
    setInsets({ top = 0, bottom = 0 } = {}) {
      if (disposed) return;
      const ok = (v) => (Number.isFinite(Number(v)) ? Math.max(0, Number(v)) : 0);
      const next = { top: ok(top), bottom: ok(bottom) };
      if (next.top === insetsPx.top && next.bottom === insetsPx.bottom) return; // resize events repeat it
      insetsPx = next;
      if (root) { frame({ snap: true }); if (paused) renderer.render(scene, camera); }
    },
    resize() {
      if (disposed) return;
      ({ w, h } = size());
      const s = renderSettings({ devicePixelRatio: globalThis.devicePixelRatio, width: w, height: h, isIOS, iosMajor });
      rs = s; // new cap (orientation/size change); keep any adaptive step below it
      renderer.setPixelRatio(Math.min(renderer.getPixelRatio(), s.pixelRatio));
      renderer.setSize(w, h, false);
      syncOutlineRes();
      frame({ snap: true });
      if (paused) renderer.render(scene, camera);
    },
    /** Park the view while another one shows: no render loop until setPaused(false). */
    setPaused(on) {
      userPaused = Boolean(on);
      if (on && !disposed) { shared.uRimColor.value.copy(rimGoal); updateTint(0); } // parked: no easing to wait for
      if (!disposed && ready) syncPaused();
    },
    dispose() { if (!disposed) teardown(); },
  };

  if (opts.debug) {
    // Test/inspection hook only (tools/avatar): direct access to the scene graph.
    api.debug = {
      THREE, scene, camera, renderer, get root() { return root; }, controls, frame, mixer, shared, outlineU, hemi, key,
      get outlines() { return outlineMeshes; }, setOutlines, look, get headJoint() { return headJoint; },
      materials() { const out = []; root?.traverse((o) => { if (o.isMesh) out.push(...[].concat(o.material)); }); return out; },
      /** Snapshot of the procedural layers (tools/avatar/debug/life.py). */
      probe() {
        const q = new THREE.Quaternion(); const f = new THREE.Vector3();
        if (faceBone) { faceBone.getWorldQuaternion(q); f.copy(restFwd).applyQuaternion(q); }
        return {
          state: sm.state, source: sm.source, pending: sm.pending, mood: sm.mood, clip: current?.action?.getClip().name, fidget: Boolean(current?.fidget),
          lookW: look.w, lookYaw: look.yaw, lookPitch: look.pitch, faceYaw: Math.atan2(f.x, f.z), facePitch: Math.asin(Math.max(-1, Math.min(1, f.y))),
          armW, pushIn, talkPresence, nods: nods.length, blink: blinkIdx >= 0 ? skinned.find((m) => m.morphTargetInfluences)?.morphTargetInfluences[blinkIdx] ?? 0 : null,
          lid, drift: drift.toArray(), swayAmp: shared.uSwayAmp.value, sway: shared.uSway.value.toArray(), shadowA, shadowVisible: Boolean(shadowMesh?.visible),
          fov: camera.fov, cam: camera.position.toArray(), target: camCur.target.toArray(), reducedMotion, outlines: outlinesOn, headJoint,
          orbit: { ...orbit.state, dragging: orbitDragging }, follow,
          eyeElevDeg: guard.e0 / DEG, eyeElevAfterDeg: guard.e1 / DEG, guardLiftDeg: guard.lift / DEG, guardW: guard.w,
          springs: springs ? { mode: springs.mode, ms: springs.ms, chains: springs.chains.map((c) => c.name), clearance: springClearance(),
            tips: springs.chains.map((c) => c.joints[c.joints.length - 1].cur.slice()) } : null,
          rim: shared.uRimColor.value.getHexString(),
        };
      },
      /** Hold clip `name` at `t` s (alone, full weight) as state `state`, let the procedural
       *  layers settle for `frames` steps, render once, and return probe() plus the screen
       *  position of her eyes (CSS px) — eyes open. For tools/avatar/debug/eyes.py. */
      poseAt(name, t, { state = 'idle', frames = 60, guard: g = true } = {}) {
        const a = actions.get(name);
        if (!a) return null;
        userPaused = true; syncPaused();
        if (STATES.includes(state)) { stateName = state; sm.settle(state); frame({ snap: true }); }
        for (const x of [...actions.values(), ...fidgetActions.values()]) { x.stop(); x.setEffectiveWeight(0); }
        a.reset(); a.setLoop(THREE.LoopRepeat, Infinity); a.setEffectiveWeight(1); a.play(); a.time = Number(t) || 0;
        current = { state, action: a, once: false };
        speakingTarget = 0; speaking = 0; talkWeight = state === 'talking' ? 1 : 0;
        resetSprings();
        GUARD.on = g !== false;
        const t0 = performance.now();
        for (let i = 0; i < frames; i++) { blinkT = -1; nextBlinkAt = Infinity; step(1 / 30, t0 + i * 33.3, 0); }
        for (const m of skinned) if (m.morphTargetInfluences && blinkIdx >= 0) m.morphTargetInfluences[blinkIdx] = 0;
        camera.updateMatrixWorld();
        renderer.render(scene, camera);
        const bone = faceBone || head;
        const eye = eyeLocal.clone().applyMatrix4(bone.matrixWorld);
        const top = eye.clone().add(new THREE.Vector3(0, 0.12, 0));
        const px = (v) => { const n = v.clone().project(camera); return [(n.x + 1) / 2 * w, (1 - n.y) / 2 * h]; };
        GUARD.on = true;
        return { ...api.debug.probe(), eyePx: px(eye), headTopPx: px(top), clipDur: a.getClip().duration };
      },
      guard: GUARD,
      /** Play an idle fidget now (normally 35 % after a full idle cycle); returns its name. */
      fidget() { if (stateName === 'idle') playFidget(); return current?.fidget ? lastFidget : null; },
      /** Pose the current clip at `t` seconds, alone at full weight (comparable stills). */
      seek(t) {
        if (!current?.action) return;
        for (const a of actions.values()) if (a !== current.action) a.setEffectiveWeight(0);
        current.action.enabled = true; current.action.setEffectiveWeight(1); current.action.time = Number(t) || 0;
        mixer.update(0);
        resetSprings(); updateSprings(0, performance.now()); // hang the hair for the still
      },
      /** Advance clips and every procedural layer by `seconds` at a fixed `dt` without
       *  real time (deterministic motion tests), then render once. `speaking` is a level
       *  or (t) => level. The render loop is parked while it runs. */
      run(seconds, { dt = 1 / 30, speaking = null } = {}) {
        userPaused = true; syncPaused();
        simClock = Math.max(simClock, performance.now() / 1000); // blinks/fidgets/saccades are scheduled on real time
        const n = Math.max(1, Math.round((Number(seconds) || 0) / dt));
        for (let i = 0; i < n; i++) {
          simClock += dt;
          if (speaking !== null) speakingTarget = typeof speaking === 'function' ? clamp(Number(speaking(simClock)) || 0, 0, 1) : clamp(Number(speaking) || 0, 0, 1);
          sm.tick();
          step(dt, simClock * 1000, dt);
        }
        camera.updateMatrixWorld();
        renderer.render(scene, camera);
        return api.debug.probe();
      },
      resetSprings,
      /** Live spring tuning: { long|lock: { stiffness:[root, tip], gravity, drag, hitRadius } }. */
      tuneSprings(set = {}) {
        if (!springs) return false;
        for (const ch of springs.chains) {
          const kind = /side|lock|front/i.test(ch.name || '') ? 'lock' : 'long';
          const p = { ...SPRING[kind], ...(set[kind] || {}) };
          ch.joints.forEach((j, i) => {
            const f = ch.joints.length > 1 ? i / (ch.joints.length - 1) : 0;
            j.stiffness = p.stiffness[0] + (p.stiffness[1] - p.stiffness[0]) * f; j.gravity = p.gravity; j.drag = p.drag; j.hit = p.hitRadius;
            j.maxAngle = (p.maxAngle || 0) * DEG;
          });
        }
        resetSprings();
        return true;
      },
      /** Start clip `name` at `t` s as state `state` (no settle; follow with run()). */
      playClip(name, t = 0, { state = 'idle' } = {}) {
        const a = actions.get(name);
        if (!a) return false;
        userPaused = true; syncPaused();
        if (STATES.includes(state)) { stateName = state; sm.settle(state); }
        for (const x of [...actions.values(), ...fidgetActions.values()]) { if (x !== a) { x.stop(); x.setEffectiveWeight(0); } }
        a.reset(); a.setLoop(THREE.LoopRepeat, Infinity); a.setEffectiveWeight(1); a.play(); a.time = Number(t) || 0;
        current = { state, action: a, once: false };
        return true;
      },
    };
  }
  // Compile shaders off the critical path where the browser supports it
  // (KHR_parallel_shader_compile), so the first frame doesn't hitch.
  try { if (renderer.compileAsync) await renderer.compileAsync(scene, camera); } catch { /* fall back to lazy compile */ }
  if (disposed) return stub;
  mark('compileMs');
  updateTint(0);
  renderer.render(scene, camera);
  mark('firstFrameMs');
  ready = true;
  syncPaused();
  try { onReady?.(api); } catch { /* caller's handler threw */ }
  return api;
}

function hasWebGL() {
  try {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl2') || c.getContext('webgl');
    gl?.getExtension('WEBGL_lose_context')?.loseContext();
    return !!gl;
  } catch { return false; }
}
