import { test } from "node:test";
import assert from "node:assert/strict";
import {
  createVoice, base64ToArrayBuffer, rmsLevel, chunkForSpeech, speechSentences, SPEAK_LIMIT,
} from "../../../flutter_app/web/companion/voice.js";
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
  const levels = []; const speaking = []; const errors = []; const ticks = []; const busy = [];
  const tts = ttsImpl || (async (text) => ({ audio: btoa(`wav:${text}`) }));
  const voice = createVoice({
    context: ctx, tts, onBusy: (b) => busy.push(b),
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
  return { ctx, voice, levels, speaking, errors, ticks, busy, finish };
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

test("long replies play in as few clips as the cap allows, in order", async () => {
  // Eight ~130-character sentences: three fit in one 500-character request.
  const sentences = Array.from({ length: 8 }, (_, i) => `Sentence ${i}: ${"gentle words ".repeat(9).trim()}.`);
  const seen = [];
  const h = harness({ ttsImpl: async (t) => { seen.push(t); return { audio: btoa("a") }; } });
  h.voice.speak(`(smiles) ${sentences.join(" ")}`);
  h.voice.speak("Next reply.");
  await h.finish();
  assert.deepEqual(seen, [
    sentences.slice(0, 3).join(" "), sentences.slice(3, 6).join(" "), sentences.slice(6).join(" "), "Next reply.",
  ]);
  assert.equal(h.ctx.of("source").length, seen.length);
  assert.ok(seen.every((s) => s.length <= SPEAK_LIMIT));
});

test("a typical reply is one TTS request and plays as one clip", async () => {
  // Owner 2026-10-19: "Voice synth works but stops after sentences".
  const seen = [];
  const h = harness({ ttsImpl: async (t) => { seen.push(t); return { audio: btoa(t) }; } });
  const reply = "(she glances at the window) The perimeter is quiet, Commander. Groza has the watch. "
    + "Sleep. *adjusts her cap* I'll still be here when you wake up!";
  assert.equal(h.voice.speak(reply), true);
  await h.finish();
  assert.deepEqual(seen, [
    "The perimeter is quiet, Commander. Groza has the watch. Sleep. I'll still be here when you wake up!",
  ]);
  assert.equal(h.ctx.of("source").length, 1);
});

test("busy spans the whole reply, not each clip, and stop() ends it", async () => {
  const words = (n) => `${"word ".repeat(n).trim()}.`;
  const h = harness();
  h.voice.speak(`${words(80)} ${words(80)}`); // two ~400-character sentences: two clips
  assert.equal(h.voice.busy, true);
  await h.finish();
  assert.equal(h.ctx.of("source").length, 2);
  assert.deepEqual(h.busy, [true, false], "one busy span across both clips and the gap between them");
  assert.deepEqual(h.speaking.filter((on) => on), [true, true], "speaking still reports each clip");
  assert.equal(h.voice.busy, false);
  h.voice.speak("Again.");
  h.voice.stop();
  assert.deepEqual(h.busy, [true, false, true, false]);
  h.voice.stop();
  assert.deepEqual(h.busy, [true, false, true, false], "a second stop reports nothing new");
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
  h.voice.speak("First.");
  h.voice.speak("Second.");
  h.voice.speak("Third.");
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
  assert.deepEqual(seen, ["First.", "Second.", "Fresh."], "prefetched second clip is discarded, not played");
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
  assert.deepEqual(speechSentences(null), []);
  assert.deepEqual(speechSentences('Ready? “Yes!”\nGo.'), ['Ready?', '“Yes!”', 'Go.']);
  assert.deepEqual(speechSentences("Wait... Really?! Yes."), ["Wait...", "Really?!", "Yes."]);
  assert.deepEqual(speechSentences("Set 3.14 then continue."), ["Set 3.14 then continue."]);
  assert.deepEqual(speechSentences("こんにちは。元気？はい！"), ["こんにちは。", "元気？", "はい！"]);
  // Under the cap a reply is one request: the whitespace-normalized text itself.
  assert.deepEqual(chunk('Ready? “Yes!”\nGo.'), ['Ready? “Yes!” Go.']);
  assert.deepEqual(chunk("Wait...   Really?!\tYes."), ["Wait... Really?! Yes."]);
  assert.deepEqual(chunk("こんにちは。元気？はい！"), ["こんにちは。元気？はい！"], "no spaces added between CJK sentences");
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

for (const [text, want] of [
  ["Dr. A. B. Smith met Mr. Jones and Mrs. Green. Ms. Lee joined Prof. Rao.",
    ["Dr. A. B. Smith met Mr. Jones and Mrs. Green.", "Ms. Lee joined Prof. Rao."]],
  ["Use examples, e.g. apples or pears. Clarify, i.e. explain simply.",
    ["Use examples, e.g. apples or pears.", "Clarify, i.e. explain simply."]],
  ["The U.S. team met the U.K. delegation. Then they left.",
    ["The U.S. team met the U.K. delegation.", "Then they left."]],
  ["Meet J. R. R. Tolkien today. All set.", ["Meet J. R. R. Tolkien today.", "All set."]],
  ["Temperature is 3.14. Version v1.2.3 is ready. Next.",
    ["Temperature is 3.14.", "Version v1.2.3 is ready.", "Next."]],
  ["I gave it a 3. She gave it a 4.", ["I gave it a 3.", "She gave it a 4."]],
  ["Use 3D. Then continue.", ["Use 3D.", "Then continue."]],
  ["We used 4G. Then left.", ["We used 4G.", "Then left."]],
  ["See αDr. Next.", ["See αDr.", "Next."]],
  ["Review the ADR. Next step.", ["Review the ADR.", "Next step."]],
  ["Release v1.2.3. It is stable.", ["Release v1.2.3.", "It is stable."]],
  ['She said, “Dr. Smith is here?!” Next.', ['She said, “Dr. Smith is here?!”', "Next."]],
  ['"はい！" Next.', ['"はい！"', "Next."]],
  ["「はい！」次です。", ["「はい！」", "次です。"]],
]) {
  test(`speech boundaries preserve abbreviations and punctuation: ${text}`, () => {
    assert.deepEqual(speechSentences(text), want);
    assert.deepEqual(chunkForSpeech(text), [want.join(want[0].endsWith("」") ? "" : " ")]);
  });
}

test("conservative abbreviation handling retains ambiguous endings within the cap", () => {
  const text = "I live in the U.S. Tomorrow I leave. Plan A. Next step.";
  assert.deepEqual(speechSentences(text), ["I live in the U.S. Tomorrow I leave.", "Plan A. Next step."]);
  const long = "x".repeat(495) + " Dr. Smith arrived.";
  assert.equal(chunkForSpeech(long).join(" "), long);
  assert.ok(chunkForSpeech(long).every((s) => s.length <= SPEAK_LIMIT));
  const quote = "a".repeat(498) + '."';
  assert.deepEqual(chunkForSpeech(quote + " Next."), [quote, "Next."]);
  const emoji = "x".repeat(496) + " 😀.";
  assert.deepEqual(chunkForSpeech(emoji + " Next."), [emoji, "Next."]);
});

test("sentences pack greedily into the fewest chunks within the cap, never split when they fit", () => {
  const sentence = (n, i) => `S${i} ${"w".repeat(n - 4)}.`; // exactly n characters
  for (const lengths of [[120, 120, 120, 120], [300, 300, 300], [495, 4, 495], [250, 249, 250, 249], [60, 440, 499, 4]]) {
    const parts = lengths.map(sentence);
    const text = parts.join(" ");
    const chunks = chunkForSpeech(text);
    assert.ok(chunks.every((c) => c.length > 0 && c.length <= SPEAK_LIMIT), `${lengths}: within the cap`);
    assert.equal(chunks.join(" "), text, `${lengths}: every word, in order`);
    // Each sentence lands whole in one chunk.
    for (const part of parts) assert.ok(chunks.some((c) => c.includes(part)), `${lengths}: ${part.slice(0, 4)} kept whole`);
    // Greedy count: a chunk closes only when the next sentence would not fit.
    let expected = 0, size = -1;
    for (const n of lengths) {
      if (size < 0 || size + 1 + n > SPEAK_LIMIT) { expected++; size = n; } else size += 1 + n;
    }
    assert.equal(chunks.length, expected, `${lengths}: fewest chunks`);
  }
  assert.equal(chunkForSpeech([120, 120, 120, 120].map(sentence).join(" ")).length, 1, "four short sentences: one clip");
});

test("pieces of an oversized sentence still share a chunk with what follows", () => {
  const long = `${"long words ".repeat(64).trim()}.`; // 703 characters, one sentence
  const chunks = chunkForSpeech(`${long} Short one. Another.`);
  assert.equal(chunks.join(" "), `${long} Short one. Another.`);
  assert.ok(chunks.every((c) => c.length <= SPEAK_LIMIT));
  assert.equal(chunks.length, 2, "the 200-character tail and both short sentences fit one request");
  assert.ok(chunks[1].endsWith("Short one. Another."));
  const token = "z".repeat(700);
  assert.deepEqual(chunkForSpeech(`${token} Hi.`).map((c) => c.length), [500, 204], "hard cuts keep no space; the next sentence keeps its own");
  assert.equal(chunkForSpeech(`${token} Hi.`).join(""), `${token} Hi.`);
});

test("one upcoming chunk is requested during playback, and gaps reset the meter", async () => {
  const next = deferred();
  const seen = [];
  const h = harness({ ttsImpl: (t) => {
    seen.push(t);
    return t === "Second." ? next.promise : Promise.resolve({ audio: btoa(t) });
  } });
  h.voice.speak("First.");
  h.voice.speak("Second.");
  await turn();
  assert.deepEqual(seen, ["First.", "Second."]);
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

test("lookahead waits for playback, overlaps synthesis, and holds at most one future clip", async () => {
  const requests = new Map(["First.", "Second.", "Third."].map((t) => [t, deferred()]));
  const decodeFirst = deferred();
  const seen = [];
  let decoding = false, inFlight = 0, maximum = 0;
  const h = harness({ ttsImpl: (text) => {
    seen.push(text);
    inFlight++;
    maximum = Math.max(maximum, inFlight);
    return requests.get(text).promise.finally(() => inFlight--);
  } });
  let decodeCount = 0;
  h.ctx.decodeAudioData = () => {
    if (!decodeCount++) { decoding = true; return decodeFirst.promise; }
    return Promise.resolve({ duration: 1 });
  };
  h.voice.speak("First.");
  h.voice.speak("Second.");
  h.voice.speak("Third.");
  assert.deepEqual(seen, ["First."]);
  requests.get("First.").resolve({ audio: btoa("first") });
  await turn();
  assert.equal(decoding, true);
  assert.deepEqual(seen, ["First."], "no speculation during decoding");
  decodeFirst.resolve({ duration: 1 });
  await turn();
  assert.deepEqual(seen, ["First.", "Second."], "synthesis overlaps the first playing source");
  assert.equal(h.ctx.of("source").length, 1);
  assert.equal(h.voice.pending, 2, "waiting text includes the single prefetched clip");
  requests.get("Second.").resolve({ audio: btoa("second") });
  await turn();
  assert.deepEqual(seen, ["First.", "Second."], "a ready cache does not fetch the third clip");
  assert.equal(h.ctx.of("source").length, 1);
  const first = h.ctx.of("source")[0];
  first.done = true; first.onended();
  await turn();
  assert.equal(h.ctx.of("source").length, 2);
  assert.deepEqual(seen, ["First.", "Second.", "Third."]);
  assert.equal(maximum, 1, "only one synthesis request in the active generation");
  requests.get("Third.").resolve({ audio: btoa("third") });
  await h.finish();
  assert.equal(h.ctx.of("source").length, 3);
});

test("a reply appended during playback starts only one lookahead request", async () => {
  const second = deferred();
  const seen = [];
  const h = harness({ ttsImpl: (text) => {
    seen.push(text);
    return text === "Second." ? second.promise : Promise.resolve({ audio: btoa(text) });
  } });
  h.voice.speak("First.");
  await turn();
  h.voice.speak("Second.");
  h.voice.speak("Third.");
  assert.deepEqual(seen, ["First.", "Second."]);
  assert.equal(h.voice.pending, 2);
  second.resolve({ audio: btoa("second") });
  await h.finish();
  assert.deepEqual(seen, ["First.", "Second.", "Third."]);
});

test("early lookahead rejection is handled once after the current clip ends", async () => {
  const second = deferred();
  const seen = [];
  const h = harness({ ttsImpl: (text) => {
    seen.push(text);
    return text === "Second." ? second.promise : Promise.resolve({ audio: btoa(text) });
  } });
  for (const line of ["First.", "Second.", "Third."]) h.voice.speak(line);
  await turn();
  assert.deepEqual(seen, ["First.", "Second."]);
  second.reject(new Error("lookahead failed"));
  await turn();
  assert.equal(h.errors.length, 0);
  assert.equal(h.speaking.at(-1), true);
  assert.deepEqual(seen, ["First.", "Second."], "failure does not fetch the third clip early");
  await h.finish();
  assert.equal(h.errors.length, 1);
  assert.equal(h.ctx.of("source").length, 2);
  assert.deepEqual(seen, ["First.", "Second.", "Third."]);
});

test("mute aborts pending lookahead and late failures cannot reset fresh playback", async () => {
  const second = deferred();
  const seen = [];
  let staleSignal;
  const h = harness({ ttsImpl: (text, { signal }) => {
    seen.push(text);
    if (text === "Second.") { staleSignal = signal; return second.promise; }
    return Promise.resolve({ audio: btoa(text) });
  } });
  for (const line of ["First.", "Second.", "Third."]) h.voice.speak(line);
  await turn();
  assert.deepEqual(seen, ["First.", "Second."]);
  const first = h.ctx.of("source")[0], lateEnded = first.onended;
  h.voice.setEnabled(false);
  assert.equal(staleSignal.aborted, true);
  assert.equal(h.voice.pending, 0);
  assert.equal(first.outs.length, 0);
  assert.equal(h.levels.at(-1), 0);
  assert.equal(h.speaking.at(-1), false);
  h.voice.setEnabled(true);
  h.voice.speak("Fresh.");
  await turn();
  second.reject(new Error("cancelled old lookahead"));
  lateEnded();
  await turn();
  assert.equal(h.ctx.of("source").length, 2);
  assert.equal(h.speaking.at(-1), true);
  assert.deepEqual(seen, ["First.", "Second.", "Fresh."]);
  assert.equal(h.errors.length, 0);
  await h.finish();
});

for (const failure of ["base64", "decode", "start"]) {
  test(`a prefetched ${failure} failure does not block the later chunk`, async () => {
    const h = harness({ ttsImpl: async (text) => ({
      audio: failure === "base64" && text === "Second." ? "%invalid%" : btoa(text),
    }) });
    if (failure === "decode") {
      let n = 0;
      h.ctx.decodeAudioData = async () => {
        if (++n === 2) throw new Error("bad prefetched audio");
        return { duration: 1 };
      };
    } else if (failure === "start") {
      const create = h.ctx.createBufferSource.bind(h.ctx);
      let n = 0;
      h.ctx.createBufferSource = () => {
        const source = create();
        if (++n === 2) source.start = () => { throw new Error("cannot start prefetched audio"); };
        return source;
      };
    }
    for (const line of ["First.", "Second.", "Third."]) h.voice.speak(line);
    await h.finish();
    assert.equal(h.errors.length, 1);
    assert.equal(h.ctx.of("source").filter((s) => s.startAt !== undefined).length, 2);
    assert.ok(h.ctx.of("source").every((s) => s.outs.length === 0));
    assert.equal(h.voice.pending, 0);
  });
}

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
    h.voice.speak("First.");
    h.voice.speak("Second.");
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
