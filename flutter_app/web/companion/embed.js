// Embedded stage: the PWA's Chat tab shows this page in a same-origin iframe,
// companion.html?embed=1&mode=3d, between its top bar and its composer (no
// PWA widget overlaps the frame: Flutter overlays over an iframe flicker on
// iOS). Embedded, the stage hides its own chrome, always shows 3D framed
// waist-up, keeps its socket (the avatar reacts to the chat) and speaks only
// on a tap. The PWA keeps it loaded behind its CHAT face and says when it is
// on screen (`hidden=1` in the URL when it starts behind it); off screen it
// neither draws nor plays.
// Pure helpers, so node --test can check the contract without a browser.

export const SPEAK = "klukai:speak";
export const VISIBILITY = "klukai:visibility";
export const HUD = "klukai:hud";         // PWA → stage: what the stage's HUD shows
export const HERDAY = "klukai:herday";   // stage → PWA: he tapped WEARING (open her day)
export const MAX_SPEAK_CHARS = 4000;
export const MAX_LINE_CHARS = 4000;
const MAX_LABEL_CHARS = 60;
const MAX_WEARING_CHARS = 120;
const MAX_STATUS_CHARS = 240;
const MAX_ID_CHARS = 128;
const INSET_MAX = 600;

/**
 * Embed flags from location.search; the 3D view is forced when embedded.
 * `hidden` means the PWA loaded the stage behind its CHAT face. The insets
 * (CSS px of the frame covered by PWA chrome) are 0 unless given: the PWA no
 * longer draws over the frame.
 */
export function embedConfig(search = "") {
  const params = new URLSearchParams(search);
  const embed = params.get("embed") === "1";
  const inset = (name, fallback) => {
    const raw = params.get(name);
    const n = raw === null || raw.trim() === "" ? Number.NaN : Number(raw);
    return Number.isFinite(n) ? Math.round(Math.min(Math.max(n, 0), INSET_MAX)) : fallback;
  };
  return {
    embed,
    mode: embed ? "model" : null,
    hidden: embed && params.get("hidden") === "1",
    insets: { top: inset("inset_top", 0), bottom: inset("inset_bottom", 0) },
  };
}

/** The data of a `type` message from the same-origin parent window, or null. */
function fromParent(event, { origin, parent }, type) {
  if (!event || typeof origin !== "string" || event.origin !== origin) return null;
  if (!parent || event.source !== parent) return null;
  const data = event.data;
  if (!data || typeof data !== "object" || data.type !== type) return null;
  return data;
}

/**
 * A speak request from the embedding page, or null. Only the same-origin
 * parent window may ask; other origins, other frames and malformed data are
 * ignored. The text is trimmed and capped.
 */
export function speakRequest(event, { origin, parent }) {
  const data = fromParent(event, { origin, parent }, SPEAK);
  if (!data || typeof data.text !== "string") return null;
  const text = data.text.trim().slice(0, MAX_SPEAK_CHARS);
  if (!text) return null;
  const id = typeof data.id === "number" && Number.isFinite(data.id) ? data.id
    : typeof data.id === "string" && data.id ? data.id : null;
  return { text, id };
}

/** What the HUD shows before (or without) a snapshot. */
export const EMPTY_HUD = Object.freeze({
  level: 0, mood: "", bpm: 0, spiking: false, wearing: "", line: "", lineId: null, streaming: false, status: null,
});

/** Trim and cap a string without splitting a UTF-16 surrogate pair. */
function capText(text, max) {
  const value = text.trim();
  if (value.length <= max) return value;
  let cut = max;
  const last = value.charCodeAt(cut - 1);
  if (last >= 0xd800 && last <= 0xdbff) cut--;
  return value.slice(0, cut);
}

/**
 * The PWA's `klukai:hud` snapshot, applied over the previous one, or null
 * when the message is not one (same checks as a speak request: this origin,
 * the parent window only). Each snapshot is complete: a field it leaves out
 * goes back to its default (so a stale status can't stick), while a field of
 * the wrong type is ignored and keeps its last good value. Numbers are
 * clamped (level 0-9, bpm 0-300), text is trimmed and capped. A snapshot
 * never makes a sound: only his tap on VOICE does.
 */
