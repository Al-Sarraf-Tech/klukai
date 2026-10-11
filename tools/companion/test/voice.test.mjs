import { test } from "node:test";
import assert from "node:assert/strict";
import { createVoice, base64ToArrayBuffer, rmsLevel, clipForSpeech, SPEAK_LIMIT } from "../../../flutter_app/web/companion/voice.js";
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

test("her only voice channel is speak(); disabling stops and clears", async () => {
  // The stage used to also play the server's `voice_audio` clips (its TTS for
  // PWA voice input), so a reply to a spoken question was heard twice: once
  // from speak() on `done` and once from the clip. Server clips are no longer
  // a stage channel — there is no play() — and logic.js drops the frame.
  const h = harness();
  assert.equal(typeof h.voice.play, "undefined");
  h.voice.setEnabled(false);
  assert.equal(h.voice.enabled, false);
  assert.equal(h.voice.speak("Hello."), false);
  await h.finish();
  assert.equal(h.ctx.of("source").length, 0);
  h.voice.setEnabled(true);
  h.voice.speak("One.");
  h.voice.speak("Two.");
  h.voice.stop();
  await h.finish();
  assert.ok(h.voice.pending === 0);
});

test("stop() releases a clip whose onended never fires (suspended context)", async () => {
  // iOS: a source started while the AudioContext is suspended never ends, so
  // the drain stayed `busy` forever and every later line was silently queued.
  const h = harness();
  h.voice.speak("First.");
  for (let i = 0; i < 20 && !h.ctx.of("source").length; i++) await new Promise((r) => setTimeout(r, 0));
  assert.equal(h.ctx.of("source").length, 1, "the first clip started");
  h.voice.speak("Stuck behind the first.");
  assert.equal(h.voice.pending, 1);
  h.voice.stop(); // nobody fires onended on the first source
  assert.equal(h.ctx.of("source")[0].stopped, true);
  h.voice.speak("Second.");
  await h.finish();
  assert.equal(h.ctx.of("source").length, 2, "the queue moved on after stop()");
  assert.equal(h.speaking.at(-1), false);
  assert.equal(h.voice.pending, 0);
});

test("long lines clip at a sentence boundary, never mid-word", async () => {
  const sentence = "She checks the perimeter again. "; // 32 chars
  const long = sentence.repeat(20); // 640 chars
  const clipped = clipForSpeech(long);
  assert.ok(clipped.length <= SPEAK_LIMIT);
  assert.ok(clipped.endsWith("again."), clipped.slice(-20));
  assert.equal(clipped.length, 15 * sentence.length - 1, "15 whole sentences, trailing space trimmed");
  // "? " and "! " and ".\n" count as boundaries too.
  assert.ok(clipForSpeech(`${"x".repeat(300)}? ${"y".repeat(300)}`).endsWith("?"));
  assert.ok(clipForSpeech(`${"x".repeat(300)}! ${"y".repeat(300)}`).endsWith("!"));
  assert.ok(clipForSpeech(`${"x".repeat(300)}.\n${"y".repeat(300)}`).endsWith("."));
  // A boundary whose space sits exactly at the limit still counts.
  assert.equal(clipForSpeech(`${"a".repeat(499)}. ${"b".repeat(10)}`).length, 500);
  // The only boundary is very early (< limit/3): hard cut keeps more of her words.
  const early = `Yes. ${"w".repeat(700)}`;
  assert.equal(clipForSpeech(early).length, SPEAK_LIMIT);
  // No boundary at all: hard cut.
  assert.equal(clipForSpeech("z".repeat(900)).length, SPEAK_LIMIT);
  // Short lines pass through untouched.
  assert.equal(clipForSpeech("Report."), "Report.");
  assert.equal(clipForSpeech(null), "");
  // speak() applies it: the TTS request carries the clipped text.
  const seen = [];
  const h = harness({ ttsImpl: async (t) => { seen.push(t); return { audio: btoa("a") }; } });
  h.voice.speak(long);
  await h.finish();
  assert.equal(seen.length, 1);
  assert.ok(seen[0].length <= SPEAK_LIMIT && seen[0].endsWith("again."));
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
