// Unit tests for the companion stage's pure logic: `node --test tools/companion/test`
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  MOODS, moodToExpression, moodBrightness, SCENES, chordNotes, midiToHz,
  sceneFor, nextBlink, mouthOpen, reduceFrame, speakable,
} from "../../../flutter_app/web/companion/logic.js";

test("the mood list matches the backend's VALID_MOODS exactly", () => {
  const src = readFileSync(new URL("../../../docker/core/app/fact_extractor.py", import.meta.url), "utf8");
  const block = src.slice(src.indexOf("VALID_MOODS = frozenset({"), src.indexOf("})", src.indexOf("VALID_MOODS")));
  const backend = new Set([...block.matchAll(/"([a-z_]+)"/g)].map((m) => m[1]));
  assert.deepEqual(new Set(MOODS), backend);
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

test("brightness follows the mood's warmth", () => {
  assert.ok(moodBrightness("affectionate") > 0);
  assert.ok(moodBrightness("furious") < 0);
  assert.ok(moodBrightness("grieving") < moodBrightness("irritated"));
  assert.equal(moodBrightness("composed"), 0);
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
  let { state, effects } = reduceFrame({ line: "", mood: "composed" }, { type: "thinking" });
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
  ({ state } = reduceFrame(state, { type: "mood", mood: "flustered" }));
  assert.equal(state.mood, "flustered");
  ({ state } = reduceFrame(state, { type: "mood" }));
  assert.equal(state.mood, "flustered");
  assert.deepEqual(reduceFrame(state, { type: "outfit", outfit: {} }).effects.map((e) => e.kind),
    ["refresh-portrait", "refresh-day"]);
  assert.deepEqual(reduceFrame(state, { type: "proactive", message: "Report." }).effects,
    [{ kind: "speak", text: "Report." }]);
  assert.deepEqual(reduceFrame(state, { type: "proactive" }).effects, []);
  assert.deepEqual(reduceFrame(state, { type: "voice_audio", audio: "QUJD" }).effects,
    [{ kind: "play-audio", audio: "QUJD" }]);
  assert.deepEqual(reduceFrame(state, { type: "voice_audio" }).effects, []);
  const err = reduceFrame({ thinking: true, streaming: true }, { type: "error" }).state;
  assert.equal(err.thinking || err.streaming, false);
  assert.deepEqual(reduceFrame(state, { type: "affection" }).state, state);
  assert.deepEqual(reduceFrame(state, null).effects, []);
});

test("stage directions never reach her voice", () => {
  assert.equal(speakable("(I look away) ...Fine. *sighs* Go."), "...Fine. Go.");
  assert.equal(speakable(null), "");
});
