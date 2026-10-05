import { test } from "node:test";
import assert from "node:assert/strict";
import {
  readToken, clearToken, createApi, AuthExpired, wsUrl, backoffMs, createSocket,
} from "../../../flutter_app/web/companion/net.js";

const store = (v) => {
  const m = new Map(v ? [["klukai_token", v]] : []);
  return { getItem: (k) => m.get(k) ?? null, removeItem: (k) => m.delete(k), m };
};

test("token storage is read safely", () => {
  assert.equal(readToken(store("abc")), "abc");
  assert.equal(readToken(store()), null);
  assert.equal(readToken({ getItem() { throw new Error("blocked"); } }), null);
  assert.equal(readToken(null), null);
  const s = store("abc"); clearToken(s); assert.equal(readToken(s), null);
  clearToken({ removeItem() { throw new Error("blocked"); } }); // never throws
});

function fakeFetch(responses) {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url, init });
    const r = responses.shift();
    return {
      status: r.status, ok: r.status >= 200 && r.status < 300,
      json: async () => r.body, arrayBuffer: async () => r.buf,
    };
  };
  return { impl, calls };
}

test("api calls carry the bearer token and decode JSON", async () => {
  const f = fakeFetch([{ status: 200, body: { date: "2026-10-05" } }, { status: 200, body: { audio: "x" } }]);
  const api = createApi({ token: "t0k", fetchImpl: f.impl, base: "https://h" });
  assert.deepEqual(await api.herDay(), { date: "2026-10-05" });
  assert.equal(f.calls[0].url, "https://h/api/her-day");
  assert.equal(f.calls[0].init.headers.Authorization, "Bearer t0k");
  assert.equal(f.calls[0].init.body, undefined);
  await api.tts("Report.");
  assert.equal(f.calls[1].init.method, "POST");
  assert.equal(f.calls[1].init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(f.calls[1].init.body), { text: "Report.", language: "en" });
});

test("401 fires onAuthExpired once; other errors carry the status", async () => {
  let n = 0;
  const f = fakeFetch([{ status: 401 }, { status: 401 }, { status: 503 }]);
  const api = createApi({ token: "t", fetchImpl: f.impl, onAuthExpired: () => n++ });
  await assert.rejects(api.portrait(), AuthExpired);
  await assert.rejects(api.affection(), AuthExpired);
  assert.equal(n, 1);
  await assert.rejects(api.herDay(), (e) => e.status === 503);
});

test("the model arrives as an ArrayBuffer", async () => {
  const buf = new ArrayBuffer(4);
  const f = fakeFetch([{ status: 200, buf }]);
  assert.equal(await createApi({ token: "t", fetchImpl: f.impl }).model(), buf);
  assert.equal(f.calls[0].url, "/api/avatar/model");
});

test("ws url follows the page scheme", () => {
  assert.equal(wsUrl({ protocol: "https:", host: "klukai.appnest.cc" }, "a b"), "wss://klukai.appnest.cc/ws?token=a%20b");
  assert.equal(wsUrl({ protocol: "http:", host: "localhost:8300" }, "t"), "ws://localhost:8300/ws?token=t");
});

test("backoff doubles to a 20 s cap with jitter", () => {
  const mid = () => 0.5;
  assert.equal(backoffMs(0, mid), 1000);
  assert.equal(backoffMs(3, mid), 8000);
  assert.equal(backoffMs(10, mid), 20000);
  assert.equal(backoffMs(-1, mid), 1000);
  assert.equal(backoffMs(0, () => 0), 800);
  assert.equal(backoffMs(0, () => 1), 1200);
});

function fakeWs() {
  const sockets = [];
  class WS {
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); }
    send(d) { this.sent.push(d); }
    close() { this.readyState = 3; }
  }
  return { WS, sockets };
}

test("socket parses frames, reconnects with backoff, resumes, and stops on auth", () => {
  const { WS, sockets } = fakeWs();
  const frames = []; const states = []; const timers = []; let authFailed = 0;
  const sock = createSocket({
    url: "ws://x/ws", WebSocketImpl: WS,
    onFrame: (f) => frames.push(f), onState: (s) => states.push(s), onAuthFailed: () => authFailed++,
    setTimer: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearTimer: () => {},
  });
  sock.start();
  const s0 = sockets[0];
  s0.readyState = 1; s0.onopen();
  s0.onmessage({ data: '{"type":"mood","mood":"tender"}' });
  s0.onmessage({ data: "not json" });
  s0.onerror();
  assert.deepEqual(frames, [{ type: "mood", mood: "tender" }]);
  assert.equal(sock.send({ type: "message", content: "hi" }), true);
  assert.deepEqual(JSON.parse(s0.sent[0]), { type: "message", content: "hi" });

  // Network drop → scheduled reconnect.
  s0.onclose({ code: 1006 });
  assert.equal(sock.send({ type: "x" }), false);
  assert.equal(timers.length, 1);
  timers[0].fn();
  assert.equal(sockets.length, 2);

  // iOS resume while the new socket is still alive: no duplicate.
  sockets[1].readyState = 1;
  sock.resume();
  assert.equal(sockets.length, 2);
  // Resume after it died: reconnect immediately.
  sockets[1].readyState = 3;
  sock.resume();
  assert.equal(sockets.length, 3);

  // Auth close: stop for good.
  sockets[2].onclose({ code: 4001 });
  assert.equal(authFailed, 1);
  sock.resume();
  assert.equal(sockets.length, 3);
  assert.ok(states.includes("auth") && states.includes("down") && states.includes("open"));
});

test("close stops reconnects", () => {
  const { WS, sockets } = fakeWs();
  const timers = [];
  const sock = createSocket({ url: "u", WebSocketImpl: WS, onFrame() {}, setTimer: (f) => timers.push(f), clearTimer() {} });
  sock.start();
  sock.close();
  assert.equal(sockets[0].readyState, 3);
  sockets[0].onclose({ code: 1000 });
  assert.equal(timers.length, 0);
  sock.close(); // idempotent with no socket
});
