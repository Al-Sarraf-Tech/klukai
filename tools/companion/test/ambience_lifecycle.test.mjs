import test from 'node:test';
import assert from 'node:assert/strict';
import { createAmbience } from '../../../flutter_app/web/companion/ambience.js';

function harness(reducedMotion = true) {
  let painted = false;
  let clears = 0;
  let fills = 0;
  let strokes = 0;
  let nextFrame = 1;
  const frames = new Map();
  const ctx = {
    setTransform() {},
    clearRect() { painted = false; clears++; },
    beginPath() {}, arc() {}, moveTo() {}, lineTo() {},
    fill() { painted = true; fills++; },
    stroke() { painted = true; strokes++; },
  };
  const canvas = {
    clientWidth: 390, clientHeight: 844,
    getContext: () => ctx,
    set width(value) { painted = false; },
    set height(value) { painted = false; },
  };
  const ambience = createAmbience(canvas, {
    reducedMotion: () => reducedMotion, dpr: () => 1, rand: () => 0.5,
    raf(fn) { const id = nextFrame++; frames.set(id, fn); return id; },
    caf(id) { frames.delete(id); },
  });
  return { ambience, canvas, frames, state: () => ({ painted, clears, fills, strokes }) };
}

test('reduced-motion still frame survives orientation resize without animation', () => {
  const h = harness();
  h.ambience.start();
  assert.equal(h.state().painted, true);
  h.canvas.clientWidth = 844;
  h.canvas.clientHeight = 390;
  h.ambience.resize();
  assert.equal(h.state().painted, true);
  assert.equal(h.state().clears, 2);
  assert.equal(h.frames.size, 0);
});

test('reduced-motion scene change replaces the static weather frame', () => {
  const h = harness();
  h.ambience.start();
  assert.ok(h.state().fills > 0);
  h.ambience.setKind('rain');
  assert.ok(h.state().strokes > 0);
  assert.equal(h.state().clears, 2);
  assert.equal(h.frames.size, 0);
});

test('stopped ambience does not paint or animate on resize or scene change', () => {
  const h = harness();
  h.ambience.start();
  h.ambience.stop();
  const before = h.state();
  h.ambience.resize();
  h.ambience.setKind('rain');
  assert.deepEqual(h.state(), { ...before, painted: false });
  assert.equal(h.frames.size, 0);
  h.ambience.start();
  assert.ok(h.state().strokes > 0);
});

test('repeated static scene and start notifications do not redraw', () => {
  const h = harness();
  h.ambience.start();
  const before = h.state();
  h.ambience.setKind('dust');
  h.ambience.start();
  assert.deepEqual(h.state(), before);
});

test('animated resize and scene change preserve the single frame loop', () => {
  const h = harness(false);
  h.ambience.start();
  h.ambience.resize();
  h.ambience.setKind('rain');
  assert.equal(h.frames.size, 1);
  assert.equal(h.state().clears, 0);
  const [id, frame] = h.frames.entries().next().value;
  h.frames.delete(id);
  frame();
  assert.equal(h.state().painted, true);
  assert.ok(h.state().strokes > 0);
  assert.equal(h.frames.size, 1);
  h.ambience.stop();
  assert.equal(h.frames.size, 0);
});
