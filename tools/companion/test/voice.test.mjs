import { test } from "node:test";
import assert from "node:assert/strict";
import { createVoice, base64ToArrayBuffer, rmsLevel, chunkForSpeech, SPEAK_LIMIT } from "../../../flutter_app/web/companion/voice.js";
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
      const playing = ctx.of("source").filter((s) => s.startAt !== undefined && !s.done && !s.stopped);
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

test("complete long replies play sentence-sized chunks in order", async () => {
  const sentences = Array.from({ length: 8 }, (_, i) => `Sentence ${i}: ${"gentle words ".repeat(9).trim()}.`);
  const seen = [];
  const h = harness({ ttsImpl: async (t) => { seen.push(t); return { audio: btoa("a") }; } });
  h.voice.speak(`(smiles) ${sentences.join(" ")}`);
  h.voice.speak("Next reply.");
  await h.finish();
  assert.deepEqual(seen, [...sentences, "Next reply."]);
  assert.equal(h.ctx.of("source").length, seen.length);
  assert.ok(seen.every((s) => s.length <= SPEAK_LIMIT));
});

const turn = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

test("stop cancels pending TTS and lets a new reply play before the old request settles", async () => {
  const old = deferred();
  let oldSignal;
  const h = harness({ ttsImpl: (text, options) => {
    if (text === "Old.") { oldSignal = options?.signal; return old.promise; }
    return Promise.resolve({ audio: btoa(text) });
  } });
  h.voice.speak("Old.");
  h.voice.stop();
  h.voice.speak("New.");
  await turn();
  assert.equal(h.ctx.of("source").length, 1, "new reply is not blocked by old TTS");
  assert.equal(oldSignal?.aborted, true);
  old.resolve({ audio: btoa("stale") });
  await turn();
  assert.equal(h.ctx.of("source").length, 1, "stale TTS cannot play");
  assert.equal(h.speaking.at(-1), true, "stale drain cannot reset new playback");
  await h.finish();
  assert.equal(h.errors.length, 0);
});

test("mute then unmute invalidates an in-flight decode", async () => {
  const old = deferred();
  const h = harness();
  let n = 0;
  h.ctx.decodeAudioData = () => n++ === 0 ? old.promise : Promise.resolve({ duration: 1 });
  h.voice.speak("Old.");
  await turn();
  h.voice.setEnabled(false);
  h.voice.setEnabled(true);
  h.voice.speak("New.");
  await turn();
  assert.equal(h.ctx.of("source").length, 1);
  old.resolve({ duration: 1 });
  await turn();
  assert.equal(h.ctx.of("source").length, 1, "decoded stale audio is discarded");
  await h.finish();
});

test("stop resets output immediately, disconnects the source, and drops remaining chunks", async () => {
  const seen = [];
  const h = harness({ ttsImpl: async (t) => { seen.push(t); return { audio: btoa(t) }; } });
  h.voice.speak("First. Second. Third.");
  await turn();
  const source = h.ctx.of("source")[0];
  const lateEnded = source.onended;
  h.voice.stop();
  assert.equal(h.speaking.at(-1), false);
  assert.equal(h.levels.at(-1), 0);
  assert.equal(source.stopped, true);
  assert.equal(source.outs.length, 0);
  h.voice.speak("Fresh.");
  await turn();
  lateEnded();
  assert.equal(h.speaking.at(-1), true);
  await h.finish();
  assert.deepEqual(seen, ["First.", "Fresh."]);
  assert.ok(h.ctx.of("source").every((s) => s.outs.length === 0));
});

