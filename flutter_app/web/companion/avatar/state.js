import { clamp } from './common.js';
import { MOODS } from '../logic.js';

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
export const MOOD_STATE = Object.freeze({
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

export const REACTION_CATALOG = Object.freeze([
  { id: 'salute', clip: 'salute', label: 'Salute', category: 'Everyday' },
  { id: 'acknowledge', clip: 'thankful', label: 'Thank you', category: 'Everyday' },
  { id: 'pleased', clip: 'happy', label: 'Pleased', category: 'Playful' },
  { id: 'thoughtful', clip: 'thinking', label: 'Thoughtful', category: 'Everyday' },
  { id: 'dismiss', clip: 'dismiss', label: 'Dismissive', category: 'Everyday' },
  { id: 'bashful', clip: 'bashful', label: 'Bashful', category: 'Playful' },
  { id: 'playful', clip: 'excited', label: 'Excited', category: 'Playful' },
  { id: 'nervous', clip: 'nervous', label: 'Shy fidget', category: 'Everyday' },
]);
/** Reactions are transient clips, so the mood/state machine keeps its context. */
export function createReactionController({ available = [], catalog = REACTION_CATALOG, play = () => {}, restore = () => {}, now = () => performance.now(), cooldown = 2600 } = {}) {
  let active = null, lastAt = -Infinity, count = 0, nextAuto = 0;
  const choices = catalog.filter((entry) => available.includes(entry.clip));
  const byId = new Map(choices.map((entry) => [entry.id, entry]));
  const automatic = choices.filter((entry) => entry.auto !== false);
  return {
    get active() { return active; },
    get count() { return count; },
    get choices() { return choices.map(({ id, label, category }) => ({ id, label, category })); },
    trigger(kind = 'auto') {
      if (active || now() - lastAt < cooldown) return false;
      const entry = kind === 'auto' ? automatic[nextAuto % automatic.length] : byId.get(kind);
      if (!entry) return false;
      if (kind === 'auto') nextAuto++;
      active = { kind: entry.id, clip: entry.clip };
      lastAt = now(); count++;
      play(active);
      return true;
    },
    finish() { if (active) { active = null; restore(); } },
    cancel() { active = null; },
  };
}

/** Installed face-family fallback, calibrated to its real lip rim and cavity in
 * bind-pose metres. The narrow upper/lower lip gap opens; cheeks fade to rest.
 * Used only on the known Clukay face atlases; other models use authored morphs. */
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
export function pickFidget(names, prev, rand = Math.random) {
  const pool = names.length > 1 ? names.filter((n) => n !== prev) : names.slice();
  if (!pool.length) return null;
  return pool[Math.min(pool.length - 1, Math.floor(rand() * pool.length))];
}

/** Calm visible time between passive gestures. Priority activity cancels the
 * current gesture and starts a fresh wait; suspended time is never replayed.
 * The renderer supplies restore(), so the latest mood remains authoritative. */
export function createPassiveController({ available = [], play = () => {}, restore = () => {}, random = Math.random } = {}) {
  const names = [...new Set(available)];
  const delay = () => 12000 + clamp(Number(random()) || 0, 0, 1) * 12000;
  let active = null, last = null, count = 0, remainingMs = delay(), blocked = false;
  const allowed = ({ state = 'idle', speaking = 0, reacting = false, dragging = false,
    paused = false, reducedMotion = false, enabled = true } = {}) =>
    enabled && !paused && !reducedMotion && state === 'idle' && speaking <= 0.04 && !reacting && !dragging;
  const controller = {
    get active() { return active; },
    get last() { return last; },
    get count() { return count; },
    get remainingMs() { return remainingMs; },
    trigger(context = {}) {
      if (active || !allowed(context)) return false;
      const name = pickFidget(names, last, random);
      if (!name) return false;
      active = name; last = name; count++;
      play(name);
      return true;
    },
    cancel({ restore: resume = true } = {}) {
      const wasActive = active !== null;
      active = null; remainingMs = delay();
      if (wasActive && resume) restore();
    },
    finish() { if (active) controller.cancel(); },
    tick(dt, context = {}) {
      if (!allowed(context)) {
        if (!blocked || active) controller.cancel();
        blocked = true;
        return false;
      }
      if (blocked) { blocked = false; remainingMs = delay(); return false; }
      if (dt > 1) { controller.cancel(); return false; }
      if (active) return false;
      remainingMs = Math.max(0, remainingMs - Math.max(0, Number(dt) || 0) * 1000);
      return remainingMs <= 0 && controller.trigger(context);
    },
  };
  return controller;
}

// Short authored one-shots, crossfaded in/out without looping their endpoints.
// The idle excerpts retain her calm weight shifts and composed ready posture.
export const FIDGETS = Object.freeze([
  { name: 'thankful' },
  { name: 'fidget_happy', from: 'happy', start: 1.0, end: 4.0 },
  { name: 'fidget_glance', from: 'thinking', start: 0.2, end: 3.2 },
  { name: 'fidget_shift', from: 'idle', start: 5.0, end: 8.5 },
  { name: 'fidget_ready', from: 'idle', start: 11.5, end: 15.5 },
]);
