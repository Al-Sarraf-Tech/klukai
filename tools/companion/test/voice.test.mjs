import { test } from "node:test";
import assert from "node:assert/strict";
import { createVoice, base64ToArrayBuffer, rmsLevel } from "../../../flutter_app/web/companion/voice.js";
import { FakeAudioContext } from "./fake-audio.mjs";

test("base64 and loudness helpers", () => {
  const buf = base64ToArrayBuffer(btoa("RIFF"));
  assert.deepEqual([...new Uint8Array(buf)], [82, 73, 70, 70]);
  assert.equal(rmsLevel(new Uint8Array(64).fill(128)), 0);
  assert.equal(rmsLevel(null), 0);
  assert.ok(rmsLevel(new Uint8Array(64).fill(200)) > 0.5);
  assert.equal(rmsLevel(new Uint8Array(64).fill(255)), 1);
});

function harness({ ttsImpl } = {}) {
  const ctx = new FakeAudioContext();
  const levels = []; const speaking = []; const errors = []; const ticks = [];
  const tts = ttsImpl || (async (text) => ({ audio: btoa(`wav:${text}`) }));
  const voice = createVoice({
    context: ctx, tts,
    onLevel: (l) => levels.push(l), onSpeaking: (s) => speaking.push(s), onError: (e) => errors.push(e),
    setTick: (fn) => { ticks.push(fn); return ticks.length; }, clearTick: () => {},
  });
  // Finish whichever clip is playing.
  const finish = async () => {
    for (let i = 0; i < 20; i++) {
      await new Promise((r) => setTimeout(r, 0));
      const playing = ctx.of("source").filter((s) => s.startAt !== undefined && !s.done);
      for (const s of playing) { s.done = true; s.onended(); }
    }
  };
  return { ctx, voice, levels, speaking, errors, ticks, finish };
}

test("lines queue, play in order, meter loudness, and signal speaking", async () => {
  const h = harness();
  assert.equal(h.voice.speak("(I look away) Report."), true);
  assert.equal(h.voice.speak("Then sleep."), true);
  assert.equal(h.voice.speak("(sigh)"), false, "pure stage direction says nothing");
  // Mid-clip: the meter reads the analyser.
  for (let i = 0; i < 5 && !h.ticks.length; i++) await new Promise((r) => setTimeout(r, 0));
  h.ctx.of("analyser")[0].level = 0.5;
  h.ticks[0]();
  assert.ok(h.levels.at(-1) > 0);
  await h.finish();
  assert.equal(h.ctx.of("source").length, 2);
  assert.deepEqual(h.speaking.slice(0, 1), [true]);
  assert.equal(h.speaking.at(-1), false);
  assert.equal(h.levels.at(-1), 0, "meter resets when she stops");
  assert.equal(h.voice.pending, 0);
});

test("server clips play directly; disabling stops and clears", async () => {
  const h = harness();
  assert.equal(h.voice.play(btoa("clip")), true);
  assert.equal(h.voice.play(""), false);
  await h.finish();
  assert.equal(h.ctx.of("source").length, 1);
  h.voice.setEnabled(false);
  assert.equal(h.voice.enabled, false);
  assert.equal(h.voice.speak("Hello."), false);
  assert.equal(h.voice.play(btoa("x")), false);
  h.voice.setEnabled(true);
  h.voice.speak("One.");
  h.voice.speak("Two.");
  h.voice.stop();
  await h.finish();
  assert.ok(h.voice.pending === 0);
});

test("a failed TTS call is reported and the queue moves on", async () => {
  let n = 0;
  const h = harness({ ttsImpl: async (t) => { if (n++ === 0) throw new Error("503"); return { audio: btoa(t) }; } });
  h.voice.speak("First.");
  h.voice.speak("Second.");
  await h.finish();
  assert.equal(h.errors.length, 1);
  assert.equal(h.ctx.of("source").length, 1);
  assert.equal(h.speaking.at(-1), false);
});
