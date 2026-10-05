// Pure logic for the companion stage — no DOM, no audio, no network, so it
// runs under `node --test` (tools/companion/test) and in the browser alike.

/** Her moods — kept identical to docker/core/app/fact_extractor.py VALID_MOODS (a test diffs them). */
export const MOODS = Object.freeze([
  "composed", "focused", "prideful", "exasperated", "protective",
  "quietly_pleased", "competitive", "tender", "longing", "battle_ready",
  "flustered", "affectionate", "shy", "yearning", "devoted",
  "passionate", "jealous", "possessive", "smitten", "infatuated",
  "vigilant", "calculating", "hunting", "adrenaline",
  "scared", "terrified", "panicked", "desperate", "relieved",
  "content", "playful", "drowsy", "amused", "bored", "excited",
  "melancholic", "haunted", "conflicted", "guilty", "determined",
  "grieving", "furious",
  "nostalgic", "curious", "irritated", "defiant", "vulnerable",
  "grateful", "worried", "embarrassed",
]);

const SMILE = new Set([
  "quietly_pleased", "content", "playful", "amused", "affectionate", "devoted",
  "smitten", "infatuated", "excited", "relieved", "grateful", "prideful",
]);
const BLUSH = new Set([
  "flustered", "shy", "embarrassed", "tender", "yearning", "passionate", "vulnerable",
]);
const ANNOYED = new Set([
  "exasperated", "irritated", "furious", "jealous", "possessive", "defiant",
  "bored", "competitive",
]);

/** Which portrait expression frame a mood shows: base | smile | blush | annoyed. */
export function moodToExpression(mood) {
  if (SMILE.has(mood)) return "smile";
  if (BLUSH.has(mood)) return "blush";
  if (ANNOYED.has(mood)) return "annoyed";
  return "base";
}

/** Warm moods brighten the music; tense ones darken it. -1..1 */
export function moodBrightness(mood) {
  if (SMILE.has(mood) || BLUSH.has(mood)) return 0.6;
  if (ANNOYED.has(mood)) return -0.4;
  if (["scared", "terrified", "panicked", "desperate", "haunted", "grieving", "melancholic", "worried"].includes(mood)) {
    return -0.7;
  }
  return 0;
}

// ── Music scenes ────────────────────────────────────────────────────────────

/** Scene parameters: tempo (bpm), progression (MIDI chord roots + quality), voices. */
export const SCENES = Object.freeze({
  deck:    { bpm: 66,  key: 57, prog: [["m9", 0], ["maj7", -4], ["maj7", 3], ["6", -2]], keys: 0.45, bass: true },
  lounge:  { bpm: 72,  key: 50, prog: [["m9", 0], ["13", 5], ["maj9", -2], ["7b9", 7]], keys: 0.7, bass: true },
  hangar:  { bpm: 80,  key: 52, prog: [["m7", 0], ["maj", -2], ["maj", -4], ["m7", 0]], keys: 0.3, bass: true, drone: true },
  night:   { bpm: 58,  key: 52, prog: [["madd9", 0], ["maj7", -4], ["maj", 3], ["maj", -2]], keys: 0.35, bass: false },
  mission: { bpm: 96,  key: 45, prog: [["5", 0], ["5", 0], ["5", -2], ["5", -4]], keys: 0.2, bass: true, pulse: true },
  arcade:  { bpm: 110, key: 60, prog: [["maj7#11", 0], ["maj7", 5], ["maj7#11", 0], ["6", 7]], keys: 0.8, bass: true },
});

const QUALITIES = Object.freeze({
  maj: [0, 4, 7], m7: [0, 3, 7, 10], maj7: [0, 4, 7, 11], m9: [0, 3, 7, 10, 14],
  maj9: [0, 4, 7, 11, 14], madd9: [0, 3, 7, 14], 6: [0, 4, 7, 9], 13: [0, 4, 10, 14, 21],
  "7b9": [0, 4, 7, 10, 13], 5: [0, 7, 12], "maj7#11": [0, 4, 7, 11, 18],
});

