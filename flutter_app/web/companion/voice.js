// Her voice on the companion stage: fetches TTS for each sentence she says, plays
// it through an analyser, and reports a live loudness level (mouth flaps,
// the 3D talking layer, music ducking). Lines queue; nothing overlaps.
// Each reply is spoken once, from its `done` text; see speak().

import { speakable } from "./logic.js";

export function base64ToArrayBuffer(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

export const SPEAK_LIMIT = 500;
/**
 * Speak the whole reply in sentence-sized requests, each within the server's
 * 500-character cap. Oversized sentences split at whitespace; only a single
 * oversized token is hard-split (never through a UTF-16 surrogate pair).
 */
export function chunkForSpeech(text, limit = SPEAK_LIMIT) {
  if (!Number.isInteger(limit) || limit < 2) throw new RangeError("Speech limit must be at least 2");
  const line = String(text || "").replace(/\s+/g, " ").trim();
  const chunks = [];
  function append(sentence) {
    let rest = sentence.trim();
    while (rest.length > limit) {
      let cut = rest.lastIndexOf(" ", limit);
      if (cut <= 0) {
        cut = limit;
        const previous = rest.charCodeAt(cut - 1);
        if (previous >= 0xd800 && previous <= 0xdbff) cut--;
      }
      chunks.push(rest.slice(0, cut));
      rest = rest.slice(cut).trimStart();
    }
    if (rest) chunks.push(rest);
  }
  // Decimal points do not end sentences. Keep closing quotation marks with
  // their sentence, and recognise CJK punctuation without a following space.
  const ends = /[.!?…]+["'”’]*(?=\s|$)|[。！？]+[”’」』]*/g;
  let start = 0;
  for (const match of line.matchAll(ends)) {
    const end = match.index + match[0].length;
    append(line.slice(start, end));
    start = end;
  }
  append(line.slice(start));
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
  context, tts, onLevel = () => {}, onSpeaking = () => {}, onError = () => {},
  setTick = (fn) => setInterval(fn, 40), clearTick = clearInterval,
}) {
  const queue = [];
  let enabled = true;
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
        onSpeaking(true);
        meter(true);
      } catch (error) {
        // Reject before cleanup resolves the wait; the next chunk can proceed.
        reject(error);
        finish(true);
      }
    });
  }

  async function drain() {
    if (active) return;
    const run = { controller: new AbortController(), cancelPlay: null };
    active = run;
    try {
      while (queue.length && isActive(run)) {
        const text = queue.shift();
        try {
          const result = await tts(text, { signal: run.controller.signal });
          if (!isActive(run)) break;
          await playBuffer(base64ToArrayBuffer(result.audio), run);
        } catch (e) {
          if (isActive(run)) onError(e);
        }
      }
    } finally {
      if (active === run) {
        active = null;
        meter(false);
        onSpeaking(false);
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
      drain();
      return true;
    },
    stop() {
      queue.length = 0;
      const run = active;
      active = null;
      if (run) {
        run.controller.abort();
        // Also releases playback on suspended contexts where onended never fires.
        run.cancelPlay?.();
      }
      meter(false);
      onSpeaking(false);
    },
    get pending() { return queue.length; },
  };
}
