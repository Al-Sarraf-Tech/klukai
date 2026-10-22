// Execute the production event wiring without a DOM, renderer, or network.
// Only browser/visual boundaries are faked; speech parsing, resume deadlines,
// cancellation, and the voice queue use their real implementations.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import vm from "node:vm";
import * as embed from "../../../flutter_app/web/companion/embed.js";
import * as logic from "../../../flutter_app/web/companion/logic.js";
import * as life from "../../../flutter_app/web/companion/stage_life.js";
import { hudView, nextVoicePhase } from "../../../flutter_app/web/companion/hud.js";
import { createVoice } from "../../../flutter_app/web/companion/voice.js";
import { createSocket } from "../../../flutter_app/web/companion/net.js";
import { FakeAudioContext } from "./fake-audio.mjs";

const source = readFileSync(new URL("../../../flutter_app/web/companion/app.js", import.meta.url), "utf8");
const turn = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
};

test("the companion page requests the current app entry point", () => {
  const page = readFileSync(new URL("../../../flutter_app/web/companion.html", import.meta.url), "utf8");
  const version = createHash("sha256").update(source).digest("hex").slice(0, 12);
  assert.ok(page.includes(`companion/app.js?v=${version}`), "the app content hash must change with its event wiring");
});

function eventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(fn);
    },
    dispatch(type, event = {}) {
      return Promise.all((listeners.get(type) || []).map((fn) => fn(event)));
    },
  };
}

