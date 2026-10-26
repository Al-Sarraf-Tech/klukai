import test from 'node:test';
import assert from 'node:assert/strict';
import { createStageAppearance } from '../../../flutter_app/web/companion/stage_appearance.js';

function element() {
  return {
    style: {}, dataset: {}, hidden: false, value: '', textContent: '', disabled: false,
    appendChild() {}, remove() {}, addEventListener() {}, replaceChildren() {},
    removeAttribute() {}, showModal() {}, close() {},
  };
}

let appearanceId = 0;
function harness(t, { awake = true, hidden = false } = {}) {
  const oldDocument = globalThis.document;
  globalThis.document = { createElement: tag => tag === 'canvas' ? { getContext: () => null } : element() };
  t.after(() => { globalThis.document = oldDocument; });
  const elements = new Map();
  const $ = id => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  let resolveModel;
  let markStarted;
  const started = new Promise(resolve => { markStarted = resolve; });
  const model = new Promise(resolve => { resolveModel = resolve; });
  const active = [];
  const runtime = { avatar: null, state: { mood: 'composed', thinking: false }, awake, hidden };
  const prefs = { mode: 'model', appearance: `test-${++appearanceId}` };
  const stage = createStageAppearance({
    $, api: { models: async () => ({ models: [] }), model: () => { markStarted(); return model; } },
    prefs, savePrefs() {}, notice() {}, portrait: { setActive: value => active.push(value) },
    stage: { dataset: {} }, runtime, syncButtons() {}, syncInsets() {},
  });
  return { stage, runtime, prefs, active, started, failRenderer: () => resolveModel(new ArrayBuffer(8)) };
}

for (const state of [
  { name: 'hidden', awake: true, hidden: true, expected: false },
  { name: 'visible awake', awake: true, hidden: false, expected: true },
  { name: 'asleep', awake: false, hidden: false, expected: false },
]) {
  test(`failed model fallback keeps portrait activity correct when ${state.name}`, async t => {
    const h = harness(t);
    const pending = h.stage.setMode('model');
    await h.started;
    h.runtime.awake = state.awake;
    h.runtime.hidden = state.hidden;
    h.failRenderer();
    await pending;
    assert.equal(h.prefs.mode, 'portrait');
    assert.equal(h.active.at(-1), state.expected);
  });

  test(`portrait selection keeps activity correct when ${state.name}`, async t => {
    const h = harness(t, state);
    await h.stage.setMode('portrait');
    assert.equal(h.active.at(-1), state.expected);
  });
}
