// Her voice on the companion stage: fetches TTS for each line she says, plays
// it through an analyser, and reports a live loudness level (mouth flaps,
// the 3D talking layer, music ducking). Lines queue; nothing overlaps.

import { speakable } from "./logic.js";

export function base64ToArrayBuffer(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
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
      current = src;
      onSpeaking(true);
      meter(true);
      src.start();
    });
    current = null;
  }

  async function drain() {
    if (busy) return;
    busy = true;
    try {
      while (queue.length && enabled) {
        const job = queue.shift();
        try {
          const buf = job.audio
            ? base64ToArrayBuffer(job.audio)
            : base64ToArrayBuffer((await tts(job.text)).audio);
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
    /** Queue a line of hers; stage directions are stripped first. */
    speak(text) {
      const line = speakable(text);
      if (!enabled || !line) return false;
      queue.push({ text: line.slice(0, 500) });
      drain();
      return true;
    },
    /** A ready-made clip from the server (voice_audio frame). */
    play(audioB64) {
      if (!enabled || !audioB64) return false;
      queue.push({ audio: audioB64 });
      drain();
      return true;
    },
    stop() {
      queue.length = 0;
      if (current) { try { current.stop(); } catch { /* already stopped */ } }
    },
    get pending() { return queue.length; },
  };
}
