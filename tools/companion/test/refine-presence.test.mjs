// Voice and stage defects that were still live after the 2026-10-11 backlog.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { createMusic } from "../../../flutter_app/web/companion/music.js";
import { createVoice } from "../../../flutter_app/web/companion/voice.js";
import { portraitRetryMs, PORTRAIT_UNAVAILABLE_RETRY_MS } from "../../../flutter_app/web/companion/stage_life.js";
import { FakeAudioContext } from "./fake-audio.mjs";

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

test("an unavailable portrait is polled again; a settled one is not", () => {
  assert.equal(portraitRetryMs("unavailable"), PORTRAIT_UNAVAILABLE_RETRY_MS);
  assert.equal(portraitRetryMs("pending", { pollMs: 15000 }), 15000);
  assert.equal(portraitRetryMs("partial", { pollMs: 15000 }), 15000);
  assert.equal(portraitRetryMs("ready"), null);
  assert.equal(portraitRetryMs(undefined), null);
});

test("a shared context resumes after hide even when the score never started", async () => {
  const ctx = new FakeAudioContext();
  const music = createMusic({ context: ctx });
  await music.setHidden(true);
  assert.equal(ctx.state, "suspended");
  await music.setHidden(false);
  assert.equal(ctx.state, "running");
  assert.equal(music.playing, false);
});

test("a late suspend cannot leave the shared context silent", async () => {
  const ctx = new FakeAudioContext();
  let releaseSuspend = () => {};
  ctx.suspend = () => new Promise((resolve) => {
    releaseSuspend = () => {
      ctx.state = "suspended";
      resolve();
    };
  });
  const music = createMusic({ context: ctx });
  const hiding = music.setHidden(true);
  const showing = music.setHidden(false);
  await Promise.resolve();
  assert.equal(ctx.state, "suspended", "show waits until the in-flight suspend finishes");
  releaseSuspend();
  await hiding;
  await showing;
  assert.equal(ctx.state, "running");
});

test("a suspended context does not jam the stage voice queue", async () => {
  const ctx = new FakeAudioContext();
  ctx.resume = async () => { ctx.state = "suspended"; };
  const errors = [];
  const voice = createVoice({
    context: ctx,
    tts: async () => ({ audio: btoa("wav") }),
    onError: (error) => errors.push(error),
    setTick: () => 1,
    clearTick: () => {},
  });
  assert.equal(voice.speak("Still there."), true);
  await flush();
  assert.equal(voice.busy, false);
  assert.equal(voice.pending, 0);
  assert.equal(errors.length, 1);
  assert.equal(ctx.of("source").length, 0);
});

function loadBubbleVoice() {
  const pageListeners = new Map();
  const listen = (target) => (type, fn) => {
    const list = pageListeners.get(target) || new Map();
    const fns = list.get(type) || [];
    fns.push(fn);
    list.set(type, fns);
    pageListeners.set(target, list);
  };
  const dispatch = (target, type) => Promise.all(
    [...((pageListeners.get(target) || new Map()).get(type) || [])].map((fn) => fn()),
  );
  class FakeAudio {
    constructor() {
      this.listeners = { ended: [], error: [] };
      this.paused = true;
      this.ended = false;
      this._src = "";
      this._playToken = 0;
      this.plays = 0;
      FakeAudio.latest = this;
    }
    addEventListener(type, fn) { this.listeners[type].push(fn); }
    setAttribute() {}
    set src(value) {
      this._src = value;
      if (FakeAudio.syncEnded) this.dispatch("ended");
    }
    get src() { return this._src; }
    play() {
      this.plays += 1;
      if (this.failNext) return Promise.reject(new Error("refused"));
      this.paused = false;
      this.ended = false;
      return Promise.resolve();
    }
    pause() { this.paused = true; }
    dispatch(type) {
      for (const fn of [...this.listeners[type]]) fn();
    }
  }
  FakeAudio.syncEnded = false;
  FakeAudio.latest = null;
  const document = {
    hidden: false,
    addEventListener: listen("document"),
    dispatch: (type) => dispatch("document", type),
  };
  const window = {
    addEventListener: listen("window"),
    removeEventListener() {},
    dispatch: (type) => dispatch("window", type),
  };
  const sandbox = { Audio: FakeAudio, document, window, navigator: {}, console };
  vm.createContext(sandbox);
  vm.runInContext(
    readFileSync(new URL("../../../flutter_app/web/js/voice.js", import.meta.url), "utf8"),
    sandbox,
    { filename: "voice.js" },
  );
  const track = (promise) => {
    const box = { settled: false, ok: undefined };
    promise.then((ok) => { box.settled = true; box.ok = ok; });
    return box;
  };
  return { voice: sandbox.window.klukaiVoice, document, window, FakeAudio, track };
}

test("a clip's ended event does not resolve the next bubble's playback", async () => {
  const h = loadBubbleVoice();
  h.FakeAudio.syncEnded = true;
  const first = h.track(h.voice.play("data:audio/wav;base64,QQ=="));
  await flush();
  const second = h.track(h.voice.play("data:audio/wav;base64,Qg=="));
  h.FakeAudio.latest.dispatch("ended");
  await flush();
  assert.equal(first.settled, true);
  assert.equal(first.ok, true);
  assert.equal(second.settled, false, "the next message is still playing, not already finished");
  h.FakeAudio.latest.dispatch("ended");
  await flush();
  assert.equal(second.ok, true);
});

test("a clip paused by hiding the tab resumes, or releases the next bubble", async () => {
  const h = loadBubbleVoice();
  const first = h.track(h.voice.play("data:audio/wav;base64,QQ=="));
  await flush();
  const audio = h.FakeAudio.latest;
  audio.pause();
  h.document.hidden = true;
  await h.document.dispatch("visibilitychange");
  assert.equal(audio.plays, 1, "hidden: leave the paused clip alone");
  h.document.hidden = false;
  await h.document.dispatch("visibilitychange");
  await flush();
  assert.equal(audio.paused, false);
  assert.ok(audio.plays >= 2);

  audio.pause();
  audio.failNext = true;
  const second = h.track(h.voice.play("data:audio/wav;base64,Qg=="));
  await flush();
  assert.equal(first.ok, false, "the paused clip is released instead of pinning the channel");
  assert.equal(second.settled, true);
  assert.equal(second.ok, false);
});
