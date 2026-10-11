// Her voice on the companion stage: fetches TTS for each line she says, plays
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
const SENTENCE_ENDS = [". ", "! ", "? ", ".\n", "!\n", "?\n"];

/**
 * Keep a line under the TTS limit without cutting her off mid-word: clip at
 * the last sentence boundary that fits, or hard-cut when the only boundary is
 * so early (before limit/3) that it would drop most of what she said.
 */
export function clipForSpeech(text, limit = SPEAK_LIMIT) {
  const s = String(text || "");
  if (s.length <= limit) return s;
  // limit + 1 so a sentence whose closing space sits exactly at `limit` counts.
  const head = s.slice(0, limit + 1);
  let cut = -1;
  for (const end of SENTENCE_ENDS) cut = Math.max(cut, head.lastIndexOf(end));
  if (cut >= limit / 3) return head.slice(0, cut + 1).trimEnd();
  return s.slice(0, limit);
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
  let busy = false;
  let current = null;
  let release = null; // resolves the in-flight playBuffer() wait (stop() uses it)
  let tick = null;
  const analyser = context.createAnalyser();
  analyser.fftSize = 256;
  analyser.connect(context.destination);
  const samples = new Uint8Array(analyser.fftSize);

  function meter(on) {
    if (on && !tick) {
      tick = setTick(() => {
        analyser.getByteTimeDomainData(samples);
        onLevel(rmsLevel(samples));
      });
    } else if (!on && tick) {
      clearTick(tick);
      tick = null;
      onLevel(0);
    }
  }

  async function playBuffer(arrayBuffer) {
    const audio = await context.decodeAudioData(arrayBuffer);
    await new Promise((resolve) => {
      const src = context.createBufferSource();
      src.buffer = audio;
      src.connect(analyser);
      src.onended = resolve;
      release = resolve;
      current = src;
      onSpeaking(true);
      meter(true);
      src.start();
    });
    current = null;
    release = null;
  }

  async function drain() {
    if (busy) return;
    busy = true;
    try {
      while (queue.length && enabled) {
        const job = queue.shift();
        try {
          const buf = base64ToArrayBuffer((await tts(job.text)).audio);
          if (!enabled) break;
          await playBuffer(buf);
        } catch (e) {
          onError(e);
        }
      }
    } finally {
      busy = false;
      meter(false);
      onSpeaking(false);
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
      queue.push({ text: clipForSpeech(line) });
      drain();
      return true;
    },
    stop() {
      queue.length = 0;
      if (current) { try { current.stop(); } catch { /* already stopped */ } }
      // A source started on a suspended context (page backgrounded) never
      // fires onended; release the drain ourselves so `busy` can't stick.
      if (release) { const r = release; release = null; r(); }
    },
    get pending() { return queue.length; },
  };
}
