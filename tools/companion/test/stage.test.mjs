import { test } from "node:test";
import assert from "node:assert/strict";
import { createPortrait, FALLBACK_PORTRAIT } from "../../../flutter_app/web/companion/portrait.js";
import { createAmbience, ambienceKind, particleCount } from "../../../flutter_app/web/companion/ambience.js";

function fakeEl() {
  const attrs = {}; const classes = new Set();
  return {
    attrs, classes,
    getAttribute: (k) => attrs[k] ?? null, setAttribute: (k, v) => { attrs[k] = v; },
    classList: { toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)), contains: (c) => classes.has(c) },
  };
}
function fakeRoot() {
  const els = { ".frame-base": fakeEl(), ".frame-expr": fakeEl(), ".frame-blink": fakeEl(), ".frame-talk": fakeEl() };
  const root = fakeEl();
  root.querySelector = (sel) => els[sel];
  return { root, els };
}
function clock() {
  const timers = []; const frames = [];
  return {
    timers, frames,
    setTimer: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimer: () => {},
    raf: (fn) => { frames.push(fn); return frames.length; },
    caf: () => {},
    runTimer: () => timers.shift()?.fn(),
  };
}

const FRAMES = { base: "/f/base", blink: "/f/blink", talk: "/f/talk", smile: "/f/smile", blush: null, annoyed: "/f/annoyed" };

test("frames load; missing base falls back to her official portrait and marks pending", () => {
  const { root, els } = fakeRoot();
  const p = createPortrait(root, clock());
  p.setFrames({});
  assert.equal(els[".frame-base"].getAttribute("src"), FALLBACK_PORTRAIT);
  assert.ok(root.classes.has("pending"));
  p.setFrames(FRAMES);
  assert.equal(els[".frame-base"].getAttribute("src"), "/f/base");
  assert.equal(els[".frame-talk"].getAttribute("src"), "/f/talk");
  assert.ok(els[".frame-base"].classes.has("on"));
  assert.ok(!root.classes.has("pending"));
  p.setFrames(FRAMES, { pending: true });
  assert.ok(root.classes.has("pending"));
});

test("mood expressions crossfade in, and fall back to base when a frame is missing", () => {
  const { root, els } = fakeRoot();
  const p = createPortrait(root, clock());
  p.setFrames(FRAMES);
  p.setExpression("smile");
  assert.equal(els[".frame-expr"].getAttribute("src"), "/f/smile");
  assert.ok(els[".frame-expr"].classes.has("on"));
  p.setExpression("blush"); // no blush frame yet
  assert.ok(!els[".frame-expr"].classes.has("on"));
  p.setExpression(null);
  assert.equal(p.state.expression, "base");
});

test("she blinks on her own while active, and not while talking", () => {
  const { root, els } = fakeRoot();
  const c = clock();
  const p = createPortrait(root, { ...c, rand: () => 0.99 }); // no double blinks
  p.setFrames(FRAMES);
  p.setActive(true);
  assert.equal(c.timers.length, 1);
  assert.ok(c.timers[0].ms >= 2400);
  c.runTimer(); // blink fires
  assert.ok(els[".frame-blink"].classes.has("on"));
  c.runTimer(); // eyes reopen
  assert.ok(!els[".frame-blink"].classes.has("on"));
  // Mid-word: the blink is skipped.
  p.setLevel(0.8);
  const next = c.timers.find((t) => t.ms >= 2400);
  next.fn();
  assert.ok(!els[".frame-blink"].classes.has("on"));
  p.setActive(false);
  assert.equal(p.state.active, false);
});

test("double blinks close twice", () => {
  const { root, els } = fakeRoot();
  const c = clock();
  let i = 0; const seq = [0.1, 0.5]; // double=true, then the delay
  const p = createPortrait(root, { ...c, rand: () => seq[i++ % 2] });
  p.setFrames(FRAMES);
  p.setActive(true);
  c.runTimer(); c.runTimer(); // first blink + reopen → schedules the second
  const second = c.timers.find((t) => t.ms === 140);
  assert.ok(second);
  second.fn();
  assert.ok(els[".frame-blink"].classes.has("on"));
});

test("the mouth flaps with her voice and closes when she stops", () => {
  const { root, els } = fakeRoot();
  const c = clock();
  let t = 0;
  const p = createPortrait(root, { ...c, now: () => t });
  p.setFrames(FRAMES);
  p.setActive(true);
  p.setLevel(0.9);
  assert.equal(c.frames.length, 1);
  let opened = false;
  for (let k = 0; k < 40; k++) { t += 16; c.frames.shift()(); opened ||= els[".frame-talk"].classes.has("on"); }
  assert.ok(opened);
  p.setLevel(0);
  c.frames.shift()?.();
  assert.ok(!els[".frame-talk"].classes.has("on"));
  p.setLevel(0.9); p.setActive(false);
  assert.ok(!els[".frame-talk"].classes.has("on"));
});

test("ambience follows weather and place, scales with the screen, and respects reduced motion", () => {
  assert.equal(ambienceKind({ scene: "deck", rain: true }), "rain");
  assert.equal(ambienceKind({ scene: "deck", snow: true }), "snow");
  assert.equal(ambienceKind({ scene: "hangar" }), "dust");
  assert.equal(ambienceKind({ scene: "night" }), "stars");
  assert.equal(ambienceKind({ scene: "lounge" }), "bokeh");
  assert.equal(ambienceKind({ scene: "mission" }), "dust");
  assert.equal(particleCount("rain", 390, 844), 90);
  assert.equal(particleCount("rain", 3840, 2160), 270, "capped on huge screens");
  assert.equal(particleCount("nope", 390, 844), 0);

  const calls = [];
  const ctx = new Proxy({}, { get: (_, k) => (k === "setTransform" || k === "clearRect" || k === "beginPath" || k === "moveTo"
    || k === "lineTo" || k === "stroke" || k === "arc" || k === "fill") ? (...a) => calls.push(k) : undefined, set: () => true });
  const canvas = { clientWidth: 390, clientHeight: 844, getContext: () => ctx };
  const frames = [];
  for (const kind of ["rain", "snow", "dust", "stars", "bokeh"]) {
    const a = createAmbience(canvas, { raf: (fn) => frames.push(fn), caf() {}, rand: () => 0.5, reducedMotion: () => false, dpr: () => 1 });
    a.setKind(kind);
    a.start(); a.start();
    assert.equal(a.kind, kind);
    assert.ok(a.count > 0);
    for (let i = 0; i < 3; i++) frames.shift()();
    a.stop();
    frames.shift()?.(); // a stopped loop draws nothing more
  }
  assert.ok(calls.includes("stroke") && calls.includes("fill"));
  const still = createAmbience(canvas, { raf: () => { throw new Error("must not animate"); }, reducedMotion: () => true, dpr: () => 2 });
  still.start();
  assert.equal(canvas.width, 780);
  still.setKind("dust"); // same kind is a no-op
});