test("cancelled TTS errors stay silent without affecting the next reply", async () => {
  const old = deferred();
  const h = harness({ ttsImpl: (t) => t === "Old." ? old.promise : Promise.resolve({ audio: btoa(t) }) });
  h.voice.speak("Old.");
  h.voice.stop();
  h.voice.speak("New.");
  old.reject(new Error("aborted"));
  await h.finish();
  assert.equal(h.errors.length, 0);
  assert.equal(h.ctx.of("source").length, 1);
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

test("speech chunks retain every word and handle boundaries, empty input, and long tokens", () => {
  const chunk = chunkForSpeech;
  assert.deepEqual(chunk(null), []);
  assert.deepEqual(chunk("  "), []);
  assert.deepEqual(chunk('Ready? “Yes!”\nGo.'), ['Ready?', '“Yes!”', 'Go.']);
  assert.deepEqual(chunk("Wait... Really?! Yes."), ["Wait...", "Really?!", "Yes."]);
  assert.deepEqual(chunk("Set 3.14 then continue."), ["Set 3.14 then continue."]);
  assert.deepEqual(chunk("こんにちは。元気？はい！"), ["こんにちは。", "元気？", "はい！"]);
  const words = "long words ".repeat(130).trim();
  const chunks = chunk(words);
  assert.equal(chunks.join(" "), words);
  assert.ok(chunks.every((s) => s.length <= SPEAK_LIMIT && s.length > 0));
  const token = "z".repeat(1300);
  assert.equal(chunk(token).join(""), token);
  assert.deepEqual(chunk(token).map((s) => s.length), [500, 500, 300]);
  const unicode = "x".repeat(499) + "😀".repeat(400);
  assert.equal(chunk(unicode).join(""), unicode);
  assert.ok(chunk(unicode).every((s) => s.length <= SPEAK_LIMIT && s.isWellFormed()));
  const exact = "a".repeat(499) + ".";
  assert.deepEqual(chunk(exact + " Next."), [exact, "Next."]);
});

test("only the current chunk is requested, and silence between chunks resets the meter", async () => {
  const next = deferred();
  const seen = [];
  const h = harness({ ttsImpl: (t) => {
    seen.push(t);
    return t === "Second." ? next.promise : Promise.resolve({ audio: btoa(t) });
  } });
  h.voice.speak("First. Second.");
  await turn();
  assert.deepEqual(seen, ["First."]);
  const first = h.ctx.of("source")[0];
  first.done = true;
  first.onended();
  await turn();
  assert.deepEqual(seen, ["First.", "Second."]);
  assert.equal(first.outs.length, 0);
  assert.equal(h.speaking.at(-1), false);
  assert.equal(h.levels.at(-1), 0);
  next.resolve({ audio: btoa("Second.") });
  await h.finish();
  assert.equal(h.ctx.of("source").length, 2);
});

for (const failure of ["decode", "start"]) {
  test(`a ${failure} failure is reported and does not stall later chunks`, async () => {
    const h = harness();
    if (failure === "decode") {
      let n = 0;
      h.ctx.decodeAudioData = async () => {
        if (!n++) throw new Error("bad audio");
        return { duration: 1 };
      };
    } else {
      const create = h.ctx.createBufferSource.bind(h.ctx);
      let n = 0;
      h.ctx.createBufferSource = () => {
        const source = create();
        if (!n++) source.start = () => { throw new Error("cannot start"); };
        return source;
      };
    }
    h.voice.speak("First. Second.");
    await h.finish();
    assert.equal(h.errors.length, 1);
    assert.equal(h.ctx.of("source").filter((s) => s.startAt !== undefined).length, 1);
    assert.ok(h.ctx.of("source").every((s) => s.outs.length === 0));
    assert.equal(h.speaking.at(-1), false);
    assert.equal(h.voice.pending, 0);
  });
}

test("a zero-valued meter handle is cleared on stop, including repeated stops", async () => {
  const ctx = new FakeAudioContext();
  const cleared = [];
  const voice = createVoice({
    context: ctx, tts: async () => ({ audio: btoa("a") }),
    setTick: () => 0, clearTick: (id) => cleared.push(id),
  });
  voice.speak("Hello.");
  await turn();
  voice.stop();
  voice.stop();
  assert.deepEqual(cleared, [0]);
});
