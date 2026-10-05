// Generative ambient score for the companion stage — Web Audio only, no
// recorded tracks (no copyright, no downloads). The scene follows Her Day
// (deck / lounge / hangar / night / mission / arcade), her mood sets the
// brightness, rain and snow add a noise bed, and the music ducks under her
// voice. iOS: start() must run inside a user gesture.

import { SCENES, chordNotes, midiToHz } from "./logic.js";

const LOOKAHEAD_S = 0.25;
const TICK_MS = 60;
const MASTER = 0.16;
const DUCKED = 0.06;

function noiseBuffer(ctx, seconds) {
  const len = Math.floor(ctx.sampleRate * seconds);
  const buf = ctx.createBuffer(2, len, ctx.sampleRate);
  for (let ch = 0; ch < 2; ch++) {
    const d = buf.getChannelData(ch);
    for (let i = 0; i < len; i++) d[i] = Math.random() * 2 - 1;
  }
  return buf;
}

function reverbBuffer(ctx, seconds = 2.8) {
  const buf = noiseBuffer(ctx, seconds);
  for (let ch = 0; ch < 2; ch++) {
    const d = buf.getChannelData(ch);
    for (let i = 0; i < d.length; i++) d[i] *= Math.pow(1 - i / d.length, 3);
  }
  return buf;
}

export function createMusic({
  AudioContextImpl = globalThis.AudioContext || globalThis.webkitAudioContext,
  context = null, // share one AudioContext with her voice (iOS prefers one)
  rand = Math.random,
} = {}) {
  let ctx = null;
  let master, bus, filter, wet, rainGain;
  let timer = null;
  let playing = false;
  let nextTime = 0;
  let step = 0;
  let state = { scene: "deck", rain: false, snow: false, brightness: 0 };

  function build() {
    ctx = context || new AudioContextImpl();
    master = ctx.createGain();
    master.gain.value = 0;
    const comp = ctx.createDynamicsCompressor();
    master.connect(comp);
    comp.connect(ctx.destination);

    filter = ctx.createBiquadFilter();
    filter.type = "lowpass";
    filter.Q.value = 0.4;
    bus = ctx.createGain();
    bus.connect(filter);
    filter.connect(master);

    const verb = ctx.createConvolver();
    verb.buffer = reverbBuffer(ctx);
    wet = ctx.createGain();
    wet.gain.value = 0.45;
    bus.connect(verb);
    verb.connect(wet);
    wet.connect(master);

    // Rain/snow bed: looped noise through a band-pass.
    const src = ctx.createBufferSource();
    src.buffer = noiseBuffer(ctx, 2);
    src.loop = true;
    const bp = ctx.createBiquadFilter();
    bp.type = "bandpass";
    bp.frequency.value = 1400;
    bp.Q.value = 0.6;
    rainGain = ctx.createGain();
    rainGain.gain.value = 0;
    src.connect(bp);
    bp.connect(rainGain);
    rainGain.connect(master);
    src.start();
    applyState();
  }

  function applyState() {
    if (!ctx) return;
    const t = ctx.currentTime;
    const cutoff = 700 + (state.brightness + 1) * 1100; // 700–2900 Hz
    filter.frequency.setTargetAtTime(cutoff, t, 1.5);
    rainGain.gain.setTargetAtTime(state.rain ? 0.05 : state.snow ? 0.02 : 0, t, 2);
  }

  function voice(freq, start, dur, { type = "triangle", gain = 0.05, attack = 0.02, release = 0.4, detune = 0 }) {
    const osc = ctx.createOscillator();
    osc.type = type;
    osc.frequency.value = freq;
    osc.detune.value = detune;
    const g = ctx.createGain();
    g.gain.setValueAtTime(0, start);
    g.gain.linearRampToValueAtTime(gain, start + attack);
    g.gain.setTargetAtTime(0, start + Math.max(attack, dur), release / 3);
    osc.connect(g);
    g.connect(bus);
    osc.start(start);
    osc.stop(start + dur + release + 0.1);
  }

  function scheduleStep(t) {
    const sc = SCENES[state.scene] || SCENES.deck;
    const eighth = 60 / sc.bpm / 2;
    const barSteps = 8;
    const chordBars = sc.bpm < 70 ? 2 : 1;
    const inBar = step % barSteps;
    const chord = sc.prog[Math.floor(step / (barSteps * chordBars)) % sc.prog.length];
    const notes = chordNotes(sc.key, chord);

    if (step % (barSteps * chordBars) === 0) {
      // Pad: the whole chord, slow swell, two slightly detuned layers.
      const dur = eighth * barSteps * chordBars;
      for (const n of notes) {
        voice(midiToHz(n), t, dur, { type: "sine", gain: 0.035, attack: 1.4, release: 1.8 });
        voice(midiToHz(n), t, dur, { type: "triangle", gain: 0.018, attack: 1.8, release: 2.2, detune: 7 });
      }
    }
    if (sc.bass && inBar % 4 === 0) {
      voice(midiToHz(notes[0] - 12), t, eighth * 3, { type: "sine", gain: 0.06, attack: 0.03, release: 0.6 });
    }
    if (sc.pulse && inBar % 2 === 0) {
      voice(midiToHz(notes[0] - 12), t, eighth * 0.7, { type: "sawtooth", gain: 0.02, attack: 0.01, release: 0.15 });
    }
    if (sc.drone && step % (barSteps * 4) === 0) {
      voice(midiToHz(sc.key - 24), t, eighth * barSteps * 4, { type: "sine", gain: 0.03, attack: 3, release: 3 });
    }
    if (rand() < sc.keys * (inBar % 2 === 0 ? 0.55 : 0.25)) {
      const n = notes[Math.floor(rand() * notes.length)] + (rand() < 0.5 ? 12 : 0);
      voice(midiToHz(n), t, eighth * 1.5, { type: "triangle", gain: 0.03, attack: 0.008, release: 0.9 });
    }
    step++;
    return eighth;
  }

  function tick() {
    if (!playing) return;
    while (nextTime < ctx.currentTime + LOOKAHEAD_S) nextTime += scheduleStep(nextTime);
  }

  return {
    get playing() { return playing; },
    get scene() { return state.scene; },
    /** Call from a user gesture (iOS). */
    async start() {
      if (!AudioContextImpl && !context) return false;
      if (!ctx) build();
      if (ctx.state === "suspended") await ctx.resume();
      playing = true;
      nextTime = ctx.currentTime + 0.1;
      master.gain.setTargetAtTime(MASTER, ctx.currentTime, 1.2);
      if (!timer) timer = setInterval(tick, TICK_MS);
      tick();
      return true;
    },
    stop() {
      playing = false;
      if (timer) { clearInterval(timer); timer = null; }
      if (ctx) master.gain.setTargetAtTime(0, ctx.currentTime, 0.4);
    },
    setScene(next) {
      const changed = next.scene !== state.scene;
      state = { ...state, ...next };
      if (changed) step = 0; // start the new progression on its first chord
      applyState();
    },
    /** Lower the score while she speaks (level 0..1). */
    duck(speaking) {
      if (!ctx || !playing) return;
      master.gain.setTargetAtTime(speaking ? DUCKED : MASTER, ctx.currentTime, speaking ? 0.15 : 0.8);
    },
    /** Page hidden (iOS background): suspend the context, resume on return. */
    async setHidden(hidden) {
      if (!ctx) return;
      if (hidden) await ctx.suspend();
      else if (playing) { await ctx.resume(); nextTime = ctx.currentTime + 0.1; }
    },
    get context() { return ctx; },
  };
}
