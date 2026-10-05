// Network layer for the companion stage: authenticated API calls and a
// self-healing WebSocket. Dependencies are injectable so node --test can run
// these without a browser.

const TOKEN_KEY = "klukai_token"; // set by web-build/login.html, shared with the PWA

export function readToken(storage = globalThis.localStorage) {
  try {
    return storage ? storage.getItem(TOKEN_KEY) : null;
  } catch {
    return null; // Safari private mode / blocked storage
  }
}

export function clearToken(storage = globalThis.localStorage) {
  try { storage && storage.removeItem(TOKEN_KEY); } catch { /* ignore */ }
}

export class AuthExpired extends Error {}

/** Thin API client. `onAuthExpired` runs once on the first 401. */
export function createApi({ token, fetchImpl = globalThis.fetch, base = "", onAuthExpired = () => {} }) {
  let expired = false;
  async function call(path, { method = "GET", body, raw = false } = {}) {
    const headers = { Authorization: `Bearer ${token}` };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const res = await fetchImpl(`${base}${path}`, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store", credentials: "omit",
    });
    if (res.status === 401) {
      if (!expired) { expired = true; onAuthExpired(); }
      throw new AuthExpired(path);
    }
    if (!res.ok) {
      const err = new Error(`${method} ${path} → ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return raw ? res : res.json();
  }
  return {
    herDay: () => call("/api/her-day"),
    portrait: () => call("/api/portrait"),
    affection: () => call("/api/affection"),
    tts: (text) => call("/api/tts", { method: "POST", body: { text, language: "en" } }),
    model: async () => (await call("/api/avatar/model", { raw: true })).arrayBuffer(),
  };
}

/** WebSocket URL for the current page origin (wss behind Cloudflare). */
export function wsUrl(location, token) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws?token=${encodeURIComponent(token)}`;
}

/** Reconnect delay: 1s, 2s, 4s … capped at 20s, with ±20% jitter. */
export function backoffMs(attempt, rand = Math.random) {
  const base = Math.min(20000, 1000 * Math.pow(2, Math.max(0, attempt)));
  return Math.round(base * (0.8 + rand() * 0.4));
}

/**
 * A WebSocket that reconnects with backoff, reconnects immediately when the
 * page comes back (iOS suspends sockets in the background), and stops for
 * good on an auth close (4001).
 */
export function createSocket({
  url, onFrame, onState = () => {}, onAuthFailed = () => {},
  WebSocketImpl = globalThis.WebSocket, setTimer = setTimeout, clearTimer = clearTimeout,
}) {
  let ws = null;
  let attempt = 0;
  let timer = null;
  let closed = false;

  function open() {
    if (closed) return;
    clearTimer(timer);
    timer = null;
    onState("connecting");
    ws = new WebSocketImpl(url);
    ws.onopen = () => { attempt = 0; onState("open"); };
    ws.onmessage = (ev) => {
      let frame;
      try { frame = JSON.parse(ev.data); } catch { return; }
      onFrame(frame);
    };
    ws.onclose = (ev) => {
      ws = null;
      if (closed) return;
      if (ev && ev.code === 4001) { closed = true; onState("auth"); onAuthFailed(); return; }
      onState("down");
      timer = setTimer(open, backoffMs(attempt++));
    };
    ws.onerror = () => { /* onclose follows */ };
  }

  return {
    start: open,
    /** Page visible again: if the socket died while suspended, reconnect now. */
    resume() {
      if (closed) return;
      if (!ws || ws.readyState >= 2) { attempt = 0; open(); }
    },
    send(obj) {
      if (ws && ws.readyState === 1) { ws.send(JSON.stringify(obj)); return true; }
      return false;
    },
    close() {
      closed = true;
      clearTimer(timer);
      if (ws) ws.close();
    },
  };
}
