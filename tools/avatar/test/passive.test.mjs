import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as avatar from '../../../flutter_app/web/companion/avatar3d.js';

const available = ['fidget_glance', 'fidget_shift', 'fidget_ready', 'fidget_happy', 'thankful'];
function advance(passive, seconds, context = {}) {
  for (let i = 0; i < seconds * 10; i++) passive.tick(0.1, context);
}

// Losing any new reaction mapping would make a click cycle fall back or repeat.
test('click reactions cycle through seven distinct installed gestures and reject unavailable clips', () => {
  const shown = [];
  const reactions = avatar.createReactionController({
    available: ['salute', 'thankful', 'happy', 'thinking', 'dismiss', 'bashful', 'excited'],
    cooldown: 0, play: ({ clip }) => shown.push(clip),
  });
  for (let i = 0; i < 7; i++) { assert.equal(reactions.trigger(), true); reactions.finish(); }
  assert.deepEqual(shown, ['salute', 'thankful', 'happy', 'thinking', 'dismiss', 'bashful', 'excited']);
  const partial = avatar.createReactionController({ available: ['thinking'] });
  assert.equal(partial.trigger('dismiss'), false);
  assert.equal(partial.trigger('thoughtful'), true);
});

// The scheduler must measure uninterrupted visible time, not full clip loops.
test('passive animation waits 12–24 calm seconds and never repeats its previous gesture', () => {
  assert.equal(typeof avatar.createPassiveController, 'function');
  const shown = [];
  const p = avatar.createPassiveController({ available, random: () => 0, play: (name) => shown.push(name) });
  advance(p, 11.9);
  assert.equal(p.active, null);
  advance(p, 0.2);
  assert.equal(p.active, 'fidget_glance');
  advance(p, 40);
  assert.equal(p.count, 1, 'an active one-shot cannot start another');
  p.finish();
  advance(p, 12.1);
  assert.equal(p.active, 'fidget_shift');
  assert.deepEqual(shown, ['fidget_glance', 'fidget_shift']);
  const slow = avatar.createPassiveController({ available, random: () => 1 });
  advance(slow, 23.9); assert.equal(slow.count, 0);
  advance(slow, 0.2); assert.equal(slow.count, 1);
});

// Each priority input must independently stop a fidget and restart the quiet wait.
for (const [name, context] of Object.entries({
  speaking: { speaking: 0.1 }, thinking: { state: 'thinking' }, talking: { state: 'talking' },
  sleep: { state: 'sleep' }, reaction: { reacting: true }, drag: { dragging: true },
  paused: { paused: true }, reducedMotion: { reducedMotion: true }, inspection: { enabled: false },
})) {
  test(`passive animation yields immediately to ${name} and waits afresh after it ends`, () => {
    assert.equal(typeof avatar.createPassiveController, 'function');
    let restores = 0;
    const p = avatar.createPassiveController({ available, random: () => 0, restore: () => restores++ });
    advance(p, 12.1);
    assert.ok(p.active);
    p.tick(0.1, context);
    assert.equal(p.active, null);
    assert.equal(restores, 1);
    advance(p, 40, context);
    assert.equal(p.count, 1);
    p.tick(0.1);
    advance(p, 11.8);
    assert.equal(p.count, 1);
    advance(p, 0.3);
    assert.equal(p.count, 2);
  });
}

test('suspension gaps cannot immediately trigger a passive or replay missed gestures', () => {
  assert.equal(typeof avatar.createPassiveController, 'function');
  const p = avatar.createPassiveController({ available, random: () => 0 });
  advance(p, 11);
  p.tick(3600);
  assert.equal(p.count, 0);
  advance(p, 11.8);
  assert.equal(p.count, 0);
  advance(p, 0.3);
  assert.equal(p.count, 1);
});

test('passive completion restores the current state-machine mood without changing its source', () => {
  assert.equal(typeof avatar.createPassiveController, 'function');
  let shown = 'idle';
  const sm = avatar.createStateMachine({ enter: (state) => { shown = state; }, now: () => 0 });
  const p = avatar.createPassiveController({ available, random: () => 0,
    play: (name) => { shown = name; }, restore: () => { shown = sm.state; } });
  advance(p, 12.1);
  sm.setMood('quietly_pleased');
  p.finish();
  assert.equal(shown, 'happy');
  assert.equal(sm.state, 'happy');
  assert.equal(sm.source, 'mood');
});

test('a camera pinch stays active until its final finger lifts or is cancelled', () => {
  const g = avatar.createAvatarGesture();
  g.down(1, 10, 10, 0); g.down(2, 40, 10, 1);
  assert.equal(g.active, true);
  g.up(1, 10, 10, 2);
  assert.equal(g.active, true);
  g.cancel(1);
  assert.equal(g.active, true);
  g.cancel(2);
  assert.equal(g.active, false);
});