export function hudUpdate(event, { origin, parent }, prev = EMPTY_HUD) {
  const data = fromParent(event, { origin, parent }, HUD);
  if (!data) return null;
  const last = { ...EMPTY_HUD, ...prev };
  const next = { ...EMPTY_HUD };
  const take = (key, valid, value) => {
    if (data[key] === undefined) return;            // left out: default
    next[key] = valid ? value() : last[key];       // malformed: keep the last good value
  };
  const int = (v) => typeof v === "number" && Number.isFinite(v);
  take("level", int(data.level), () => Math.min(9, Math.max(0, Math.round(data.level))));
  take("bpm", int(data.bpm), () => Math.min(300, Math.max(0, Math.round(data.bpm))));
  take("mood", typeof data.mood === "string", () => capText(data.mood, MAX_LABEL_CHARS));
  take("wearing", typeof data.wearing === "string", () => capText(data.wearing, MAX_WEARING_CHARS));
  take("line", typeof data.line === "string", () => capText(data.line, MAX_LINE_CHARS));
  take("spiking", typeof data.spiking === "boolean", () => data.spiking);
  take("streaming", typeof data.streaming === "boolean", () => data.streaming);
  take("lineId", data.lineId === null || int(data.lineId)
    || (typeof data.lineId === "string" && data.lineId.length <= MAX_ID_CHARS), () => data.lineId === "" ? null : data.lineId);
  take("status", data.status === null || typeof data.status === "string",
    () => (data.status === null ? null : capText(data.status, MAX_STATUS_CHARS) || null));
  return next;
}

/**
 * The PWA showing (its 3D face) or hiding (its CHAT face) the stage, or null.
 * Same checks as a speak request: the same-origin parent only.
 */
export function visibilityRequest(event, { origin, parent }) {
  const data = fromParent(event, { origin, parent }, VISIBILITY);
  if (!data || typeof data.visible !== "boolean") return null;
  return { visible: data.visible };
}

/**
 * What may run right now. The stage draws and plays only while it is on
 * screen: the page visible and, embedded, the PWA showing its 3D face.
 * Off screen: no render loop, no ambience, no portrait blinks, audio
 * suspended (her voice is stopped by the caller when the PWA hides it).
 */
export function stageActivity({ documentHidden = false, embedHidden = false, mode = "model", awake = false } = {}) {
  const shown = !documentHidden && !embedHidden;
  return {
    shown,
    render: shown && mode === "model",
    portrait: shown && awake && mode === "portrait",
    ambience: shown,
    audio: shown,
  };
}

/** One playback per tap: a request repeating the last id is dropped. */
export function createSpeakGate() {
  let last = null;
  return {
    admit(request) {
      if (!request) return false;
      if (request.id !== null && request.id === last) return false;
      last = request.id;
      return true;
    },
  };
}

/**
 * Resume an AudioContext, giving up after `ms`: without a user gesture the
 * browser can leave resume() pending forever. True when it is running.
 */
export async function resumeWithin(ctx, ms = 1500, { setTimer = setTimeout, clearTimer = clearTimeout } = {}) {
  if (!ctx) return false;
  if (ctx.state === "running") return true;
  let timer;
  try {
    await Promise.race([
      Promise.resolve().then(() => ctx.resume()),
      new Promise((resolve) => { timer = setTimer(resolve, ms); }),
    ]);
  } catch { /* refused: report the state below */ }
  finally { clearTimer(timer); }
  return ctx.state === "running";
}

/** Tell the embedding page something; same origin only, never throws. */
export function postToParent(win, message) {
  try {
    if (win && win.parent && win.parent !== win) win.parent.postMessage(message, win.location.origin);
  } catch { /* detached or cross-origin: nothing to tell */ }
}
