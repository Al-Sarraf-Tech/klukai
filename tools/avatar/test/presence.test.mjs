import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as avatar from '../../../flutter_app/web/companion/avatar3d.js';

test('saved framing survives speech and camera changes, rejects unsafe offsets, and resets', () => {
  const view = avatar.createCameraView({ dTheta: 7, dPhi: 0.06, zoom: 0.8, panY: 0.3 });
  assert.ok(Math.abs(view.state.dTheta) < Math.PI);
  assert.equal(view.state.panY, 0.3);
  view.moveBy(0.12);
  assert.equal(view.state.panY, 0.18);
  for (let i = 0; i < 100; i++) {
    const p = view.place({ radius: 4 + i / 100, theta: 0, phi: 1.5 }, i % 2);
    view.absorb(p);
  }
  assert.equal(view.state.panY, 0.18);
  view.set({ panY: Infinity, zoom: NaN, dTheta: NaN });
  assert.equal(view.state.panY, 0);
  assert.equal(view.state.zoom, 1);
  view.moveBy(100);
  assert.ok(view.state.panY >= -1.2);
  view.reset();
  assert.equal(view.state.panY, 0);
});

test('taps tolerate hand jitter but dragging, holding and pinching never trigger reactions', () => {
  assert.equal(typeof avatar.createAvatarGesture, 'function');
  const g = avatar.createAvatarGesture();
  g.down(1, 10, 10, 0);
  assert.equal(g.move(1, 13, 12), null);
  assert.deepEqual(g.up(1, 13, 12, 120), { x: 13, y: 12, downAt: 0, upAt: 120 });
  g.down(2, 10, 10, 200);
  assert.deepEqual(g.move(2, 10, 40), { dx: 0, dy: 30 });
  assert.equal(g.up(2, 10, 40, 300), null);
  g.down(1, 10, 10, 400);
  g.down(2, 20, 10, 420);
  assert.equal(g.move(1, 10, 40), null);
  assert.equal(g.up(2, 20, 10, 450), null);
  assert.equal(g.up(1, 10, 10, 460), null);
  g.down(1, 10, 10, 500);
  assert.equal(g.up(1, 10, 10, 1500), null);
  g.down(1, 10, 10, 1600);
  g.cancel();
  assert.equal(g.up(1, 10, 10, 1700), null);
});

test('a reaction has a cooldown and restores the latest underlying state without replacing its mood', () => {
  assert.equal(typeof avatar.createReactionController, 'function');
  let now = 0, shown = 'idle';
  const sm = avatar.createStateMachine({ enter: (state) => { shown = state; }, now: () => now });
  sm.setState('thinking');
  const r = avatar.createReactionController({ now: () => now, available: ['salute', 'thankful', 'happy'],
    play: (reaction) => { shown = reaction.clip; }, restore: () => { shown = sm.state; } });
  assert.equal(r.trigger('salute'), true);
  assert.equal(shown, 'salute');
  assert.equal(sm.state, 'thinking');
  assert.equal(r.trigger('pleased'), false);
  r.finish();
  assert.equal(shown, 'thinking');
  assert.equal(r.trigger('pleased'), false);
  now = 4000;
  assert.equal(r.trigger('pleased'), true);
  sm.setMood('quietly_pleased');
  r.finish();
  assert.equal(shown, 'happy');
  assert.equal(sm.source, 'mood');
});

test('lifting and replacing a pinch finger keeps the surviving finger in the same non-tap gesture', () => {
  const g = avatar.createAvatarGesture();
  g.down(2, 100, 300, 0);
  g.down(3, 240, 300, 5);
  assert.equal(g.up(2, 100, 300, 100), null);
  g.cancel(2); // lostpointercapture for the released finger; 3 remains held
  g.down(4, 145, 320, 110);
  assert.equal(g.up(4, 145, 320, 150), null);
  g.cancel(4);
  assert.equal(g.up(3, 240, 300, 160), null);
  g.cancel(3);
  g.down(5, 120, 300, 200);
  assert.deepEqual(g.up(5, 120, 300, 240), { x: 120, y: 300, downAt: 200, upAt: 240 }, 'a later independent tap still works');
});

test('speech opens the authored lip gap while keeping eyes, hair and closed rest unchanged', () => {
  assert.equal(typeof avatar.speechMouthDelta, 'function');
  const upper = avatar.speechMouthDelta([0, 1.33669, 0.05643]);
  const lower = avatar.speechMouthDelta([0, 1.33631, 0.05633]);
  assert.ok(upper[1] > 0);
  assert.ok(lower[1] < -0.004);
  assert.ok(upper[1] - lower[1] > 0.006);
  for (const p of [[0, 1.386, 0.05], [0.06, 1.337, 0.055], [0, 1.337, -0.03]]) {
    assert.deepEqual(avatar.speechMouthDelta(p), [0, 0, 0]);
  }
  assert.equal(avatar.speechMouthLevel(0), 0);
  assert.ok(avatar.speechMouthLevel(0.4) > 0.4);
  assert.ok(avatar.speechMouthLevel(100) <= 1);
  const variantUpper = avatar.speechMouthDelta([0, 1.33765, 0.05715], 1.33745);
  const variantLower = avatar.speechMouthDelta([0, 1.33726, 0.05706], 1.33745);
  assert.ok(variantUpper[1] - variantLower[1] > 0.006, 'Garf lip seam opens too');
});
