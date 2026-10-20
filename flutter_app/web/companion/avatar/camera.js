import { DEG, clamp, smoothstep, wrapPi } from './common.js';

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

export const BASE_FOV = 26;
export const UPPER_PORTRAIT_FOV = 22; // telephoto close-up on phones: flatter, rounder anime face

/** Framings: 'full' (whole body), 'upper' (head to hips) and 'waist' — the
 *  PWA's embedded 3D view (Ani-style): the upper crop with the camera level
 *  with her chest, so she fills a phone frame and is seen from slightly below
 *  (her eyes stay clear of the cap's brim; see EYE_GUARD). */
export function normalizeFraming(framing) {
  return framing === 'full' || framing === 'waist' ? framing : 'upper';
}
const WAIST_EYE_DROP = 0.04; // m below the target at the design distance: a touch of upward view

// Framing as it was designed when the canvas was exactly the visible band
// between the topbar and the dock (at BASE_FOV).
export function bandFraming({ framing, state, aspect, headY }) {
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
  return { targetY, camY: framing === 'waist' ? targetY - WAIST_EYE_DROP : targetY + 0.06, dist };
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
  let top = clamp(Number(insets?.top) || 0, 0, 0.9);
  let bottom = clamp(Number(insets?.bottom) || 0, 0, 0.9);
  const lowBody = framing === 'full' || state === 'sleep';
  // Short landscape screens can genuinely lose over half their height to the
  // dock. Fit that measured space, keeping the fade margin proportional when
  // little room remains. Only degenerate/overlapping insets need a safety floor.
  if (lowBody && bottom > 0) bottom += Math.min(0.08, Math.max(0, 1 - top - bottom) * 0.2);
  let band = 1 - top - bottom;
  // Preserve a usable projection when overlapping UI insets leave no real band.
  if (band < 0.22) { const s = 0.78 / (top + bottom); top *= s; bottom *= s; band = 0.22; }
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

export const CAMERA_ZOOM_RANGE = Object.freeze({ min: 0.2, max: 3 });
const STANDARD_LIMITS = Object.freeze({
  zoom: CAMERA_ZOOM_RANGE,
  pitch: Object.freeze({ min: -0.22, max: 0.12 }),
  panY: Object.freeze({ min: -1.2, max: 1.2 }),
});
/** The embedded view's freer camera: pinch from a face close-up (0.36) to her
 *  whole body and beyond (3), drag ↕ far enough to centre her shoes or the
 *  space above her cap even when pinched in. */
export const WAIST_LIMITS = Object.freeze({
  zoom: Object.freeze({ min: 0.36, max: 3 }),
  pitch: STANDARD_LIMITS.pitch,
  panY: Object.freeze({ min: -1.7, max: 1.7 }),
});
/** User-view limits for a framing; 'full'/'upper' keep the stage's originals. */
export function cameraLimits(framing) {
  return framing === 'waist' ? WAIST_LIMITS : STANDARD_LIMITS;
}

/** Where the waist framing aims as he pinches: up to her face (eyes, just
 *  under the brim, ~3 cm above the head bone) at the closest zoom, down to her
 *  middle once her whole body shows. */
export const WAIST_AIM = Object.freeze({ faceAbove: 0.03, bodyY: 0.8, fullZoom: 2.1 });

/**
 * Pure: vertical aim offset (m) added to the framing target for a user zoom.
 * Only the waist framing aims (not asleep: then she is framed from the side).
 * Smoothstep both ways, so the default view (zoom 1) is flat and continuous.
 */
export function zoomAim({ framing, state = 'idle', zoom = 1, targetY = 0, headY = 1.36 } = {}) {
  if (framing !== 'waist' || state === 'sleep') return 0;
  const z = Number(zoom), y = Number(targetY), head = Number(headY);
  if (![z, y, head].every(Number.isFinite)) return 0;
  if (z < 1) return (head + WAIST_AIM.faceAbove - y) * smoothstep(1, WAIST_LIMITS.zoom.min, z);
  return (WAIST_AIM.bodyY - y) * smoothstep(1, WAIST_AIM.fullZoom, z);
}

/**
 * Pure: where the camera's slow horizontal follow heads, and how fast (1/s).
 * By default it trails her hips (the idle clip drifts ~10 cm sideways; she
 * stays centred on a narrow phone without chasing every sway). Pinched in on
 * the waist framing it follows her head, and quicker, so a face close-up
 * keeps her face in frame while she sways.
 */
export function followGoal({ framing, state = 'idle', zoom = 1, hipsX = 0, headX = Number.NaN } = {}) {
  const hips = Number.isFinite(hipsX) ? hipsX * 0.85 : 0;
  if (framing !== 'waist' || state === 'sleep' || !Number.isFinite(headX)) return { x: hips, rate: 0.8 };
  const w = smoothstep(1, WAIST_LIMITS.zoom.min, Number.isFinite(zoom) ? zoom : 1);
  return { x: hips + (headX - hips) * w, rate: 0.8 + 2.7 * w };
}

/** Pure: the contact shadow is drawn whenever her feet can be on screen. */
export function contactShadowWanted({ framing, state = 'idle', zoom = 1 } = {}) {
  return framing === 'full' || state === 'sleep' || (framing === 'waist' && zoom >= 1.5);
}

/** Keep the model inside the far plane even when a small visible band requires
 *  a distant camera. The 3 m margin covers her body and animated accessories;
 *  whole-metre steps avoid rebuilding the projection for sub-pixel movement. */
export function cameraFarPlane(distanceFromOrigin = 0) {
  return Math.max(30, Math.ceil(distanceFromOrigin + 3));
}

/** User inspection offsets, relative to the animated automatic framing.
 *  Absorb only changes since the last camera placement: speech push-in and
 *  resize/state framing must never feed back into the user's zoom or orbit.
 *  Yaw wraps after each turn; horizontal orbit has no stops. */
export function createCameraView(initial = null, limits = STANDARD_LIMITS) {
  const { min: minZoom, max: maxZoom } = limits.zoom;
  const { min: minPitch, max: maxPitch } = limits.pitch;
  const { min: minPan, max: maxPan } = limits.panY;
  let dTheta = 0, dPhi = 0, zoom = 1, panY = 0;
  let applied = null;
  const set = (value) => {
    const finite = (v, fallback) => Number.isFinite(v) ? v : fallback;
    dTheta = wrapPi(finite(value?.dTheta, 0));
    dPhi = clamp(finite(value?.dPhi, 0), minPitch, maxPitch);
    zoom = clamp(finite(value?.zoom, 1), minZoom, maxZoom);
    panY = clamp(finite(value?.panY, 0), minPan, maxPan);
    applied = null;
  };
  set(initial);
  return {
    get state() { return { dTheta, dPhi, zoom, panY }; },
    set,
    absorb(observed) {
      if (!applied || !Number.isFinite(observed.radius) || observed.radius <= 0
          || !Number.isFinite(observed.theta) || !Number.isFinite(observed.phi)) return;
      dTheta = wrapPi(dTheta + wrapPi(observed.theta - applied.theta));
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
        minAzimuthAngle: -Infinity, maxAzimuthAngle: Infinity,
        minPolarAngle: base.phi + minPitch, maxPolarAngle: base.phi + maxPitch,
      };
    },
    zoomBy(factor) { if (Number.isFinite(factor) && factor > 0) zoom = clamp(zoom * factor, minZoom, maxZoom); },
    rotateBy(radians) { if (Number.isFinite(radians)) dTheta = wrapPi(dTheta + radians); },
    moveBy(metres) { if (Number.isFinite(metres)) panY = clamp(panY - metres, minPan, maxPan); },
    reset() { set(null); },
    get limits() { return limits; },
  };
}

