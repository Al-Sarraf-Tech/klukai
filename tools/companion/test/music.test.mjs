import { test } from "node:test";
import assert from "node:assert/strict";
import { createMusic } from "../../../flutter_app/web/companion/music.js";
import { FakeAudioContext } from "./fake-audio.mjs";

test("no Web Audio → start() is a no-op, not a crash", async () => {
  const m = createMusic({ AudioContextImpl: undefined });
  assert.equal(await m.start(), false);
  m.stop(); m.duck(true); await m.setHidden(true); // all safe before start
  assert.equal(m.playing, false);
});

test("start builds the graph inside the gesture and schedules notes", async (t) => {
  const m = createMusic({ AudioContextImpl: FakeAudioContext, rand: () => 0 });
  t.after(() => m.stop());
  assert.equal(await m.start(), true);
  const ctx = FakeAudioContext.last;
  assert.equal(ctx.state, "running");
  assert.equal(m.playing, true);
  assert.equal(ctx.of("convolver").length, 1);
  const oscs = ctx.of("osc");
  assert.ok(oscs.length > 0, "the first tick schedules the opening chord");
  for (const o of oscs) assert.ok(o.stopAt > o.startAt);
  m.stop();
  assert.equal(m.playing, false);
});

test("scenes switch progression, weather fades the noise bed, mood moves the filter", async (t) => {
  const m = createMusic({ AudioContextImpl: FakeAudioContext, rand: () => 0.99 });
  t.after(() => m.stop()); // never leak the scheduler interval, even on failure
  await m.start();
  const ctx = FakeAudioContext.last;
  const [filter] = ctx.of("filter").filter((f) => f.frequency.events.length);
  m.setScene({ scene: "night", rain: true, brightness: 0.6 });
  assert.equal(m.scene, "night");
  const last = filter.frequency.events.at(-1);
  assert.ok(last[1] > 2000, "warm mood opens the filter");
  const rainBed = ctx.of("gain").find((g) => g.gain.events.some((e) => e[0] === "target" && e[1] === 0.05));
  assert.ok(rainBed, "rain raises the noise bed");
  m.setScene({ scene: "night", rain: false, snow: true, brightness: -0.7 });
  assert.ok(filter.frequency.events.at(-1)[1] < 1100, "tense mood darkens it");
  // Unknown scene names fall back to the deck progression rather than throwing.
  m.setScene({ scene: "nowhere" });
  ctx.currentTime += 5;
  await new Promise((r) => setTimeout(r, 80));
  m.stop();
});

test("ducking under her voice, and iOS background suspend/resume", async (t) => {
  const m = createMusic({ AudioContextImpl: FakeAudioContext });
  t.after(() => m.stop());
  await m.start();
  const ctx = FakeAudioContext.last;
  m.duck(true);
  const master = ctx.of("gain")[0];
  assert.equal(master.gain.events.at(-1)[1], 0.06);
  m.duck(false);
  assert.equal(master.gain.events.at(-1)[1], 0.16);
  await m.setHidden(true);
  assert.equal(ctx.state, "suspended");
  await m.setHidden(false);
  assert.equal(ctx.state, "running");
  m.stop();
  m.duck(true); // ignored when stopped
  assert.equal(master.gain.events.at(-1)[1], 0);
  await m.setHidden(false); // stays suspended-safe when not playing
});