function harness({ ttsImpl, embedded = true, socketFactory } = {}) {
  const elements = new Map();
  const element = (id) => {
    const classes = new Set();
    if (!elements.has(id)) elements.set(id, {
      ...eventTarget(), textContent: "", hidden: false, dataset: {},
      classList: {
        add: (name) => classes.add(name), remove: (name) => classes.delete(name),
        contains: (name) => classes.has(name),
        toggle(name, on = !classes.has(name)) { if (on) classes.add(name); else classes.delete(name); },
      },
      style: { setProperty() {} }, setAttribute() {}, removeAttribute() {},
      querySelectorAll: () => [], getBoundingClientRect: () => ({ height: 100 }),
      focus() {},
    });
    return elements.get(id);
  };
  const document = {
    ...eventTarget(), hidden: false, documentElement: element("html"),
    getElementById: element, querySelector: element, querySelectorAll: () => [],
  };
  const resumes = []; const requests = []; const messages = []; const views = []; const timers = new Map();
  let nextTimer = 0;
  const timer = (fn, ms) => { const id = ++nextTimer; timers.set(id, { fn, ms }); return id; };
  const clearTimer = (id) => timers.delete(id);
  class AudioContext extends FakeAudioContext {
    resume() {
      const wait = deferred();
      const finish = () => { this.state = "running"; wait.resolve(); };
      finish.reject = () => wait.reject(new Error("Audio resume refused"));
      resumes.push(finish);
      return wait.promise;
    }
  }
  const parent = { postMessage: (message) => messages.push(message) };
  const window = {
    ...eventTarget(), AudioContext, parent,
    location: { search: embedded ? "?embed=1" : "", origin: "https://klukai.example" },
  };
  let hud, onFrame;
  const sandbox = {
    ...embed, ...logic, ...life, hudView, nextVoicePhase,
    window, document, location: window.location,
    localStorage: { getItem: () => '{"music":false}' },
    readToken: () => "test-token", clearToken() {}, wsUrl: () => "unused",
    AuthExpired: class extends Error {},
    createApi: () => ({
      herDay: async () => ({}), portrait: async () => ({ frames: {} }),
      tts: (text, options) => {
        requests.push(text);
        return ttsImpl ? ttsImpl(text, options) : Promise.resolve({ audio: btoa(text) });
      },
    }),
    createSocket: (options) => {
      onFrame = options.onFrame;
      return socketFactory ? socketFactory(options) : { start() {}, resume() {}, send: () => true };
    },
    createPortrait: () => ({ setFrames() {}, setExpression() {}, setActive() {}, setLevel() {} }),
    createAmbience: () => ({ start() {}, stop() {}, setKind() {} }), ambienceKind: () => "none",
    createMusic: () => ({ setHidden() {}, duck() {}, setScene() {} }),
    createHud: (_doc, callbacks) => { hud = callbacks; return { hold() {}, update: (view) => views.push(view) }; },
    createVoice: (options) => createVoice({ ...options, setTick: timer, clearTick: clearTimer }),
    createStageAppearance: () => ({ refreshAppearances() {}, setMode() {}, warmModel() {}, forgetModels() {} }),
    // Resume uses the genuine helper, with this harness's inert timers.
    resumeWithin: (ctx, ms) => embed.resumeWithin(ctx, ms, { setTimer: timer, clearTimer }),
    setTimeout: timer, clearTimeout: clearTimer, setInterval: timer,
    console, URLSearchParams,
  };
  // Resolve module dependencies above, then run the complete boot/event code.
  vm.runInNewContext(source.replace(/^import[\s\S]*?from\s+"[^"]+";\n/gm, ""), sandbox);
  const message = (data) => window.dispatch("message", { data, source: parent, origin: window.location.origin });
  return {
    requests, resumes, messages, document, views,
    element, frame: (data) => onFrame(data), click: (id) => element(id).dispatch("click"),
    showPage: () => window.dispatch("pageshow"),
    expireResume: () => [...timers.values()].find((entry) => entry.ms === 3000).fn(),
    speak: (text, id) => message({ type: embed.SPEAK, text, id }),
    visible: (visible) => message({ type: embed.VISIBILITY, visible }),
    hud: (line) => message({ type: embed.HUD, line, streaming: false }),
    tapVoice: () => hud.onVoice(),
    get sources() { return FakeAudioContext.last?.of("source") || []; },
  };
}

// The actual app creates/starts the actual transport. Only WebSocket and timers
// are inert: pageshow drives the production resume handler, not a copied one.
function socketHarness(t) {
  const sockets = [], states = [], reconnects = [];
  class FakeSocket {
    constructor() { this.readyState = 0; this.sent = []; sockets.push(this); }
    close() { this.readyState = 3; }
    send(data) { this.sent.push(JSON.parse(data)); }
    open() { this.readyState = 1; this.onopen(); }
    frame(data) { this.onmessage({ data: JSON.stringify(data) }); }
  }
  const h = harness({ embedded: false, socketFactory: (options) => {
    const socket = createSocket({
      ...options, WebSocketImpl: FakeSocket,
      onState: (state) => { states.push(state); options.onState(state); },
      setTimer: (fn) => { reconnects.push(fn); return reconnects.length; }, clearTimer() {},
    });
    t.after(() => socket.close());
    return socket;
  } });
  return { ...h, sockets, states, reconnects };
}

for (const readyState of [2, 3]) {
  test(`replacement of a ${readyState === 2 ? "closing" : "closed"} socket abandons its prefix before new tokens`, async (t) => {
    const h = socketHarness(t), old = h.sockets[0];
    old.open();
    old.frame({ type: "token", text: "An interrupted old prefix. " });
    old.readyState = readyState; // close is queued behind the resume event
    await h.showPage();
    const live = h.sockets[1];
    assert.equal(h.element("subtitle").textContent, "An interrupted old prefix. ", "the readable partial is retained while dialing");
    assert.equal(h.element("notice").textContent, "", "connecting alone must not report link-down");
    live.open();
    old.onclose({ code: 1006 });
    old.frame({ type: "token", text: "Stale token." });
    live.frame({ type: "token", text: "A brand new reply." });
    assert.equal(h.element("subtitle").textContent, "A brand new reply.");
    old.frame({ type: "done", text: "Stale final." });
    assert.equal(h.element("subtitle").textContent, "A brand new reply.");
    live.frame({ type: "done", text: "A complete canonical reply." });
    assert.equal(h.element("subtitle").textContent, "A complete canonical reply.");
    assert.equal(h.element("btn-voice").disabled, false);
    assert.deepEqual(h.states, ["connecting", "open", "connecting", "open"]);
    assert.equal(h.reconnects.length, 0);
    assert.equal(h.sockets.length, 2);
    assert.equal(h.element("notice").hidden, true);
    assert.deepEqual(h.requests, [], "completion must not automatically speak");
  });
}

test("replacement dialing releases thinking without waiting for old close or new open", async (t) => {
  const h = socketHarness(t), old = h.sockets[0];
  old.open();
  old.frame({ type: "thinking", text: "Working…" });
  assert.equal(h.element("subtitle").classList.contains("thinking"), true);
  old.readyState = 3;
  await h.showPage();
  assert.equal(h.element("subtitle").classList.contains("thinking"), false);
  assert.equal(h.element("notice").textContent, "");
  assert.deepEqual(h.states, ["connecting", "open", "connecting"]);
});

test("initial and repeated resume leave a healthy current stream intact", async (t) => {
  const h = socketHarness(t), live = h.sockets[0];
  assert.deepEqual(h.states, ["connecting"]);
  assert.equal(h.element("subtitle").textContent, "");
  await h.showPage();
  await h.showPage();
  assert.equal(h.sockets.length, 1, "resume does not redial a connecting socket");
  live.open();
  live.frame({ type: "token", text: "Current " });
  await h.showPage();
  live.frame({ type: "token", text: "reply." });
  assert.equal(h.element("subtitle").textContent, "Current reply.");
  live.frame({ type: "done", text: "Current reply." });
  await h.showPage();
  assert.equal(h.element("subtitle").textContent, "Current reply.");
  assert.equal(h.sockets.length, 1);
  assert.deepEqual(h.states, ["connecting", "open"]);
});

test("a further connecting replacement is harmless and still accepts done-only success", async (t) => {
  const h = socketHarness(t), old = h.sockets[0];
  old.open();
  old.frame({ type: "token", text: "Interrupted." });
  old.readyState = 3;
  await h.showPage();
  const failedDial = h.sockets[1];
  failedDial.readyState = 3;
  await h.showPage();
  const live = h.sockets[2];
  live.open();
  live.frame({ type: "done", text: "The whole reply, without any tokens." });
  old.onclose({ code: 1006 });
  failedDial.onclose({ code: 1006 });
  old.frame({ type: "done", text: "Stale final." });
  assert.equal(h.element("subtitle").textContent, "The whole reply, without any tokens.");
  assert.equal(h.element("btn-voice").disabled, false);
  assert.equal(h.element("notice").hidden, true);
  assert.deepEqual(h.states, ["connecting", "open", "connecting", "connecting", "open"]);
  assert.equal(h.reconnects.length, 0);
  assert.deepEqual(h.requests, []);
});

test("done-only and missing-prefix success preserve canonical text on the actual socket", (t) => {
  const h = socketHarness(t), live = h.sockets[0];
  live.open();
  live.frame({ type: "done", text: "Done-only canonical reply." });
  assert.equal(h.element("subtitle").textContent, "Done-only canonical reply.");
  live.frame({ type: "token", text: "suffix only." });
  live.frame({ type: "done", text: "The full reply, not suffix only." });
  assert.equal(h.element("subtitle").textContent, "The full reply, not suffix only.");
  assert.equal(h.element("btn-voice").disabled, false);
  assert.deepEqual(h.requests, []);
});

test("a delivered down event still abandons the stream and shows the reconnect notice", (t) => {
  const h = socketHarness(t), old = h.sockets[0];
  old.open();
  old.frame({ type: "token", text: "Interrupted." });
  old.frame({ type: "thinking", text: "Working…" });
  old.readyState = 3;
  old.onclose({ code: 1006 });
  assert.equal(h.element("subtitle").classList.contains("thinking"), false);
  assert.equal(h.element("notice").hidden, false);
  assert.equal(h.element("notice").textContent, "Link down — reconnecting…");
  assert.equal(h.reconnects.length, 1);
  h.reconnects[0]();
  const live = h.sockets[1];
  live.open();
  live.frame({ type: "token", text: "New reply." });
  assert.equal(h.element("subtitle").textContent, "New reply.");
  assert.equal(h.element("notice").hidden, true);
});

test("a newer parent speech request wins when audio resumes out of order", async () => {
  const h = harness();
  const old = h.speak("Old line.", 1);
  await turn();
  const fresh = h.speak("New line.", 2);
  await turn();
  h.resumes[1]();
  await fresh;
  await turn();
  assert.deepEqual(h.requests, ["New line."]);
  const playing = h.sources.at(-1);
  h.resumes[0]();
  await old;
  await turn();
  assert.deepEqual(h.requests, ["New line."], "late resume must not synthesize the superseded line");
  assert.notEqual(playing.stopped, true, "the newer speech must not be stopped by stale completion");
  await h.visible(false);
});

test("hide then show cancels parent speech that is still resuming audio", async () => {
  const h = harness();
  const pending = h.speak("Cancelled line.", 1);
  await turn();
  await h.visible(false);
  await h.visible(true);
  h.resumes[0]();
  await pending;
  await turn();
  assert.deepEqual(h.requests, [], "showing the stage must never resurrect cancelled speech");
});

test("backgrounding the document stops active voice instead of resuming it later", async () => {
  const h = harness();
  const pending = h.speak("Stop when hidden.", 1);
  await turn();
  h.resumes[0]();
  await pending;
  await turn();
  const playing = h.sources.at(-1);
  assert.ok(playing, "speech started");
  h.document.hidden = true;
  await h.document.dispatch("visibilitychange");
  assert.equal(playing.stopped, true, "off-screen speech must be cancelled, not left running or suspended");
  h.document.hidden = false;
  await h.document.dispatch("visibilitychange");
  assert.deepEqual(h.requests, ["Stop when hidden."]);
});

test("a superseded resume failure cannot trigger the PWA fallback for the old line", async () => {
  const h = harness();
  const old = h.speak("Old line.", 1);
  await turn();
  const fresh = h.speak("New line.", 2);
  await turn();
  h.resumes[0].reject();
  await old;
  assert.equal(h.messages.some((m) => m.type === "klukai:speak-status" && m.id === 1), false,
    "the PWA plays unavailable lines itself, so stale failures must stay silent");
  h.resumes[1]();
  await fresh;
  await turn();
  assert.deepEqual(h.requests, ["New line."]);
  await h.visible(false);
});

test("a superseded resume deadline cannot request fallback while the newer start is pending", async () => {
  const h = harness();
  const old = h.speak("Old deadline.", 1);
  await turn();
  const fresh = h.speak("New request.", 2);
  await turn();
  h.expireResume();
  await old;
  assert.equal(h.messages.some((m) => m.type === "klukai:speak-status" && m.id === 1), false);
  h.resumes[1]();
  await fresh;
  await turn();
  assert.deepEqual(h.requests, ["New request."]);
  await h.visible(false);
});

test("document hide then show invalidates a speech request that is resuming", async () => {
  const h = harness();
  const pending = h.speak("Do not resume me.", 1);
  await turn();
  h.document.hidden = true;
  await h.document.dispatch("visibilitychange");
  h.document.hidden = false;
  await h.document.dispatch("visibilitychange");
  h.resumes[0]();
  await pending;
  await turn();
  assert.deepEqual(h.requests, []);
});

test("parent requests arriving in a background document do not start audio", async () => {
  const h = harness();
  h.document.hidden = true;
  await h.document.dispatch("visibilitychange");
  const pending = h.speak("Not while hidden.", 1);
  await turn();
  assert.equal(h.resumes.length, 0, "hidden requests should be rejected before resuming audio");
  await pending;
  assert.deepEqual(h.requests, []);
  assert.ok(h.messages.some((m) => m.type === "klukai:speak-status" && m.id === 1 && m.status === "hidden"));
});

test("a current audio failure still reports unavailable for the PWA fallback", async () => {
  const h = harness();
  const pending = h.speak("Fallback line.", 1);
  await turn();
  h.resumes[0].reject();
  await pending;
  assert.deepEqual(h.requests, []);
  assert.ok(h.messages.some((m) => m.type === "klukai:speak-status" && m.id === 1 && m.status === "unavailable"));
});

test("a repeated parent request id does not schedule a second voice start", async () => {
  const h = harness();
  const first = h.speak("Once.", 1);
  await turn();
  await h.speak("Once.", 1);
  assert.equal(h.resumes.length, 1);
  h.resumes[0]();
  await first;
  await turn();
  assert.deepEqual(h.requests, ["Once."]);
  await h.visible(false);
});

test("a HUD stop leaves the button idle after the real voice engine clears busy", async () => {
  const h = harness();
  await h.hud("Hear me once.");
  const pending = h.tapVoice();
  await turn();
  // The tap unlocks a silent sample first, then waits for startAudio's resume.
  h.resumes[1]();
  await pending;
  await turn();
  assert.equal(h.views.at(-1).voice, "playing");
  const playing = h.sources.at(-1);
  await h.tapVoice();
  assert.equal(playing.stopped, true);
  assert.equal(h.views.at(-1).voice, "idle", "stop must not toggle done back to loading");
  await h.tapVoice();
  await turn();
  assert.deepEqual(h.requests, ["Hear me once.", "Hear me once."], "a fresh tap replays without an extra reset tap");
  assert.equal(h.views.at(-1).voice, "playing");
  await h.tapVoice();
  assert.equal(h.views.at(-1).voice, "idle", "the second playback can also be stopped");
});

test("HUD cancellation during audio resume remains idle when both pending starts finish", async () => {
  const h = harness();
  await h.hud("Only on a fresh tap.");
  const first = h.tapVoice();
  await turn();
  await h.tapVoice();
  assert.equal(h.views.at(-1).voice, "idle");
  const second = h.tapVoice();
  await turn();
  await h.tapVoice();
  assert.equal(h.views.at(-1).voice, "idle");
  h.resumes.forEach((resume) => resume());
  await Promise.all([first, second]);
  await turn();
  assert.deepEqual(h.requests, [], "cancelled resumes must not synthesize");
  await h.tapVoice();
  await turn();
  assert.deepEqual(h.requests, ["Only on a fresh tap."]);
  await h.visible(false);
});

test("HUD cancellation during synthesis aborts the request and permits fresh playback", async () => {
  const old = deferred();
  let signal, count = 0;
  const h = harness({ ttsImpl: (_text, options) => {
    if (++count === 1) { signal = options.signal; return old.promise; }
    return Promise.resolve({ audio: btoa("fresh") });
  } });
  await h.hud("Still preparing.");
  const pending = h.tapVoice();
  await turn();
  h.resumes[1]();
  await pending;
  await turn();
  assert.equal(h.views.at(-1).voice, "loading");
  await h.tapVoice();
  assert.equal(signal.aborted, true);
  assert.equal(h.views.at(-1).voice, "idle");
  await h.tapVoice();
  await turn();
  assert.equal(h.views.at(-1).voice, "playing");
  const before = h.sources.length;
  old.resolve({ audio: btoa("stale") });
  await turn();
  assert.equal(h.sources.length, before, "late cancelled synthesis cannot restart playback");
  assert.equal(h.views.at(-1).voice, "playing");
  await h.tapVoice();
  assert.equal(h.views.at(-1).voice, "idle");
});

test("standalone first-play resume cannot survive a document hide/show", async () => {
  const h = harness({ embedded: false });
  h.frame({ type: "done", text: "Cancelled standalone." });
  const pending = h.click("btn-voice");
  await turn();
  h.document.hidden = true;
  await h.document.dispatch("visibilitychange");
  h.document.hidden = false;
  await h.document.dispatch("visibilitychange");
  h.resumes[0]();
  await pending;
  await turn();
  assert.deepEqual(h.requests, [], "a hidden first tap must not start later");
  await h.click("btn-voice");
  await turn();
  assert.deepEqual(h.requests, ["Cancelled standalone."], "a new tap can replay after cancellation");
  h.document.hidden = true;
  await h.document.dispatch("visibilitychange");
});

test("standalone speech plays to completion and can be requested again", async () => {
  const h = harness({ embedded: false });
  h.frame({ type: "done", text: "A completed line." });
  const pending = h.click("btn-voice");
  await turn();
  h.resumes[0]();
  await pending;
  await turn();
  const first = h.sources.at(-1);
  assert.ok(first);
  first.onended();
  await turn();
  await h.click("btn-voice");
  await turn();
  assert.deepEqual(h.requests, ["A completed line.", "A completed line."]);
  assert.equal(h.sources.length, 2);
  h.document.hidden = true;
  await h.document.dispatch("visibilitychange");
});

test("a cancelled standalone resume failure does not display a stale audio error", async () => {
  const h = harness({ embedded: false });
  h.frame({ type: "done", text: "Cancelled failure." });
  const pending = h.click("btn-voice");
  await turn();
  h.document.hidden = true;
  await h.document.dispatch("visibilitychange");
  h.resumes[0].reject();
  await pending;
  assert.deepEqual(h.requests, []);
  assert.notEqual(h.element("notice").textContent, life.beginWake().audioMissing);
});
