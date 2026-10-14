import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createReactionController } from '../../../flutter_app/web/companion/avatar3d.js';

test('downloaded gestures are selectable while dance remains an explicit choice', () => {
  const played = [];
  const catalog = [
    { id: 'salute', clip: 'salute', label: 'Salute', category: 'Everyday' },
    { id: 'wave', clip: 'motion_greeting', label: 'Greeting', category: 'Everyday' },
    { id: 'dance', clip: 'motion_dance', label: 'Dance', category: 'Playful', auto: false },
  ];
  const r = createReactionController({ catalog, available: catalog.map((x) => x.clip),
    cooldown: 0, play: (reaction) => played.push(reaction.clip) });
  for (let i = 0; i < 4; i++) { assert.equal(r.trigger(), true); r.finish(); }
  assert.deepEqual(played, ['salute', 'motion_greeting', 'salute', 'motion_greeting']);
  assert.equal(r.trigger('dance'), true);
  assert.equal(played.at(-1), 'motion_dance');
  r.finish();
  assert.equal(r.trigger('missing'), false);
});

test('unavailable motions never appear in the selectable catalog or automatic cycle', () => {
  const r = createReactionController({ available: ['salute'], catalog: [
    { id: 'salute', clip: 'salute', label: 'Salute', category: 'Everyday' },
    { id: 'wave', clip: 'motion_greeting', label: 'Greeting', category: 'Everyday' },
  ] });
  assert.deepEqual(r.choices.map((choice) => choice.id), ['salute']);
  assert.equal(r.trigger('wave'), false);
  assert.equal(r.trigger(), true);
});