const VIEW_ZERO = Object.freeze({ dTheta: 0, dPhi: 0, zoom: 1, panY: 0 });

/** Pure: a view `k` (0..1) of the way from `from` to `to`. Yaw takes the short
 *  way round the seam; zoom eases geometrically (even speed in and out). */
export function blendView(from, to, k) {
  const t = clamp(Number(k) || 0, 0, 1);
  const read = (view, key) => (Number.isFinite(view?.[key]) ? view[key] : VIEW_ZERO[key]);
  const zoomOf = (view) => { const z = read(view, 'zoom'); return z > 0 ? z : 1; };
  const a = { dTheta: read(from, 'dTheta'), dPhi: read(from, 'dPhi'), zoom: zoomOf(from), panY: read(from, 'panY') };
  const b = { dTheta: read(to, 'dTheta'), dPhi: read(to, 'dPhi'), zoom: zoomOf(to), panY: read(to, 'panY') };
  if (t === 0) return a;
  if (t === 1) return b;
  return {
    dTheta: wrapPi(a.dTheta + wrapPi(b.dTheta - a.dTheta) * t),
    dPhi: a.dPhi + (b.dPhi - a.dPhi) * t,
    zoom: a.zoom * Math.pow(b.zoom / a.zoom, t),
    panY: a.panY + (b.panY - a.panY) * t,
  };
}

