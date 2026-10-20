// Her voice on the companion stage: fetches TTS for what she says, plays it
// through an analyser, and reports a live loudness level (mouth flaps, the 3D
// talking layer, music ducking). A reply goes out in as few requests as the
// server's 500-character cap allows (usually one), so it plays as one clip
// instead of pausing after every sentence. Playback never overlaps; the next
// clip may synthesize while the current one plays.
// Each reply is spoken once, from its `done` text; see speak().

import { speakable } from "./logic.js";

export function base64ToArrayBuffer(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

export const SPEAK_LIMIT = 500;
const ABBREVIATIONS = new Set([
  "mr", "mrs", "ms", "dr", "prof", "rev", "capt", "lt", "col", "sgt", "gen", "sr", "jr", "st",
  "vs", "etc", "approx", "dept", "fig", "vol", "inc", "ltd", "ref",
]);
/**
 * Her line as sentence-sized pieces, each within the cap, with the separator
 * that preceded each piece in the whitespace-normalized text (" ", or "" for
 * CJK sentences and hard cuts). Oversized sentences split at whitespace; only
 * a single oversized token is hard-split (never through a UTF-16 surrogate pair).
 */
function speechUnits(text, limit) {
  if (!Number.isInteger(limit) || limit < 2) throw new RangeError("Speech limit must be at least 2");
  const line = String(text || "").replace(/\s+/g, " ").trim();
  const units = [];
  function append(sentence) {
    let sep = /^\s/.test(sentence) ? " " : "";
    let rest = sentence.trim();
    while (rest.length > limit) {
      let cut = rest.lastIndexOf(" ", limit);
      let next = " ";
      if (cut <= 0) {
        cut = limit;
        next = "";
        const previous = rest.charCodeAt(cut - 1);
        if (previous >= 0xd800 && previous <= 0xdbff) cut--;
      }
      units.push({ text: rest.slice(0, cut), sep });
      rest = rest.slice(cut).trimStart();
      sep = next;
    }
    if (rest) units.push({ text: rest, sep });
  }
  // Keep common abbreviations and initials in context. Ambiguous endings like
  // "in the U.S. Tomorrow..." may share a sentence; punctuation stays intact.
  // Decimal/version dots need following whitespace to be a possible boundary.
  const ends = /[.!?…]+["'”’」』]*(?=\s|$)|[。！？]+["'”’」』]*/g;
  let start = 0;
  for (const match of line.matchAll(ends)) {
    const end = match.index + match[0].length;
    const punctuation = match[0].replace(/["'”’」』]+$/u, "");
    if (punctuation === "." && end < line.length) {
      const token = line.slice(start, match.index).match(/(?:^|[\s([{“"'‘])([A-Za-z.]+)$/u)?.[1] || "";
      if (ABBREVIATIONS.has(token.toLowerCase()) || /^[A-Z]$/.test(token) ||
          /^(?:[A-Za-z]\.)+[A-Za-z]$/.test(token)) continue;
    }
    append(line.slice(start, end));
    start = end;
  }
  append(line.slice(start));
  return units;
}

/** Her line split into sentences, each within the cap (see speechUnits). */
export function speechSentences(text, limit = SPEAK_LIMIT) {
  return speechUnits(text, limit).map((unit) => unit.text);
}

/**
 * The TTS requests for one reply: consecutive sentences packed greedily into
 * chunks of at most `limit` characters, so a typical reply is ONE clip and a
 * long one uses as few as possible (greedy packing is optimal when the order
 * is fixed and sentences stay whole). A sentence is only ever split when it
 * alone is over the cap; the original separators are kept.
 */
export function chunkForSpeech(text, limit = SPEAK_LIMIT) {
  const chunks = [];
  let current = "";
  for (const { text: piece, sep } of speechUnits(text, limit)) {
    if (current && current.length + sep.length + piece.length <= limit) current += sep + piece;
    else {
      if (current) chunks.push(current);
      current = piece;
    }
  }
  if (current) chunks.push(current);
  return chunks;
}

/** RMS loudness 0..1 from 8-bit time-domain samples (128 = silence). */
export function rmsLevel(samples) {
  if (!samples || !samples.length) return 0;
  let sum = 0;
  for (let i = 0; i < samples.length; i++) {
    const v = (samples[i] - 128) / 128;
    sum += v * v;
  }
  return Math.min(1, Math.sqrt(sum / samples.length) * 3);
}

export function createVoice({
  context, tts, onLevel = () => {}, onSpeaking = () => {}, onError = () => {}, onBusy = () => {},
  setTick = (fn) => setInterval(fn, 40), clearTick = clearInterval,
}) {
  const queue = [];
  let enabled = true;
  // Busy spans a whole reply (synthesis, every clip, the gaps between them):
  // a voice button can show one state per tap instead of flickering per clip.
  let busy = false;
  function setBusy(on) {
    if (busy === on) return;
    busy = on;
    onBusy(on);
  }
  // Each drain owns its requests/playback. A stopped drain may still finish
  // decoding, but it must never play or reset the next drain's output.
  let active = null;
  let tick = null;
  const analyser = context.createAnalyser();
  analyser.fftSize = 256;
  analyser.connect(context.destination);
  const samples = new Uint8Array(analyser.fftSize);

  function meter(on) {
    if (on && tick === null) {
      tick = setTick(() => {
        analyser.getByteTimeDomainData(samples);
        onLevel(rmsLevel(samples));
      });
    } else if (!on) {
      if (tick !== null) clearTick(tick);
      tick = null;
      onLevel(0);
    }
  }

  function isActive(run) {
    return active === run && enabled && !run.controller.signal.aborted;
  }

  function requestNext(run) {
    if (!isActive(run) || !queue.length) return null;
    const text = queue.shift();
    // Install both handlers now: a lookahead may fail before playback ends.
    // There is at most one request/cache ahead in the active generation.
    try {
      return Promise.resolve(tts(text, { signal: run.controller.signal })).then(
        (result) => ({ ok: true, result }), (error) => ({ ok: false, error }),
      );
    } catch (error) {
      return Promise.resolve({ ok: false, error });
    }
  }

  function prefetch(run) {
    if (isActive(run) && run.playing && !run.next) run.next = requestNext(run);
  }

  async function playBuffer(arrayBuffer, run) {
    const audio = await context.decodeAudioData(arrayBuffer);
    if (!isActive(run)) return;
    await new Promise((resolve, reject) => {
      const src = context.createBufferSource();
      let finished = false;
      function finish(stop = false) {
        if (finished) return;
        finished = true;
        src.onended = null;
        if (stop) { try { src.stop(); } catch { /* not started/already stopped */ } }
        src.disconnect();
        run.cancelPlay = null;
        run.playing = false;
        if (isActive(run)) {
          meter(false);
          onSpeaking(false);
        }
        resolve();
      }
      run.cancelPlay = () => finish(true);
      try {
        src.buffer = audio;
        src.connect(analyser);
        src.onended = () => finish();
        src.start();
        run.playing = true;
        onSpeaking(true);
        meter(true);
        prefetch(run);
      } catch (error) {
        // Reject before cleanup resolves the wait; the next chunk can proceed.
        reject(error);
        finish(true);
      }
    });
  }

  async function drain() {
    if (active) return;
    const run = { controller: new AbortController(), cancelPlay: null, playing: false, next: null };
    active = run;
    setBusy(true);
    try {
      while (isActive(run)) {
        const request = run.next || requestNext(run);
        run.next = null;
        if (!request) break;
        try {
          const outcome = await request;
          if (!isActive(run)) break;
          if (!outcome.ok) throw outcome.error;
          await playBuffer(base64ToArrayBuffer(outcome.result.audio), run);
        } catch (e) {
          if (isActive(run)) onError(e);
        }
      }
    } finally {
      if (active === run) {
        active = null;
        meter(false);
        onSpeaking(false);
        setBusy(false);
      }
    }
  }

  return {
    get enabled() { return enabled; },
    setEnabled(on) {
      enabled = on;
      if (!on) this.stop();
    },
    /**
     * Queue a line of hers; stage directions are stripped first. This is the
     * stage's only voice channel: the server's `voice_audio` clips (its TTS
     * for PWA voice input) are ignored here, or she would say every such
     * reply twice.
     */
    speak(text) {
      const line = speakable(text);
      if (!enabled || !line) return false;
      queue.push(...chunkForSpeech(line));
      if (active) prefetch(active);
      drain();
      return true;
    },
    stop() {
      queue.length = 0;
      const run = active;
      active = null;
      if (run) {
        run.next = null;
        run.controller.abort();
        // Also releases playback on suspended contexts where onended never fires.
        run.cancelPlay?.();
      }
      meter(false);
      onSpeaking(false);
      setBusy(false);
    },
    get pending() { return queue.length + (active?.next ? 1 : 0); },
    /** A reply is being synthesized or played (between speak() and its end or stop()). */
    get busy() { return busy; },
  };
}
