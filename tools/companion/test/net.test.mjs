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

test("tts follows a queued job until its audio is ready", async () => {
  const f = fakeFetch([
    { status: 202, body: { job_id: "voice-1", status: "queued" } },
    { status: 200, body: { job_id: "voice-1", status: "running" } },
    { status: 200, body: { job_id: "voice-1", status: "completed", audio: "wav" } },
    { status: 200, body: { job_id: "voice-1", status: "completed" } },
  ]);
  const api = createApi({ token: "t", fetchImpl: f.impl, voicePollMs: 1 });
  assert.equal((await api.tts("All of this sentence.")).audio, "wav");
  assert.deepEqual(f.calls.map((c) => c.url), ["/api/tts", "/api/tts/jobs/voice-1", "/api/tts/jobs/voice-1", "/api/tts/jobs/voice-1"]);
});

test("aborting queued speech deletes it with a fresh signal", async () => {
  const controller = new AbortController();
  const calls = [];
  let polled;
  const started = new Promise((resolve) => { polled = resolve; });
  const api = createApi({ token: "t", voicePollMs: 1, fetchImpl: async (url, init) => {
    calls.push({ url, init });
    if (init.method === "POST") return { status: 202, ok: true, json: async () => ({ job_id: "cancel-me", status: "queued" }) };
    if (init.method === "DELETE") return { status: 200, ok: true, json: async () => ({ status: "cancelled" }) };
    polled();
    return new Promise((_resolve, reject) => init.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true }));
  } });
  const pending = api.tts("Cancel this.", { signal: controller.signal });
  await started;
  controller.abort();
  await assert.rejects(pending, { name: "AbortError" });
  const deletion = calls.find((c) => c.init.method === "DELETE");
  assert.equal(deletion.url, "/api/tts/jobs/cancel-me");
  assert.notEqual(deletion.init.signal, controller.signal);
  assert.equal(deletion.init.signal?.aborted, false);
});

test("tts surfaces terminal job failure without expiring the session", async () => {
  const f = fakeFetch([
    { status: 202, body: { job_id: "failed", status: "queued" } },
    { status: 200, body: { job_id: "failed", status: "failed", error: "Voice service unavailable" } },
    { status: 200, body: { status: "failed" } },
  ]);
  const api = createApi({ token: "t", fetchImpl: f.impl, voicePollMs: 1 });
  await assert.rejects(api.tts("Hello."), /Voice service unavailable/);
});

test("a stalled voice poll is aborted by the overall deadline and its job is cancelled", async () => {
  let pollAborted = false;
  let deleted = false;
  const api = createApi({ token: "t", voicePollMs: 1, voiceDeadlineMs: 30, fetchImpl: async (_url, init) => {
    if (init.method === "POST") return { status: 202, ok: true, json: async () => ({ job_id: "stalled", status: "queued" }) };
    if (init.method === "DELETE") {
      deleted = true;
      return { status: 200, ok: true, json: async () => ({ status: "cancelled" }) };
    }
    return new Promise((_resolve, reject) => init.signal.addEventListener("abort", () => {
      pollAborted = true;
      reject(new DOMException("Aborted", "AbortError"));
    }, { once: true }));
  } });
  await assert.rejects(api.tts("Do not hang."), /timed out/);
  assert.equal(pollAborted, true);
  assert.equal(deleted, true);
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

test('appearance catalog and selected model remain authenticated, with cancellation', async () => {
  const buf = new ArrayBuffer(4), controller = new AbortController();
  const f = fakeFetch([{status:200, body:{models:[]}}, {status:200,buf}]);
  const api=createApi({token:'test',fetchImpl:f.impl});
  assert.deepEqual(await api.models(),{models:[]});
  assert.equal(await api.model('hk416_gfl64',{signal:controller.signal}),buf);
  assert.equal(f.calls[0].url,'/api/avatar/models');
  assert.equal(f.calls[1].url,'/api/avatar/model?model=hk416_gfl64');
  assert.equal(f.calls[1].init.headers.Authorization,'Bearer test');
  assert.equal(f.calls[1].init.signal,controller.signal);
});

test("TTS forwards cancellation without changing authentication or language", async () => {
  const controller = new AbortController();
  const f = fakeFetch([{ status: 200, body: { audio: "x" } }]);
  const api = createApi({ token: "test", fetchImpl: f.impl });
  await api.tts("Hello.", { signal: controller.signal });
  assert.equal(f.calls[0].init.signal.aborted, false);
  assert.equal(f.calls[0].init.headers.Authorization, "Bearer test");
  assert.deepEqual(JSON.parse(f.calls[0].init.body), { text: "Hello.", language: "en" });
});
