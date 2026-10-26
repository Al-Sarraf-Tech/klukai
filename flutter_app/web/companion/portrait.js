// Live portrait renderer: stacks her portrait frames (base, expression,
// blink, talk — all img2img from one base, so they line up) and brings them
// to life: irregular blinks, mouth flaps driven by her voice level, mood
// crossfades. Breathing/sway is pure CSS (companion.css).

import { nextBlink, mouthOpen } from "./logic.js";

export const FALLBACK_PORTRAIT = "assets/assets/klukai_portrait.png";

export function createPortrait(root, {
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (id) => clearTimeout(id),
  raf = (fn) => requestAnimationFrame(fn),
  caf = (id) => cancelAnimationFrame(id),
  now = () => performance.now(),
  rand = Math.random,
  onFrameError = () => {}, // (layer, url): an <img> failed to load, e.g. an expired signed URL
} = {}) {
  const el = {
    base: root.querySelector(".frame-base"),
    expr: root.querySelector(".frame-expr"),
    blink: root.querySelector(".frame-blink"),
    talk: root.querySelector(".frame-talk"),
  };
  // Signed frame URLs expire after 15–30 min; an expression first requested
  // after that 403s and, unreported, would never show again this session.
  for (const [layer, node] of Object.entries(el)) {
    node?.addEventListener?.("error", () => onFrameError(layer, node.getAttribute("src")));
  }
  let frames = {};
  let expression = "base";
  let level = 0;
  let active = false;
  let blinkTimer = null;
  const blinkTimers = new Set();
  let blinkGeneration = 0;
  let talkLoop = null;

  const on = (node, yes) => node.classList.toggle("on", Boolean(yes));

  function showExpression() {
    const url = expression !== "base" ? frames[expression] : null;
    if (url) {
      if (el.expr.getAttribute("src") !== url) el.expr.setAttribute("src", url);
      on(el.expr, true);
    } else {
      on(el.expr, false);
    }
  }

  function armBlink(fn, ms) {
    const generation = blinkGeneration;
    const id = setTimer(() => {
      if (generation !== blinkGeneration || !active) return;
      blinkTimers.delete(id);
      fn();
    }, ms);
    blinkTimers.add(id);
    return id;
  }

  function scheduleBlink() {
    clearTimer(blinkTimer);
    blinkTimers.delete(blinkTimer);
    if (!active) return;
    const b = nextBlink(rand);
    blinkTimer = armBlink(() => blink(b.closedMs, b.double), b.delay);
  }

  function blink(closedMs, double) {
    // A closing eye mid-word looks wrong (and there may be no blink frame yet):
    // skip this one but always book the next, or she stops blinking for good.
    if (!frames.blink || level > 0.06) { scheduleBlink(); return; }
    on(el.blink, true);
    armBlink(() => {
      on(el.blink, false);
      // Second half of a double blink; it books the next blink itself.
      if (double) armBlink(() => blink(closedMs, false), 140);
    }, closedMs);
    if (!double) scheduleBlink();
  }

  function talkFrame() {
    talkLoop = null;
    const open = frames.talk && mouthOpen(level, now());
    on(el.talk, open);
    if (active && level > 0.06) talkLoop = raf(talkFrame);
    else on(el.talk, false);
  }

  return {
    /** frames: {base, blink, talk, smile, blush, annoyed} → url|null. */
    setFrames(next, { pending = false } = {}) {
      frames = { ...next };
      const base = frames.base || FALLBACK_PORTRAIT;
      if (el.base.getAttribute("src") !== base) el.base.setAttribute("src", base);
      on(el.base, true);
      for (const k of ["blink", "talk"]) {
        if (frames[k]) el[k].setAttribute("src", frames[k]);
        else on(el[k], false);
      }
      root.classList.toggle("pending", pending || !frames.base);
      showExpression();
    },
    setExpression(name) {
      expression = name || "base";
      showExpression();
    },
    /** Her voice loudness 0..1 — flaps the mouth while she speaks. */
    setLevel(next) {
      level = next;
      if (active && level > 0.06 && !talkLoop) talkLoop = raf(talkFrame);
    },
    setActive(yes) {
      const next = Boolean(yes);
      if (next === active) return;
      active = next;
      if (active) scheduleBlink();
      else {
        blinkGeneration++;
        for (const timer of blinkTimers) clearTimer(timer);
        blinkTimers.clear();
        blinkTimer = null;
        if (talkLoop) { caf(talkLoop); talkLoop = null; }
        on(el.blink, false);
        on(el.talk, false);
      }
    },
    get state() { return { expression, level, active, frames: { ...frames } }; },
  };
}
