import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  SPEAK, MAX_SPEAK_CHARS, embedConfig, speakRequest, createSpeakGate, resumeWithin, postToParent,
} from "../../../flutter_app/web/companion/embed.js";

const root = new URL("../../../", import.meta.url);
const source = (path) => readFileSync(new URL(path, root), "utf8");

const ORIGIN = "https://klukai.example";
const parent = { name: "pwa" };
const speak = (data, over = {}) => ({ origin: ORIGIN, source: parent, data, ...over });

test("embed=1 forces 3D and reads clamped insets; no flag is the full stage", () => {
  assert.deepEqual(embedConfig("?embed=1&mode=3d&inset_top=96&inset_bottom=240"), {
    embed: true, mode: "model", insets: { top: 96, bottom: 240 },
  });
  // mode=portrait cannot turn embedded 3D off
  assert.equal(embedConfig("?embed=1&mode=portrait").mode, "model");
  assert.deepEqual(embedConfig("?embed=1&inset_top=-5&inset_bottom=99999").insets, { top: 0, bottom: 600 });
  assert.deepEqual(embedConfig("?embed=1&inset_top=abc&inset_bottom=").insets, { top: 120, bottom: 210 });
  assert.deepEqual(embedConfig(""), { embed: false, mode: null, insets: { top: 120, bottom: 210 } });
  assert.equal(embedConfig("?embed=true").embed, false);
});

test("only the same-origin parent can ask her to speak", () => {
  const ctx = { origin: ORIGIN, parent };
  assert.deepEqual(speakRequest(speak({ type: SPEAK, text: "  Sit. The stars are clear.  ", id: 3 }), ctx),
    { text: "Sit. The stars are clear.", id: 3 });
  assert.equal(speakRequest(speak({ type: SPEAK, text: "hi", id: 1 }, { origin: "https://evil.example" }), ctx), null);
  assert.equal(speakRequest(speak({ type: SPEAK, text: "hi", id: 1 }, { source: { name: "other frame" } }), ctx), null);
  assert.equal(speakRequest(speak({ type: SPEAK, text: "hi" }), { origin: ORIGIN, parent: null }), null);
  assert.equal(speakRequest(speak({ type: "klukai:other", text: "hi" }), ctx), null);
  assert.equal(speakRequest(speak({ type: SPEAK, text: 42 }), ctx), null);
  assert.equal(speakRequest(speak({ type: SPEAK, text: "   " }), ctx), null);
  assert.equal(speakRequest(speak("klukai:speak"), ctx), null);
  assert.equal(speakRequest(null, ctx), null);
  const long = speakRequest(speak({ type: SPEAK, text: "a".repeat(MAX_SPEAK_CHARS + 50) }), ctx);
  assert.equal(long.text.length, MAX_SPEAK_CHARS);
  assert.equal(long.id, null);
});

test("each tap plays once: a repeated id is dropped, a new tap is admitted", () => {
  const gate = createSpeakGate();
  assert.equal(gate.admit(null), false);
  assert.equal(gate.admit({ text: "a", id: 1 }), true);
  assert.equal(gate.admit({ text: "a", id: 1 }), false);
  assert.equal(gate.admit({ text: "a", id: 2 }), true);
  assert.equal(gate.admit({ text: "b", id: null }), true);
});

test("resumeWithin gives up on a context that never resumes, and reports success", async () => {
  const stuck = { state: "suspended", resume: () => new Promise(() => {}) };
  const timers = [];
  const pending = resumeWithin(stuck, 50, {
    setTimer: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimer: () => {},
  });
  await Promise.resolve();
  assert.equal(timers[0].ms, 50);
  timers[0].fn();
  assert.equal(await pending, false);

  const ok = { state: "suspended", resume() { this.state = "running"; return Promise.resolve(); } };
  assert.equal(await resumeWithin(ok, 50), true);
  const refused = { state: "suspended", resume: () => Promise.reject(new Error("NotAllowedError")) };
  assert.equal(await resumeWithin(refused, 50), false);
  assert.equal(await resumeWithin({ state: "running" }), true);
  assert.equal(await resumeWithin(null), false);
});

test("postToParent targets this origin and never throws", () => {
  const sent = [];
  const win = { location: { origin: ORIGIN }, parent: { postMessage: (m, o) => sent.push([m, o]) } };
  postToParent(win, { type: "klukai:stage-ready", ready: true });
  assert.deepEqual(sent, [[{ type: "klukai:stage-ready", ready: true }, ORIGIN]]);
  const top = { location: { origin: ORIGIN } };
  top.parent = top; // not embedded: nobody to tell
  postToParent(top, { type: "x" });
  postToParent({ location: { origin: ORIGIN }, parent: { postMessage: () => { throw new Error("gone"); } } }, {});
  postToParent(null, {});
});

test("app.js wires embed mode without bringing back auto-play", () => {
  const app = source("flutter_app/web/companion/app.js");
  // A finished reply is remembered, never spoken by itself.
  assert.match(app, /if \(fx\.kind === "speak"\) \{ lastLine = fx\.text; syncButtons\(\); \}/);
  assert.doesNotMatch(app, /fx\.kind === "speak"\) voice\?\.speak/);
  // The only speak() calls: the stage's voice button and the PWA's request.
  const speaks = app.match(/voice\.speak\(/g) || [];
  assert.equal(speaks.length, 2);
  assert.match(app, /voice\.speak\(lastLine\)/);
  assert.match(app, /voice\.speak\(request\.text\)/);
  assert.match(app, /speakRequest\(event, \{ origin: location\.origin, parent: window\.parent \}\)/);
  assert.match(app, /gate\.admit\(request\)/);
  // Embedded: no music of its own, no overwriting his saved mode, no login page in the frame.
  assert.match(app, /prefs\.music && !EMBED\.embed/);
  assert.match(app, /savePrefs\(\{ \.\.\.p, mode: storedMode \}\)/);
  assert.match(app, /if \(EMBED\.embed\) postToParent\(window, \{ type: "klukai:auth-expired" \}\)/);
});

test("embedded CSS hides the stage chrome; the page loads the current files", () => {
  const css = source("flutter_app/web/companion/companion.css");
  assert.match(css, /html\[data-embed\] \.topbar,\s*html\[data-embed\] \.notice,\s*html\[data-embed\] \.dock,\s*html\[data-embed\] \.wake \{ display: none !important; \}/);
  const html = source("flutter_app/web/companion.html");
  assert.match(html, /companion\/app\.js\?v=[0-9a-f]{12}/);
  assert.match(html, /companion\/companion\.css\?v=[0-9a-f]{12}/);
  assert.match(html, /id="btn-voice" aria-label="Hear her last line"/);
});
