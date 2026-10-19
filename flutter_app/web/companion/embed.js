// Embedded stage: the PWA's Chat tab shows this page in a same-origin iframe,
// companion.html?embed=1&mode=3d, under its own HUD. Embedded, the stage
// hides its chrome, always shows 3D, keeps its socket (the avatar reacts to
// the chat) and speaks only when the PWA asks, once per tap.
// Pure helpers, so node --test can check the contract without a browser.

export const SPEAK = "klukai:speak";
export const MAX_SPEAK_CHARS = 4000;
const INSET_MAX = 600;

/** Embed flags from location.search; the 3D view is forced when embedded. */
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
    insets: { top: inset("inset_top", 120), bottom: inset("inset_bottom", 210) },
  };
}

/**
 * A speak request from the embedding page, or null. Only the same-origin
 * parent window may ask; other origins, other frames and malformed data are
 * ignored. The text is trimmed and capped.
 */
export function speakRequest(event, { origin, parent }) {
  if (!event || typeof origin !== "string" || event.origin !== origin) return null;
  if (!parent || event.source !== parent) return null;
  const data = event.data;
  if (!data || typeof data !== "object" || data.type !== SPEAK) return null;
  if (typeof data.text !== "string") return null;
  const text = data.text.trim().slice(0, MAX_SPEAK_CHARS);
  if (!text) return null;
  const id = typeof data.id === "number" && Number.isFinite(data.id) ? data.id
    : typeof data.id === "string" && data.id ? data.id : null;
  return { text, id };
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
