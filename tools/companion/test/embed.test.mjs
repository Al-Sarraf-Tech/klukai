import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  SPEAK, VISIBILITY, MAX_SPEAK_CHARS, embedConfig, speakRequest, visibilityRequest, stageActivity,
  createSpeakGate, resumeWithin, postToParent,
} from "../../../flutter_app/web/companion/embed.js";

const root = new URL("../../../", import.meta.url);
const source = (path) => readFileSync(new URL(path, root), "utf8");

const ORIGIN = "https://klukai.example";
const parent = { name: "pwa" };
const speak = (data, over = {}) => ({ origin: ORIGIN, source: parent, data, ...over });

test("embed=1 forces 3D and reads clamped insets; no flag is the full stage", () => {
  assert.deepEqual(embedConfig("?embed=1&mode=3d&inset_top=96&inset_bottom=240"), {
    embed: true, mode: "model", hidden: false, insets: { top: 96, bottom: 240 },
  });
  // mode=portrait cannot turn embedded 3D off
  assert.equal(embedConfig("?embed=1&mode=portrait").mode, "model");
  assert.deepEqual(embedConfig("?embed=1&inset_top=-5&inset_bottom=99999").insets, { top: 0, bottom: 600 });
  assert.deepEqual(embedConfig("?embed=1&inset_top=abc&inset_bottom=").insets, { top: 120, bottom: 210 });
  assert.deepEqual(embedConfig(""), { embed: false, mode: null, hidden: false, insets: { top: 120, bottom: 210 } });
  assert.equal(embedConfig("?embed=true").embed, false);
});

test("hidden=1 starts the embedded stage parked behind the PWA's CHAT face", () => {
  assert.equal(embedConfig("?embed=1&mode=3d&hidden=1").hidden, true);
  assert.equal(embedConfig("?embed=1&mode=3d").hidden, false);
  assert.equal(embedConfig("?embed=1&hidden=true").hidden, false);
  // Only the embedded stage can be parked; the full stage always shows.
  assert.equal(embedConfig("?hidden=1").hidden, false);
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

test("only the same-origin parent can show or hide the stage", () => {
  const ctx = { origin: ORIGIN, parent };
  const show = (data, over) => speak(data, over);
  assert.deepEqual(visibilityRequest(show({ type: VISIBILITY, visible: false }), ctx), { visible: false });
  assert.deepEqual(visibilityRequest(show({ type: VISIBILITY, visible: true }), ctx), { visible: true });
  assert.equal(visibilityRequest(show({ type: VISIBILITY, visible: true }, { origin: "https://evil.example" }), ctx), null);
  assert.equal(visibilityRequest(show({ type: VISIBILITY, visible: true }, { source: { name: "other frame" } }), ctx), null);
  assert.equal(visibilityRequest(show({ type: VISIBILITY, visible: true }), { origin: ORIGIN, parent: null }), null);
  assert.equal(visibilityRequest(show({ type: VISIBILITY, visible: "yes" }), ctx), null);
  assert.equal(visibilityRequest(show({ type: VISIBILITY }), ctx), null);
  assert.equal(visibilityRequest(show({ type: SPEAK, visible: true }), ctx), null);
  assert.equal(visibilityRequest(show(VISIBILITY), ctx), null);
  assert.equal(visibilityRequest(null, ctx), null);
  // A visibility message is never a speak request, and the other way round.
  assert.equal(speakRequest(show({ type: VISIBILITY, visible: true, text: "hi" }), ctx), null);
});

test("off screen the stage neither draws nor plays", () => {
  const on = { shown: true, render: true, portrait: false, ambience: true, audio: true };
  const off = { shown: false, render: false, portrait: false, ambience: false, audio: false };
  assert.deepEqual(stageActivity({ mode: "model", awake: true }), on);
  // Parked behind the PWA's CHAT face, or the whole page in the background.
  assert.deepEqual(stageActivity({ embedHidden: true, mode: "model", awake: true }), off);
  assert.deepEqual(stageActivity({ documentHidden: true, mode: "model", awake: true }), off);
  assert.deepEqual(stageActivity({ documentHidden: true, embedHidden: true, mode: "portrait", awake: true }), off);
  // The portrait blinks only when she is awake in portrait mode, on screen.
  assert.deepEqual(stageActivity({ mode: "portrait", awake: true }),
    { shown: true, render: false, portrait: true, ambience: true, audio: true });
  assert.equal(stageActivity({ mode: "portrait", awake: false }).portrait, false);
  assert.equal(stageActivity({ embedHidden: true, mode: "portrait", awake: true }).portrait, false);
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

test("app.js parks the stage while the PWA hides it, and never speaks on show", () => {
  const app = source("flutter_app/web/companion/app.js");
  // Same strict origin + source check as a speak request.
  assert.match(app, /visibilityRequest\(event, \{ origin: location\.origin, parent: window\.parent \}\)/);
  // It starts parked when the PWA loads it behind the CHAT face.
  assert.match(app, /let embedHidden = EMBED\.hidden;/);
  // One rule for what runs, shared by page visibility and the PWA's flip.
  assert.match(app, /stageActivity\(\{ documentHidden: document\.hidden, embedHidden,/);
  assert.match(app, /avatar\?\.setPaused\(!run\.render\)/);
  assert.match(app, /if \(run\.ambience\) ambience\.start\(\); else ambience\.stop\(\);/);
  assert.match(app, /music\?\.setHidden\(!run\.audio\)/);
  // Hidden: her voice stops; a speak request while hidden is refused.
  assert.match(app, /if \(embedHidden\) voice\?\.stop\(\);/);
  assert.match(app, /if \(embedHidden\) \{ reply\("hidden"\); return; \}/);
  // The appearance switcher parks a model that finishes loading off screen.
  assert.match(app, /get hidden\(\) \{ return document\.hidden \|\| embedHidden; \}/);
  const appearance = source("flutter_app/web/companion/stage_appearance.js");
  assert.doesNotMatch(appearance, /document\.hidden/);
  assert.equal((appearance.match(/prefs\.mode !== "model" \|\| runtime\.hidden/g) || []).length, 2);
  // Showing her again never makes a sound: still only two speak() calls.
  assert.equal((app.match(/voice\.speak\(/g) || []).length, 2);
});

test("a timed notice clears itself in the PWA's readout too", () => {
  const app = source("flutter_app/web/companion/app.js");
  assert.match(app, /if \(EMBED\.embed\) postToParent\(window, \{ type: "klukai:stage-notice", text: "" \}\);/);
});

test("embedded CSS hides the stage chrome; the page loads the current files", () => {
  const css = source("flutter_app/web/companion/companion.css");
  assert.match(css, /html\[data-embed\] \.topbar,\s*html\[data-embed\] \.notice,\s*html\[data-embed\] \.dock,\s*html\[data-embed\] \.wake \{ display: none !important; \}/);
  const html = source("flutter_app/web/companion.html");
  assert.match(html, /companion\/app\.js\?v=[0-9a-f]{12}/);
  assert.match(html, /companion\/companion\.css\?v=[0-9a-f]{12}/);
  assert.match(html, /id="btn-voice" aria-label="Hear her last line"/);
});
