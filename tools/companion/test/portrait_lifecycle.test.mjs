import { test } from "node:test";
import assert from "node:assert/strict";
import { createPortrait } from "../../../flutter_app/web/companion/portrait.js";

function harness() {
  const elements = new Map();
  function element(key) {
    if (!elements.has(key)) {
      const classes = new Set();
      const attrs = new Map();
      elements.set(key, {
        classes,
        classList: { toggle: (name, on) => on ? classes.add(name) : classes.delete(name) },
        getAttribute: (name) => attrs.get(name),
        setAttribute: (name, value) => attrs.set(name, value),
      });
    }
    return elements.get(key);
  }
  const root = element("root");
  root.querySelector = element;
  const timers = new Map();
  let nextId = 0;
  const portrait = createPortrait(root, {
    setTimer: (fn, ms) => { const id = nextId++; timers.set(id, { fn, ms }); return id; },
    clearTimer: (id) => timers.delete(id),
    rand: () => 0.1, // double blinks, 110 ms closed
  });
  portrait.setFrames({ base: "/base", blink: "/blink" });
  function fireNext() {
    const [id, timer] = timers.entries().next().value;
    timers.delete(id);
    timer.fn();
    return timer;
  }
  return {
    portrait, timers, fireNext,
    blinking: () => element(".frame-blink").classes.has("on"),
  };
}

for (const phase of [0, 1, 2, 3]) {
  test(`deactivation clears every pending blink during phase ${phase}`, () => {
    const h = harness();
    h.portrait.setActive(true);
    for (let i = 0; i < phase; i++) h.fireNext();
    h.portrait.setActive(false);
    assert.equal(h.blinking(), false);
    assert.equal(h.timers.size, 0, "no blink callback remains scheduled offscreen");
  });
}

test("an already-dispatched blink callback cannot act after stop and reactivation", () => {
  const h = harness();
  h.portrait.setActive(true);
  h.fireNext(); // first blink closes
  const stale = [...h.timers.values()][0].fn;
  h.portrait.setActive(false);
  h.portrait.setActive(true);
  h.fireNext(); // the new generation's first blink closes
  const pending = [...h.timers.keys()];
  assert.equal(h.blinking(), true);
  stale();
  assert.equal(h.blinking(), true, "stale reopen cannot alter the new blink");
  assert.deepEqual([...h.timers.keys()], pending, "stale callback schedules no second blink");
});

test("repeated active notifications preserve one ongoing double blink", () => {
  const h = harness();
  h.portrait.setActive(true);
  h.fireNext();
  const pending = [...h.timers.keys()];
  h.portrait.setActive(true);
  assert.deepEqual([...h.timers.keys()], pending, "active notification does not add a parallel blink");
  h.fireNext(); // reopen
  assert.equal(h.blinking(), false);
  h.fireNext(); // second close
  assert.equal(h.blinking(), true);
  h.fireNext(); // second reopen
  assert.equal(h.blinking(), false);
  assert.equal(h.timers.size, 1, "the next normal blink is scheduled once");
});
