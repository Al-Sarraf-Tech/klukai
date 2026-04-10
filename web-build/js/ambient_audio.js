/**
 * Klukai Ambient Audio — procedural mood-reactive soundscapes.
 * Uses Web Audio API oscillators + filters, no external files needed.
 */
const ambientAudio = (() => {
  let ctx = null;
  let currentNodes = [];
  let currentMood = null;
  let muted = true; // Muted by default — respect user preference
  let masterGain = null;

  function init() {
    if (ctx) return;
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    masterGain = ctx.createGain();
    masterGain.gain.value = 0;
    masterGain.connect(ctx.destination);
  }

  function stopAll() {
    currentNodes.forEach(node => {
      try {
        if (node.stop) node.stop();
        if (node.disconnect) node.disconnect();
      } catch (e) {}
    });
    currentNodes = [];
  }

  function createDrone(freq, type, gain, filterFreq) {
    const osc = ctx.createOscillator();
    osc.type = type || 'sine';
    osc.frequency.value = freq;

    const gainNode = ctx.createGain();
    gainNode.gain.value = gain || 0.03;

    const filter = ctx.createBiquadFilter();
    filter.type = 'lowpass';
    filter.frequency.value = filterFreq || 800;
    filter.Q.value = 1;

    osc.connect(filter);
    filter.connect(gainNode);
    gainNode.connect(masterGain);
    osc.start();

    currentNodes.push(osc, gainNode, filter);
    return { osc, gainNode, filter };
  }

  // Mood → soundscape mapping
  const MOOD_SOUNDS = {
    // Tender / romantic
    tender: () => {
      createDrone(220, 'sine', 0.025, 600);
      createDrone(330, 'sine', 0.015, 400);
      createDrone(277.18, 'sine', 0.01, 500);
    },
    affectionate: () => { MOOD_SOUNDS.tender(); },
    devoted: () => { MOOD_SOUNDS.tender(); },
    shy: () => {
      createDrone(196, 'sine', 0.02, 500);
      createDrone(293.66, 'sine', 0.01, 350);
    },
    flustered: () => { MOOD_SOUNDS.shy(); },
    vulnerable: () => { MOOD_SOUNDS.shy(); },

    // Calm / peaceful
    composed: () => {
      createDrone(110, 'sine', 0.02, 400);
      createDrone(165, 'sine', 0.01, 300);
    },
    content: () => { MOOD_SOUNDS.composed(); },
    quietly_pleased: () => { MOOD_SOUNDS.composed(); },
    relieved: () => { MOOD_SOUNDS.composed(); },

    // Sleepy / night
    drowsy: () => {
      createDrone(82.41, 'sine', 0.025, 250);
      createDrone(123.47, 'sine', 0.015, 200);
    },

    // Alert / tactical
    focused: () => {
      createDrone(146.83, 'sawtooth', 0.008, 300);
      createDrone(110, 'triangle', 0.015, 500);
    },
    vigilant: () => { MOOD_SOUNDS.focused(); },
    calculating: () => { MOOD_SOUNDS.focused(); },

    // Combat / tense
    battle_ready: () => {
      createDrone(73.42, 'sawtooth', 0.015, 600);
      createDrone(92.5, 'square', 0.005, 200);
      createDrone(146.83, 'sawtooth', 0.01, 400);
    },
    adrenaline: () => { MOOD_SOUNDS.battle_ready(); },
    hunting: () => { MOOD_SOUNDS.battle_ready(); },

    // Dark / melancholic
    melancholic: () => {
      createDrone(130.81, 'sine', 0.02, 350);
      createDrone(155.56, 'sine', 0.015, 280);
      createDrone(196, 'triangle', 0.008, 200);
    },
    haunted: () => { MOOD_SOUNDS.melancholic(); },
    grieving: () => { MOOD_SOUNDS.melancholic(); },
    guilty: () => { MOOD_SOUNDS.melancholic(); },
    nostalgic: () => { MOOD_SOUNDS.melancholic(); },

    // Playful
    playful: () => {
      createDrone(261.63, 'sine', 0.015, 800);
      createDrone(329.63, 'sine', 0.01, 600);
    },
    amused: () => { MOOD_SOUNDS.playful(); },
    excited: () => { MOOD_SOUNDS.playful(); },
    curious: () => { MOOD_SOUNDS.playful(); },

    // Intense / passionate
    passionate: () => {
      createDrone(164.81, 'sine', 0.025, 700);
      createDrone(246.94, 'sine', 0.02, 500);
      createDrone(196, 'triangle', 0.01, 400);
    },
    yearning: () => { MOOD_SOUNDS.passionate(); },
    longing: () => { MOOD_SOUNDS.passionate(); },
    smitten: () => { MOOD_SOUNDS.passionate(); },
  };

  function setMood(mood) {
    if (mood === currentMood) return;
    currentMood = mood;

    if (muted || !ctx) return;

    // Fade out current
    if (masterGain) {
      masterGain.gain.linearRampToValueAtTime(0, ctx.currentTime + 0.5);
    }

    setTimeout(() => {
      stopAll();
      const soundFn = MOOD_SOUNDS[mood];
      if (soundFn) {
        soundFn();
        if (masterGain) {
          masterGain.gain.linearRampToValueAtTime(0.6, ctx.currentTime + 0.5);
        }
      }
    }, 500);
  }

  function toggleMute() {
    init();
    muted = !muted;
    if (muted) {
      if (masterGain) {
        masterGain.gain.linearRampToValueAtTime(0, ctx.currentTime + 0.3);
      }
      setTimeout(stopAll, 300);
    } else {
      // Force re-trigger with current mood
      const mood = currentMood;
      currentMood = null;
      setMood(mood);
    }
    return !muted; // Return whether audio is now ON
  }

  function isMuted() { return muted; }

  return { setMood, toggleMute, isMuted, init };
})();