/** MIDI notes for a chord: [quality, offset] relative to a key root. */
export function chordNotes(key, [quality, offset]) {
  const shape = QUALITIES[quality];
  if (!shape) throw new Error(`unknown chord quality: ${quality}`);
  return shape.map((iv) => key + offset + iv);
}

export function midiToHz(midi) {
  return 440 * Math.pow(2, (midi - 69) / 12);
}

/**
 * Pick the music scene from Her Day + time + weather + mood.
 * status: {location, activity, override} from /api/her-day; weather: {condition}.
 */
export function sceneFor({ status = {}, hour = 12, weather = null, mood = "composed" } = {}) {
  const loc = String(status.location || "").toLowerCase();
  let scene = "deck";
  if (status.override === "mission") scene = "mission";
  else if (status.override === "gaming") scene = "arcade";
  else if (loc.includes("hangar")) scene = "hangar";
  else if (loc.includes("lounge") || loc.includes("mess")) scene = "lounge";
  else if (loc.includes("quarters") || hour >= 22 || hour < 5) scene = "night";
  const cond = weather && weather.condition;
  return {
    scene,
    rain: ["rain", "drizzle", "storm"].includes(cond),
    snow: cond === "snow",
    brightness: moodBrightness(mood),
  };
}

// ── Live portrait timing ────────────────────────────────────────────────────

/** Next blink delay in ms (2.4–6.5 s), with an occasional quick double blink. */
export function nextBlink(rand = Math.random) {
  const double = rand() < 0.18;
  return { delay: 2400 + Math.floor(rand() * 4100), double, closedMs: 110 };
}

/**
 * Talking mouth: given an audio level 0..1 and a time (ms), is the mouth open?
 * Flaps at ~9 Hz scaled by loudness; silence keeps it closed.
 */
export function mouthOpen(level, tMs) {
  if (!(level > 0.06)) return false;
  const rate = 7 + Math.min(level, 1) * 5; // 7–12 Hz
  return Math.sin((tMs / 1000) * rate * Math.PI * 2) > 0.15 - level * 0.4;
}

// ── WebSocket frames → stage actions ────────────────────────────────────────

/**
 * Reduce a server frame into the stage state. Returns {state, effects}; effects
 * are side-effect requests the app performs (speak, refetch, ...).
 */
export function reduceFrame(state, frame) {
  const s = { ...state };
  const effects = [];
  switch (frame && frame.type) {
    case "token":
      if (!s.streaming) { s.streaming = true; s.line = ""; }
      s.line += frame.text || "";
      s.thinking = false;
      break;
    case "done": {
      const text = typeof frame.text === "string" ? frame.text : s.line;
      s.streaming = false;
      s.thinking = false;
      s.line = text;
      if (text && text.trim()) effects.push({ kind: "speak", text });
      break;
    }
    case "thinking":
      s.thinking = true;
      break;
    case "mood":
      if (frame.mood) s.mood = frame.mood;
      break;
    case "outfit":
      effects.push({ kind: "refresh-portrait" }, { kind: "refresh-day" });
      break;
    case "proactive":
      if (frame.message) {
        s.line = frame.message;
        effects.push({ kind: "speak", text: frame.message });
      }
      break;
    case "voice_audio":
      if (frame.audio) effects.push({ kind: "play-audio", audio: frame.audio });
      break;
    case "error":
      s.thinking = false;
      s.streaming = false;
      break;
    default:
      break;
  }
  return { state: s, effects };
}

/** Strip her stage directions "(I look away)" for subtitles-as-speech checks. */
export function speakable(text) {
  return String(text || "").replace(/\([^)]*\)/g, " ").replace(/\*[^*]*\*/g, " ").replace(/\s+/g, " ").trim();
}
