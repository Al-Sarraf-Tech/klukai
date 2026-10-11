// The push-to-talk recorder bridge, run in a vm context with a fake browser:
// `node --test tools/companion/test`
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SRC_URL = new URL("../../../flutter_app/web/js/audio_recorder.js", import.meta.url);
const BUILD_URL = new URL("../../../web-build/js/audio_recorder.js", import.meta.url);

// Let a recorder's deferred onstop run.
const settle = () => new Promise((r) => setTimeout(r, 0));

class FakeMediaRecorder {
  static isTypeSupported(t) { return t === "audio/webm;codecs=opus"; }
  constructor(stream, opts = {}) {
    this.stream = stream; this.mimeType = opts.mimeType || ""; this.state = "inactive";
    this.ondataavailable = null; this.onstop = null;
    FakeMediaRecorder.instances.push(this);
  }
  start() { this.state = "recording"; }
  stop() {
    this.state = "inactive";
    // A real recorder flushes its last chunk and fires onstop a tick later.
    queueMicrotask(() => {
      if (this.ondataavailable && !this.silent) this.ondataavailable({ data: { size: 3 } });
      if (this.onstop) this.onstop();
    });
  }
}
class FakeBlob {
  constructor(parts, opts = {}) { this.parts = parts; this.type = opts.type; FakeBlob.last = this; }
}
class FakeFileReader {
  readAsDataURL(blob) {
    this.result = blob.parts.length ? "data:audio/webm;base64,QUJD" : "data:audio/webm;base64,";
    queueMicrotask(() => this.onloadend());
  }
}

// Loads the bridge fresh, with a mic grant the test releases by hand and
// timers it fires by hand.
function harness() {
  FakeMediaRecorder.instances = [];
  const grants = []; const timers = new Map(); const errors = []; let nextTimer = 1;
  const sandbox = {
    MediaRecorder: FakeMediaRecorder, Blob: FakeBlob, FileReader: FakeFileReader,
    navigator: { mediaDevices: { getUserMedia: () => new Promise((resolve, reject) => grants.push({ resolve, reject })) } },
    setTimeout: (fn, ms) => { const id = nextTimer++; timers.set(id, { fn, ms }); return id; },
    clearTimeout: (id) => { timers.delete(id); },
    console: { log() {}, error: (...a) => errors.push(a) },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(readFileSync(SRC_URL, "utf8"), sandbox, { filename: SRC_URL.pathname });
  // Answer the oldest pending getUserMedia with a fresh stream.
  const grant = () => {
    const tracks = [{ stopped: false, stop() { this.stopped = true; } }];
    const stream = { tracks, getTracks: () => tracks, released: () => tracks.every((t) => t.stopped) };
    grants.shift().resolve(stream);
    return stream;
  };
  const refuse = () => grants.shift().reject(new Error("NotAllowedError"));
  const fireTimers = () => { for (const [id, t] of timers) { timers.delete(id); t.fn(); } };
  return { rec: sandbox.window.audioRecorder, recorders: FakeMediaRecorder.instances, grant, refuse, timers, fireTimers, errors };
}

test("start then stop hands back the clip and releases the mic", async () => {
  const h = harness();
  const started = h.rec.start();
  const stream = h.grant();
  assert.equal(await started, true);
  assert.equal(h.rec.isRecording(), true);
  assert.equal(h.recorders[0].mimeType, "audio/webm;codecs=opus");
  assert.equal(await h.rec.stop(), "QUJD");
  assert.equal(FakeBlob.last.type, "audio/webm;codecs=opus");
  assert.equal(FakeBlob.last.parts.length, 1);
  assert.equal(stream.released(), true);
  assert.equal(h.rec.isRecording(), false);
  assert.equal(await h.rec.stop(), null, "nothing left to collect");
});

test("a take with no audio data resolves null, not an empty clip", async () => {
  const h = harness();
  const started = h.rec.start();
  const stream = h.grant();
  assert.equal(await started, true);
  h.recorders[0].silent = true;
  assert.equal(await h.rec.stop(), null);
  assert.equal(stream.released(), true);
});

test("a stop() racing a pending start() leaves no recorder running", async () => {
  const h = harness();
  const started = h.rec.start();
  const stopped = h.rec.stop();
  assert.equal(await stopped, null);
  const stream = h.grant();
  assert.equal(await started, false);
  assert.equal(stream.released(), true);
  assert.equal(h.rec.isRecording(), false);
  assert.equal(h.recorders.length, 0);
  assert.equal(h.timers.size, 0);
  // The next take is unaffected.
  const again = h.rec.start();
  h.grant();
  assert.equal(await again, true);
  assert.equal(await h.rec.stop(), "QUJD");
});

test("a newer start() supersedes one still waiting on the mic", async () => {
  const h = harness();
  const first = h.rec.start();
  const second = h.rec.start();
  const s1 = h.grant();
  const s2 = h.grant();
  assert.equal(await first, false);
  assert.equal(await second, true);
  assert.equal(s1.released(), true);
  assert.equal(s2.released(), false);
  assert.equal(h.recorders.length, 1);
  assert.equal(h.rec.isRecording(), true);
});

test("start while recording stops and releases the previous take", async () => {
  const h = harness();
  const first = h.rec.start();
  const s1 = h.grant();
  assert.equal(await first, true);
  const second = h.rec.start();
  const s2 = h.grant();
  assert.equal(await second, true);
  assert.equal(s1.released(), true);
  assert.equal(h.recorders[0].state, "inactive");
  assert.equal(h.recorders[1].state, "recording");
  assert.equal(h.rec.isRecording(), true);
  assert.equal(h.timers.size, 1, "only the new take's cap is armed");
  assert.equal(await h.rec.stop(), "QUJD");
  assert.equal(s2.released(), true);
});

test("the cap stops the take, keeps the clip for the button release, then forgets it", async () => {
  const h = harness();
  const started = h.rec.start();
  const stream = h.grant();
  assert.equal(await started, true);
  assert.equal([...h.timers.values()][0].ms, 60_000);
  h.fireTimers();
  assert.equal(h.recorders[0].state, "inactive");
  assert.equal(h.rec.isRecording(), false);
  await settle();
  assert.equal(stream.released(), true);
  assert.equal(await h.rec.stop(), "QUJD");
  assert.equal(await h.rec.stop(), null);
});

test("a normal stop clears the cap timer", async () => {
  const h = harness();
  const started = h.rec.start();
  h.grant();
  await started;
  assert.equal(h.timers.size, 1);
  await h.rec.stop();
  assert.equal(h.timers.size, 0);
  h.fireTimers();
  assert.equal(await h.rec.stop(), null);
});

test("a refused mic reports false and leaves nothing behind", async () => {
  const h = harness();
  const started = h.rec.start();
  h.refuse();
  assert.equal(await started, false);
  assert.equal(h.errors.length, 1);
  assert.equal(h.recorders.length, 0);
  assert.equal(h.timers.size, 0);
  assert.equal(h.rec.isRecording(), false);
  assert.equal(await h.rec.stop(), null);
});

test("the deployed PWA copy is byte-identical to the source", () => {
  assert.equal(readFileSync(BUILD_URL, "utf8"), readFileSync(SRC_URL, "utf8"));
});
