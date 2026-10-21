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
export function createApi({ token, fetchImpl = globalThis.fetch, base = "", onAuthExpired = () => {}, voicePollMs = 1000, voiceDeadlineMs = 610000 }) {
  let expired = false;
  // `cache`: API answers are never stored ("no-store"), except where the
  // server sends a private validator (the model: "default" lets the browser
  // keep it and revalidate with If-None-Match, still sending the bearer).
  async function call(path, { method = "GET", body, raw = false, signal, cache = "no-store" } = {}) {
    const headers = { Authorization: `Bearer ${token}` };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const res = await fetchImpl(`${base}${path}`, {
      method, headers, body: body === undefined ? undefined : JSON.stringify(body),
      cache, credentials: "omit", signal,
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

  async function tts(text, { signal } = {}) {
    let jobId;
    let completed = false;
    const callerSignal = signal;
    const controller = new AbortController();
    const cancel = () => controller.abort();
    callerSignal?.addEventListener("abort", cancel, { once: true });
    if (callerSignal?.aborted) controller.abort();
    signal = controller.signal;
    let timedOut = false;
    const deadline = setTimeout(() => { timedOut = true; controller.abort(); }, voiceDeadlineMs);
    try {
      if (signal.aborted) throw new DOMException("Aborted", "AbortError");
      let result = await call("/api/tts", { method: "POST", body: { text, language: "en" }, signal });
      // Dominus keeps its existing synchronous response contract.
      if (result.audio) return result;
      jobId = result.job_id;
      if (!jobId) throw new Error("Voice service returned no audio or job");
      while (true) {
        if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
        if (result.status === "completed" && result.audio) {
          completed = true;
          return result;
        }
        if (["failed", "cancelled"].includes(result.status)) throw new Error(result.error || "Voice synthesis cancelled");
        await new Promise((resolve, reject) => {
          const stop = () => {
            clearTimeout(timer);
            signal?.removeEventListener("abort", stop);
            reject(new DOMException("Aborted", "AbortError"));
          };
          const timer = setTimeout(() => {
            signal?.removeEventListener("abort", stop);
            resolve();
          }, voicePollMs);
          signal?.addEventListener("abort", stop, { once: true });
        });
        result = await call(`/api/tts/jobs/${encodeURIComponent(jobId)}`, { signal });
      }
    } catch (err) {
      if (timedOut) throw new Error("Voice synthesis timed out");
      throw err;
    } finally {
      clearTimeout(deadline);
      callerSignal?.removeEventListener("abort", cancel);
      if (jobId) {
        // The playback AbortSignal is already aborted on stop. Use a fresh,
        // bounded request so cancellation reaches the GPU supervisor.
        const cleanup = new AbortController();
        const timer = setTimeout(() => cleanup.abort(), 5000);
        const removal = call(`/api/tts/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE", signal: cleanup.signal })
          .catch(() => {}).finally(() => clearTimeout(timer));
        // Keep stop/cancel deterministic; successful playback needn't wait
        // for the optional early release of its completed audio result.
        if (!completed) await removal;
      }
    }
  }
  return {
    herDay: () => call("/api/her-day"),
    portrait: () => call("/api/portrait"),
    affection: () => call("/api/affection"),
    tts,
    models: () => call("/api/avatar/models"),
    // Her model: private, ETag'd, so a repeat load is a 304 from the browser's
    // own cache (never a shared one: the server says `private`).
    model: async (id = "default", { signal } = {}) => (await call(`/api/avatar/model${id === "default" ? "" : `?model=${encodeURIComponent(id)}`}`, { raw: true, signal, cache: "default" })).arrayBuffer(),
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
    // Every handler checks it still belongs to the current socket: a replaced
    // one (resume() dials while iOS has yet to deliver the old close event)
    // must not null out the new socket and dial a third over it, leaving the
    // second open, unreferenced and delivering every frame twice.
    const sock = new WebSocketImpl(url);
    ws = sock;
    sock.onopen = () => { if (ws !== sock) return; attempt = 0; onState("open"); };
    sock.onmessage = (ev) => {
      if (ws !== sock) return;
      let frame;
      try { frame = JSON.parse(ev.data); } catch { return; }
      onFrame(frame);
    };
    sock.onclose = (ev) => {
      if (ws !== sock) return;
      ws = null;
      if (closed) return;
      if (ev && ev.code === 4001) { closed = true; onState("auth"); onAuthFailed(); return; }
      onState("down");
      timer = setTimer(open, backoffMs(attempt++));
    };
    sock.onerror = () => { /* onclose follows */ };
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
