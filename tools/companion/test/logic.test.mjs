// Unit tests for the companion stage's pure logic: `node --test tools/companion/test`
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  MOODS, moodToExpression, moodBrightness, SCENES, chordNotes, midiToHz,
  sceneFor, nextBlink, mouthOpen, reduceFrame, speakable, isFailureSentinel, abandonTurn, avatarCue,
} from "../../../flutter_app/web/companion/logic.js";

/**
 * The fifteen moods that came with her live heart rate (2026-10-19). The core
 * adds them to VALID_MOODS in a parallel change; until that lands they are
 * listed here so the stage covers them either way (the union is the same
 * set before and after).
 */
const NEW_MOODS = Object.freeze([
  "serene", "cozy", "wistful", "lonely", "stern", "smug", "hopeful", "sulking",
  "admiring", "teasing", "restless", "heartbroken", "giddy", "overwhelmed", "breathless",
]);

function backendMoods() {
  const src = readFileSync(new URL("../../../docker/core/app/fact_extractor.py", import.meta.url), "utf8");
  const block = src.match(/VALID_MOODS\s*=\s*frozenset\(\s*\{([\s\S]*?)\}\s*\)/);
  assert.ok(block, "backend VALID_MOODS literal must be found");
  return [...block[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
}

test("the mood list matches the backend's VALID_MOODS (plus the new fifteen) exactly", () => {
  const backend = backendMoods();
  for (const m of backend) assert.ok(MOODS.includes(m), `backend mood ${m} missing from MOODS`);
  assert.deepEqual(new Set(MOODS), new Set([...backend, ...NEW_MOODS]));
  assert.equal(MOODS.length, 65);
});

test("every mood maps to a known portrait frame", () => {
  assert.equal(new Set(MOODS).size, MOODS.length);
  for (const m of MOODS) assert.ok(["base", "smile", "blush", "annoyed"].includes(moodToExpression(m)), m);
  assert.equal(moodToExpression("playful"), "smile");
  assert.equal(moodToExpression("flustered"), "blush");
  assert.equal(moodToExpression("irritated"), "annoyed");
  assert.equal(moodToExpression("composed"), "base");
  assert.equal(moodToExpression("not-a-mood"), "base");
});

test("the new moods show the portrait frame that fits them", () => {
  const frames = {
    serene: "smile", cozy: "smile", smug: "smile", hopeful: "smile", admiring: "smile",
    teasing: "smile", giddy: "smile",
    breathless: "blush", overwhelmed: "blush",
    stern: "annoyed", sulking: "annoyed",
    wistful: "base", lonely: "base", restless: "base", heartbroken: "base",
  };
  assert.deepEqual(Object.keys(frames).sort(), [...NEW_MOODS].sort());
  for (const [mood, frame] of Object.entries(frames)) assert.equal(moodToExpression(mood), frame, mood);
});

test("brightness follows the mood's warmth", () => {
  assert.ok(moodBrightness("affectionate") > 0);
  assert.ok(moodBrightness("furious") < 0);
  assert.ok(moodBrightness("grieving") < moodBrightness("irritated"));
  assert.equal(moodBrightness("composed"), 0);
  // The new ones: warm brightens, hurt darkens, quiet longing stays neutral.
  assert.ok(moodBrightness("giddy") > 0);
  assert.ok(moodBrightness("cozy") > 0);
  assert.ok(moodBrightness("sulking") < 0);
  assert.ok(moodBrightness("heartbroken") < moodBrightness("sulking"));
  assert.ok(moodBrightness("lonely") < 0);
  assert.equal(moodBrightness("wistful"), 0);
});

test("a heartbeat frame changes nothing on the stage (the PWA shows it)", () => {
  const before = { line: "Report.", mood: "serene", thinking: false, streaming: false };
  const { state, effects } = reduceFrame(before, { type: "heartbeat", bpm: 96, mood: "giddy", intensity: 7 });
  assert.deepEqual(state, before);
  assert.deepEqual(effects, []);
});

test("every scene's chords resolve and stay in a sane register", () => {
  for (const [name, sc] of Object.entries(SCENES)) {
    assert.ok(sc.bpm >= 50 && sc.bpm <= 120, name);
    for (const ch of sc.prog) {
      const notes = chordNotes(sc.key, ch);
      assert.ok(notes.length >= 3, `${name} ${ch}`);
      for (const n of notes) assert.ok(n >= 36 && n <= 84, `${name} ${n}`);
    }
  }
  assert.throws(() => chordNotes(60, ["nope", 0]));
  assert.equal(Math.round(midiToHz(69)), 440);
  assert.equal(Math.round(midiToHz(57)), 220);
});

test("scene selection reads Her Day, the hour, the weather and her mood", () => {
  assert.equal(sceneFor({ status: { location: "Hangar" }, hour: 14 }).scene, "hangar");
  assert.equal(sceneFor({ status: { location: "Lounge" }, hour: 13 }).scene, "lounge");
  assert.equal(sceneFor({ status: { location: "Mess hall" }, hour: 13 }).scene, "lounge");
  assert.equal(sceneFor({ status: { location: "Quarters" }, hour: 15 }).scene, "night");
  assert.equal(sceneFor({ status: { location: "Command deck" }, hour: 23 }).scene, "night");
  assert.equal(sceneFor({ status: { location: "Command deck" }, hour: 10 }).scene, "deck");
  assert.equal(sceneFor({ status: { location: "Hangar", override: "mission" } }).scene, "mission");
  assert.equal(sceneFor({ status: { override: "gaming" } }).scene, "arcade");
  assert.equal(sceneFor().scene, "deck");
  const wet = sceneFor({ weather: { condition: "drizzle" } });
  assert.equal(wet.rain, true);
  assert.equal(wet.snow, false);
  assert.equal(sceneFor({ weather: { condition: "snow" } }).snow, true);
  assert.ok(sceneFor({ mood: "tender" }).brightness > 0);
});

test("blinks are irregular but bounded; doubles happen", () => {
  let i = 0;
  const seq = [0.5, 0.0, 0.99, 0.1];
  const rand = () => seq[i++ % seq.length];
  const a = nextBlink(rand);
  assert.ok(a.delay >= 2400 && a.delay <= 6500);
  assert.equal(a.closedMs, 110);
  const doubles = Array.from({ length: 200 }, () => nextBlink().double).filter(Boolean).length;
  assert.ok(doubles > 5 && doubles < 80);
});

test("the mouth flaps only with sound, faster when louder", () => {
  assert.equal(mouthOpen(0, 100), false);
  assert.equal(mouthOpen(0.02, 100), false);
  const flips = (lvl) => {
    let n = 0; let prev = mouthOpen(lvl, 0);
    for (let t = 5; t < 2000; t += 5) { const o = mouthOpen(lvl, t); if (o !== prev) n++; prev = o; }
    return n;
  };
  assert.ok(flips(0.3) > 10);
  assert.ok(flips(0.9) > flips(0.3));
});

test("frames reduce into the stage and request side effects", () => {
  // As ws_manager.send_thinking sends it: a status line in `text`.
  let { state, effects } = reduceFrame({ line: "", mood: "composed" }, { type: "thinking", text: "Composing response..." });
  assert.equal(state.thinking, true);
  ({ state, effects } = reduceFrame(state, { type: "token", text: "Com" }));
  ({ state, effects } = reduceFrame(state, { type: "token", text: "mander." }));
  assert.equal(state.line, "Commander.");
  assert.equal(state.thinking, false);
  assert.deepEqual(effects, []);
  ({ state, effects } = reduceFrame(state, { type: "done", message_id: "x" }));
  assert.equal(state.streaming, false);
  assert.deepEqual(effects, [{ kind: "speak", text: "Commander." }]);
  // The server's final text wins over the streamed tokens.
  ({ state, effects } = reduceFrame({ line: "Comm", streaming: true }, { type: "done", text: "Commander!" }));
  assert.equal(state.line, "Commander!");
  // An empty reply doesn't speak.
  ({ effects } = reduceFrame({ line: "" }, { type: "done", text: "  " }));
  assert.deepEqual(effects, []);
  // The offline notice shows on the stage but is never voiced.
  const offline = "Communications disrupted, Commander. Standby for reconnection.";
  ({ state, effects } = reduceFrame({ line: "" }, { type: "done", text: offline }));
  assert.equal(state.line, offline);
  assert.deepEqual(effects, []);
  ({ state } = reduceFrame(state, { type: "mood", mood: "flustered" }));
  assert.equal(state.mood, "flustered");
  ({ state } = reduceFrame(state, { type: "mood" }));
  assert.equal(state.mood, "flustered");
  assert.deepEqual(reduceFrame(state, { type: "outfit", outfit: {} }).effects.map((e) => e.kind),
    ["refresh-portrait", "refresh-day"]);
  assert.deepEqual(reduceFrame(state, { type: "proactive", message: "Report." }).effects,
    [{ kind: "speak", text: "Report." }]);
  assert.deepEqual(reduceFrame(state, { type: "proactive" }).effects, []);
  // The server's voice_audio clip (its TTS for PWA voice input) is not a stage
  // channel: every reply is already spoken once from its `done` text, so the
  // clip used to make her say voice-input replies twice.
  assert.deepEqual(reduceFrame(state, { type: "voice_audio", audio: "QUJD" }).effects, []);
  assert.deepEqual(reduceFrame(state, { type: "voice_audio" }).effects, []);
  const err = reduceFrame({ thinking: true, streaming: true }, { type: "error" }).state;
  assert.equal(err.thinking || err.streaming, false);
  assert.deepEqual(reduceFrame(state, { type: "affection" }).state, state);
  assert.deepEqual(reduceFrame(state, null).effects, []);
});

test("thinking follows the server's status text and clears on its empty frame, proactive and error", () => {
  let { state } = reduceFrame({ thinking: false }, { type: "thinking", text: "Composing response..." });
  assert.equal(state.thinking, true);
  // The backend clears its bubble with {"type":"thinking","content":""}.
  ({ state } = reduceFrame(state, { type: "thinking", content: "" }));
  assert.equal(state.thinking, false);
  ({ state } = reduceFrame(state, { type: "thinking", content: "Warming up image systems" }));
  assert.equal(state.thinking, true);
  ({ state } = reduceFrame(state, { type: "thinking", text: "" }));
  assert.equal(state.thinking, false);
  ({ state } = reduceFrame(state, { type: "thinking" }));
  assert.equal(state.thinking, false, "a thinking frame with no text clears, it doesn't hang");
  // A proactive line is her speaking: she is no longer thinking.
  ({ state } = reduceFrame({ thinking: true }, { type: "proactive", message: "Report." }));
  assert.equal(state.thinking, false);
  ({ state } = reduceFrame({ thinking: true }, { type: "proactive" }));
  assert.equal(state.thinking, false);
  ({ state } = reduceFrame({ thinking: true, streaming: true }, { type: "error", message: "STT failed" }));
  assert.equal(state.thinking, false);
  assert.equal(state.streaming, false);
  // The image a status line announced arrived: the pulse stops.
  ({ state } = reduceFrame({ thinking: true }, { type: "image", url: "/x.webp" }));
  assert.equal(state.thinking, false);
});

test("an error frame's message replaces the blank dock and is never spoken", () => {
  const msg = "Something glitched on my end, Commander. Your message is safe — try again.";
  let { state, effects } = reduceFrame({ line: "", thinking: true }, { type: "error", message: msg });
  assert.equal(state.line, msg);
  assert.deepEqual(effects, []);
  // No message (or a blank one): the last line stays as it was.
  ({ state, effects } = reduceFrame({ line: "Earlier.", thinking: true }, { type: "error" }));
  assert.equal(state.line, "Earlier.");
  assert.deepEqual(effects, []);
  ({ state } = reduceFrame({ line: "Earlier." }, { type: "error", message: "   " }));
  assert.equal(state.line, "Earlier.");
});

test("a dropped link abandons the turn: no pulse, and the next reply starts a fresh line", () => {
  let { state } = reduceFrame({ line: "", thinking: true }, { type: "token", text: "I was say" });
  state = abandonTurn({ ...state, thinking: true });
  assert.equal(state.thinking, false);
  assert.equal(state.streaming, false);
  assert.equal(state.line, "I was say", "the partial line stays readable");
  ({ state } = reduceFrame(state, { type: "token", text: "Understood." }));
  assert.equal(state.line, "Understood.");
});

test("the 3D avatar holds the thinking pose only while she thinks", () => {
  assert.equal(avatarCue({ thinking: false }, { thinking: true }), "think");
  assert.equal(avatarCue({ thinking: true }, { thinking: true }), "think");
  // Her reply started (or the turn ended): leave the pose even if no mood frame follows.
  assert.equal(avatarCue({ thinking: true }, { thinking: false }), "release");
  assert.equal(avatarCue({ thinking: false }, { thinking: false }), null);
  const prev = { thinking: true, mood: "composed" };
  const { state } = reduceFrame(prev, { type: "token", text: "Commander." });
  assert.equal(avatarCue(prev, state), "release");
});

test("the failure sentinel is shown but never spoken", () => {
  const SENTINEL = "Communications disrupted, Commander. Standby for reconnection.";
  assert.equal(isFailureSentinel(SENTINEL), true);
  assert.equal(isFailureSentinel("  Communications disrupted."), true);
  assert.equal(isFailureSentinel("Communications are fine, Commander."), false);
  assert.equal(isFailureSentinel(null), false);
  // Streamed as her reply while the GPU host is off.
  let { state, effects } = reduceFrame({ line: "", thinking: true }, { type: "token", text: SENTINEL });
  ({ state, effects } = reduceFrame(state, { type: "done", message_id: "m1" }));
  assert.equal(state.line, SENTINEL, "the subtitle still shows it");
  assert.equal(state.thinking, false);
  assert.deepEqual(effects, [], "no speak effect for the sentinel");
  // Also when the server sends the final text explicitly, or as a proactive line.
  assert.deepEqual(reduceFrame({ line: "" }, { type: "done", text: SENTINEL }).effects, []);
  assert.deepEqual(reduceFrame({ line: "" }, { type: "proactive", message: SENTINEL }).effects, []);
  // A real reply still speaks.
  assert.deepEqual(reduceFrame({ line: "" }, { type: "done", text: "Standing by, Commander." }).effects,
    [{ kind: "speak", text: "Standing by, Commander." }]);
});

test("stage directions never reach her voice", () => {
  assert.equal(speakable("(I look away) ...Fine. *sighs* Go."), "...Fine. Go.");
  assert.equal(speakable(null), "");
});