/** Pure: a short ease-out (cubic) from one view to another, sampled by time. */
export function createViewTween({ from, to = VIEW_ZERO, at = 0, ms = 380 } = {}) {
  const span = Math.max(1, Number(ms) || 0);
  return {
    value(now) {
      const p = clamp((Number(now) - at) / span, 0, 1);
      return { view: blendView(from, to, 1 - (1 - p) ** 3), done: p >= 1 };
    },
  };
}

/** Taps → double taps: the second press within `ms` of the first release
 *  (like iOS/Android) and within `slop` px. The tap after a double tap starts
 *  a new pair. `downAt`/`upAt` are the tap's press and release times. */
export function createDoubleTap({ ms = 350, slop = 36 } = {}) {
  let last = null;
  return {
    tap(x, y, downAt, upAt = downAt) {
      if (![x, y, downAt, upAt].every(Number.isFinite)) { last = null; return false; }
      const double = Boolean(last && downAt - last.upAt <= ms && downAt >= last.downAt
        && Math.hypot(x - last.x, y - last.y) <= slop);
      last = double ? null : { x, y, downAt, upAt };
      return double;
    },
    reset() { last = null; },
  };
}

/** Pointer intent shared by touch and mouse: a pinch remains a pinch until all
 * fingers lift; small jitter is a tap, and a cancelled drag can never be a tap. */
export function createAvatarGesture({ threshold = 7, tapMs = 650 } = {}) {
  const points = new Map();
  let blocked = false;
  return {
    get active() { return points.size > 0; },
    down(id, x, y, at) {
      if (!points.size) blocked = false;
      points.set(id, { x, y, startX: x, startY: y, at, dragged: false });
      if (points.size > 1) blocked = true;
    },
    move(id, x, y) {
      const p = points.get(id);
      if (!p || blocked) return null;
      if (!p.dragged && Math.hypot(x - p.startX, y - p.startY) <= threshold) return null;
      p.dragged = true;
      const delta = { dx: x - p.x, dy: y - p.y };
      p.x = x; p.y = y;
      return delta;
    },
    up(id, x, y, at) {
      const p = points.get(id);
      points.delete(id);
      return p && !blocked && !p.dragged && at - p.at <= tapMs
        && Math.hypot(x - p.startX, y - p.startY) <= threshold ? { x, y, downAt: p.at, upAt: at } : null;
    },
    cancel(id) {
      // Losing capture for one finger must not forget the other fingers: a
      // replacement finger is still part of that pinch, never a new tap.
      if (id === undefined) points.clear(); else points.delete(id);
      blocked = true;
    },
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
