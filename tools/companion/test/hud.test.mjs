import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { HUD, HERDAY, EMPTY_HUD, MAX_LINE_CHARS, hudUpdate, speakRequest, visibilityRequest } from "../../../flutter_app/web/companion/embed.js";
import { hudView, nextVoicePhase, createHud, VOICE_FACES, PLACEHOLDER } from "../../../flutter_app/web/companion/hud.js";

const root = new URL("../../../", import.meta.url);
const source = (path) => readFileSync(new URL(path, root), "utf8");
const ORIGIN = "https://klukai.example";
const parent = { name: "pwa" };
const ctx = { origin: ORIGIN, parent };
const msg = (data, over = {}) => ({ origin: ORIGIN, source: parent, data, ...over });
const SNAP = {
  type: HUD, level: 7, mood: "COMPOSED", bpm: 72, spiking: false, wearing: "Blazing Star",
  line: "The perimeter is quiet, Commander.", lineId: 41, streaming: false, status: null,
};

test("contract names", () => {
  assert.equal(HUD, "klukai:hud");
  assert.equal(HERDAY, "klukai:herday");
});

test("only the same-origin parent can update the HUD", () => {
  assert.deepEqual(hudUpdate(msg(SNAP), ctx), {
    level: 7, bondKnown: true, mood: "COMPOSED", bpm: 72, spiking: false, wearing: "Blazing Star",
    line: "The perimeter is quiet, Commander.", lineId: 41, streaming: false, status: null,
  });
  assert.equal(hudUpdate(msg(SNAP, { origin: "https://evil.example" }), ctx), null);
  assert.equal(hudUpdate(msg(SNAP, { source: { name: "other frame" } }), ctx), null);
  assert.equal(hudUpdate(msg(SNAP), { origin: ORIGIN, parent: null }), null);
  assert.equal(hudUpdate(msg({ ...SNAP, type: "klukai:speak" }), ctx), null);
  assert.equal(hudUpdate(msg("klukai:hud"), ctx), null);
  assert.equal(hudUpdate(null, ctx), null);
  // A HUD snapshot is never a speak or visibility request (no audio from a snapshot).
  assert.equal(speakRequest(msg({ ...SNAP, text: "hi", visible: true }), ctx), null);
  assert.equal(visibilityRequest(msg({ ...SNAP, visible: true }), ctx), null);
});

test("malformed fields keep the last good value; missing fields reset; values are clamped and capped", () => {
  const prev = hudUpdate(msg(SNAP), ctx);
  const junk = hudUpdate(msg({
    type: HUD, level: "9", mood: 4, bpm: Number.NaN, spiking: "yes", wearing: {}, line: ["x"],
    lineId: { id: 1 }, streaming: 1, status: 7,
  }), ctx, prev);
  assert.deepEqual(junk, prev, "every malformed field is ignored");
  const empty = hudUpdate(msg({ type: HUD }), ctx, prev);
  assert.deepEqual(empty, EMPTY_HUD, "a snapshot without a field means its default (no stale status)");
  const clamped = hudUpdate(msg({ ...SNAP, level: 42.6, bpm: -3, mood: `  ${"M".repeat(200)}  `, lineId: "" }), ctx);
  assert.equal(clamped.level, 9);
  assert.equal(clamped.bpm, 0);
  assert.equal(clamped.mood.length, 60);
  assert.equal(clamped.lineId, null);
  assert.equal(hudUpdate(msg({ ...SNAP, level: -1.2, bpm: 71.6 }), ctx).level, 0);
  assert.equal(hudUpdate(msg({ ...SNAP, bondKnown: false }), ctx).bondKnown, false);
  assert.equal(hudUpdate(msg({ ...SNAP, bondKnown: "no" }), ctx, prev).bondKnown, true);
  assert.equal(hudUpdate(msg({ ...SNAP, bpm: 71.6 }), ctx).bpm, 72);
  const long = hudUpdate(msg({ ...SNAP, line: "a".repeat(MAX_LINE_CHARS - 1) + "😀" + "b".repeat(50) }), ctx);
  assert.equal(long.line.length, MAX_LINE_CHARS - 1, "capped without splitting a surrogate pair");
  assert.ok(long.line.isWellFormed());
  assert.equal(hudUpdate(msg({ ...SNAP, status: "  Compiling tactical visualization, Commander...  " }), ctx).status,
    "Compiling tactical visualization, Commander...");
  assert.equal(hudUpdate(msg({ ...SNAP, status: "   " }), ctx).status, null);
  assert.equal(hudUpdate(msg({ ...SNAP, lineId: "m-9" }), ctx).lineId, "m-9");
  assert.equal(hudUpdate(msg({ ...SNAP, lineId: "x".repeat(500) }), ctx, prev).lineId, 41, "an absurd id is malformed");
});

test("the HUD view: chips, her line or her status, and when VOICE is offered", () => {
  const s = (over) => ({ ...EMPTY_HUD, ...hudUpdate(msg(SNAP), ctx), ...over });
  const v = hudView(s());
  assert.equal(v.level, "LV7");
  assert.equal(v.levelLabel, "Affinity level 7");
  assert.equal(hudView(s({ bondKnown: false, level: 0 })).level, "—");
  assert.equal(hudView(s({ bondKnown: false, level: 0 })).levelLabel, "Affinity unread");
  assert.equal(v.mood, "COMPOSED");
  assert.equal(v.heart, "72 bpm");
  assert.equal(v.spiking, false);
  assert.equal(v.wearing, "Blazing Star");
  assert.equal(v.line, "The perimeter is quiet, Commander.");
  assert.equal(v.placeholder, false);
  assert.equal(v.status, "");
  assert.equal(v.voice, "idle");
  assert.equal(hudView(s({ spiking: true })).spiking, true);
  assert.equal(hudView(s({ mood: "" })).mood, "—");
  assert.equal(hudView(s({ wearing: "" })).wearing, "");
  // No line yet: a placeholder, nothing to speak.
  const quiet = hudView(s({ line: "", lineId: null }));
  assert.deepEqual([quiet.line, quiet.placeholder, quiet.voice], [PLACEHOLDER, true, "off"]);
  // While she streams: the growing line, no VOICE.
  const streaming = hudView(s({ streaming: true, status: "Thinking" }));
  assert.deepEqual([streaming.line, streaming.status, streaming.voice], [SNAP.line, "", "off"]);
  // Working, no line streaming: her status instead of the line; gone when null.
  const working = hudView(s({ status: "Compiling tactical visualization, Commander..." }));
  assert.deepEqual([working.status, working.line, working.voice], ["Compiling tactical visualization, Commander...", "", "off"]);
  assert.equal(hudView(s({ status: null })).status, "");
  // Playing or preparing: the button stays (to stop her) whatever the line does.
  assert.equal(hudView(s({ streaming: true }), "playing").voice, "playing");
  assert.equal(hudView(s({ line: "" }), "loading").voice, "loading");
  for (const face of Object.values(VOICE_FACES)) assert.ok(face.icon && face.label && face.aria);
  assert.equal(VOICE_FACES.idle.label, "VOICE");
  assert.equal(VOICE_FACES.playing.label, "STOP");
});

test("voice phases: one state per tap, steady across clip gaps, back to idle when she is done", () => {
  let phase = "idle";
  const step = (event) => (phase = nextVoicePhase(phase, event));
  assert.equal(step("tap"), "loading");
  assert.equal(step("busy"), "loading");
  assert.equal(step("speaking"), "playing");
  assert.equal(step("busy"), "playing", "between clips she is still playing");
  assert.equal(step("done"), "idle");
  assert.equal(step("tap"), "loading");
  assert.equal(step("tap"), "idle", "a second tap stops her");
  assert.equal(step("busy"), "loading", "a PWA-requested line shows too");
  assert.equal(step("failed"), "idle");
  assert.equal(step("nonsense"), "idle");
  assert.equal(nextVoicePhase("playing", "tap"), "idle");
});

// ── DOM binder against a tiny fake document ──────────────────────────────
function fakeDocument({ hiddenLayout = false } = {}) {
  const nodes = new Map();
  const writes = [];
  const make = (id) => {
    const listeners = {};
    const classes = new Set();
    const attrs = {};
    let text = "";
    let hidden = id === "hud" || id === "hud-status";
    let scrollTop = 0;
    let scrollHeight = 40, clientHeight = 88;
    const el = {
      id, dataset: {}, listeners, attrs,
      get scrollHeight() { return hiddenLayout && nodes.get("hud")?.hidden ? 0 : scrollHeight; },
      set scrollHeight(v) { scrollHeight = v; },
      get clientHeight() { return hiddenLayout && nodes.get("hud")?.hidden ? 0 : clientHeight; },
      set clientHeight(v) { clientHeight = v; },
      get textContent() { return text; },
      set textContent(v) { writes.push([id, "text", v]); text = String(v); if (el.heightFor) el.scrollHeight = el.heightFor(text); },
      get hidden() { return hidden; },
      set hidden(v) { writes.push([id, "hidden", v]); hidden = Boolean(v); },
      get scrollTop() { return scrollTop; },
      set scrollTop(v) { scrollTop = Math.max(0, Math.min(Number(v), Math.max(0, el.scrollHeight - el.clientHeight))); },
      classList: {
        toggle(name, on) { writes.push([id, "class", name, on]); if (on) classes.add(name); else classes.delete(name); },
        contains: (name) => classes.has(name),
      },
      setAttribute(name, value) { writes.push([id, "attr", name, value]); attrs[name] = String(value); },
      addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
      click() { for (const fn of listeners.click || []) fn({ type: "click" }); },
    };
    return el;
  };
  return {
    writes, nodes,
    getElementById(id) { if (!nodes.has(id)) nodes.set(id, make(id)); return nodes.get(id); },
    documentElement: { dataset: {} },
  };
}

test("the HUD renders text nodes once per change, coalesced to one frame, and only after a snapshot", () => {
  const doc = fakeDocument();
  const frames = [];
  const taps = { voice: 0, herday: 0 };
  const hud = createHud(doc, { onVoice: () => taps.voice++, onHerDay: () => taps.herday++, schedule: (fn) => frames.push(fn) });
  const s = { ...EMPTY_HUD, ...hudUpdate(msg(SNAP), ctx) };
  assert.equal(doc.getElementById("hud").hidden, true, "hidden until the PWA sends a snapshot");
  hud.update(hudView(s), { lineId: 41, streaming: false });
  hud.update(hudView({ ...s, bpm: 80 }), { lineId: 41, streaming: false });
  assert.equal(frames.length, 1, "two updates, one frame");
  frames.shift()();
  assert.equal(doc.getElementById("hud").hidden, false);
  assert.equal(doc.documentElement.dataset.hud, "1");
  assert.equal(doc.getElementById("hud-level").textContent, "LV7");
  assert.equal(doc.getElementById("hud-heart").textContent, "80 bpm");
  assert.equal(doc.getElementById("hud-wearing-v").textContent, "Blazing Star");
  assert.equal(doc.getElementById("hud-wearing").hidden, false);
  assert.equal(doc.getElementById("hud-line").textContent, SNAP.line);
  assert.equal(doc.getElementById("hud-voice").hidden, false);
  assert.equal(doc.getElementById("hud-voice-label").textContent, "VOICE");
  // The same view again writes nothing at all (no layout work per message).
  doc.writes.length = 0;
  hud.update(hudView({ ...s, bpm: 80 }), { lineId: 41, streaming: false });
  frames.shift()();
  assert.deepEqual(doc.writes, []);
  // One field changes: one node is written.
  hud.update(hudView({ ...s, bpm: 80, spiking: true }), { lineId: 41, streaming: false });
  frames.shift()();
  assert.deepEqual(doc.writes, [["hud-heart-tag", "class", "hot", true]]);
  // Buttons: WEARING asks for her day, VOICE is his tap.
  doc.getElementById("hud-wearing").click();
  doc.getElementById("hud-voice").click();
  assert.deepEqual(taps, { voice: 1, herday: 1 });
});

test("status shows and clears; voice states relabel the button", () => {
  const doc = fakeDocument();
  const hud = createHud(doc, { schedule: (fn) => fn() });
  const s = { ...EMPTY_HUD, ...hudUpdate(msg(SNAP), ctx) };
  hud.update(hudView({ ...s, status: "Compiling tactical visualization, Commander..." }), { lineId: 41, streaming: false });
  const status = doc.getElementById("hud-status");
  assert.equal(status.hidden, false);
  assert.equal(status.textContent, "◢ Compiling tactical visualization, Commander...");
  assert.equal(doc.getElementById("hud-line").hidden, true);
  assert.equal(doc.getElementById("hud-voice").hidden, true);
  hud.update(hudView(s), { lineId: 41, streaming: false });
  assert.equal(status.hidden, true, "status null: it disappears");
  assert.equal(doc.getElementById("hud-line").hidden, false);
  hud.update(hudView(s, "loading"), { lineId: 41, streaming: false });
  assert.equal(doc.getElementById("hud-voice-label").textContent, VOICE_FACES.loading.label);
  assert.equal(doc.getElementById("hud-voice").dataset.state, "loading");
  hud.update(hudView(s, "playing"), { lineId: 41, streaming: false });
  assert.equal(doc.getElementById("hud-voice-label").textContent, "STOP");
  assert.equal(doc.getElementById("hud-voice").attrs["aria-label"], VOICE_FACES.playing.aria);
});

test("a new line starts at its top; a streaming line follows its tail; a long line can scroll", () => {
  const doc = fakeDocument();
  const hud = createHud(doc, { schedule: (fn) => fn() });
  const line = doc.getElementById("hud-line");
  const s = { ...EMPTY_HUD, ...hudUpdate(msg(SNAP), ctx) };
  // Content height per text (its box is 88 px tall).
  const heights = { "long one": 300, "long one, still": 300, "a new reply": 300, "a new reply, growing": 400 };
  line.heightFor = (text) => heights[text] ?? 40;
  hud.update(hudView({ ...s, line: "long one" }), { lineId: 1, streaming: false });
  assert.equal(line.scrollTop, 0);
  assert.equal(line.classList.contains("scrolls"), true, "a long line takes touches to scroll");
  line.scrollTop = 120;
  hud.update(hudView({ ...s, line: "long one, still" }), { lineId: 1, streaming: false });
  assert.equal(line.scrollTop, 120, "same finished line: his scroll position stays");
  hud.update(hudView({ ...s, line: "a new reply" }), { lineId: 2, streaming: true });
  assert.equal(line.scrollTop, 0, "a new reply starts at its top");
  line.scrollTop = 212; // at the bottom
  hud.update(hudView({ ...s, line: "a new reply, growing", streaming: true }), { lineId: 2, streaming: true });
  assert.equal(line.scrollTop, 312, "streaming: follows the tail while he is at the bottom");
  hud.update(hudView({ ...s, line: "short" }), { lineId: 3, streaming: false });
  assert.equal(line.classList.contains("scrolls"), false, "a short line lets touches through to her");
});

test("parked, the HUD holds its update and renders it when shown again", () => {
  const doc = fakeDocument();
  const frames = [];
  const hud = createHud(doc, { schedule: (fn) => frames.push(fn) });
  const s = { ...EMPTY_HUD, ...hudUpdate(msg(SNAP), ctx) };
  hud.hold(true);
  hud.update(hudView(s), { lineId: 41, streaming: false });
  assert.equal(frames.length, 0, "no frame while parked");
  hud.hold(false);
  assert.equal(doc.getElementById("hud-line").textContent, SNAP.line, "rendered at once when shown");
});

test("companion.html carries the HUD; the CSS draws it only embedded, without animation loops", () => {
  const html = source("flutter_app/web/companion.html");
  for (const id of ["hud", "hud-tags", "hud-level", "hud-mood", "hud-heart", "hud-heart-tag", "hud-wearing", "hud-wearing-v",
    "hud-card", "hud-status", "hud-line", "hud-voice", "hud-voice-icon", "hud-voice-label"]) {
    assert.match(html, new RegExp(`id="${id}"`), id);
  }
  assert.match(html, /<div class="hud" id="hud" hidden>/);
  assert.match(html, /<button[^>]*id="hud-wearing"[^>]*type="button"/);
  assert.match(html, /<button[^>]*id="hud-voice"[^>]*type="button"/);
  const css = source("flutter_app/web/companion/companion.css");
  assert.match(css, /\.hud \{ display: none; \}/);
  assert.match(css, /html\[data-embed\]\[data-hud\] \.hud:not\(\[hidden\]\) \{/);
  // Bundled fonts from the PWA's own assets; no third-party requests.
  assert.match(css, /url\("\.\.\/assets\/assets\/fonts\/Rajdhani-SemiBold\.ttf"\)/);
  assert.match(css, /url\("\.\.\/assets\/assets\/fonts\/JetBrainsMono-Regular\.ttf"\)/);
  assert.doesNotMatch(css, /https?:\/\//);
  // The HUD block has no infinite animations (iOS repaint flicker) and no backdrop blur over WebGL.
  const hudCss = css.slice(css.indexOf("/* ── Embedded HUD"));
  assert.ok(hudCss.length > 200, "the embedded HUD block exists");
  assert.doesNotMatch(hudCss, /infinite|animation:|backdrop-filter/);
});


test("first long reply is measured after the hidden HUD becomes visible", () => {
  const doc = fakeDocument({ hiddenLayout: true });
  const hud = createHud(doc, { schedule: (fn) => fn() });
  const line = doc.getElementById("hud-line");
  line.heightFor = () => 300;
  hud.update(hudView({ ...EMPTY_HUD, line: "Existing long reply" }), { lineId: 1 });
  assert.equal(doc.getElementById("hud").hidden, false);
  assert.equal(line.classList.contains("scrolls"), true);
  assert.equal(line.scrollTop, 0);
});

test("HUD resize remeasures unchanged text without rewriting it or moving its scroll", () => {
  const doc = fakeDocument();
  const hud = createHud(doc, { schedule: (fn) => fn() });
  const line = doc.getElementById("hud-line");
  line.heightFor = () => 200;
  line.clientHeight = 220;
  hud.update(hudView({ ...EMPTY_HUD, line: "Same reply" }), { lineId: 1 });
  assert.equal(line.classList.contains("scrolls"), false);
  line.clientHeight = 44;
  line.scrollTop = 30;
  doc.writes.length = 0;
  hud.resize();
  assert.equal(line.classList.contains("scrolls"), true);
  assert.equal(line.scrollTop, 30);
  assert.deepEqual(doc.writes, [["hud-line", "class", "scrolls", true]]);
  doc.writes.length = 0;
  hud.resize();
  assert.deepEqual(doc.writes, [], "repeated resize is idempotent");
  line.clientHeight = 220;
  hud.resize();
  assert.equal(line.classList.contains("scrolls"), false);
  assert.equal(line.textContent, "Same reply");
});

test("HUD reveal remeasures resized text even without a pending update", () => {
  const doc = fakeDocument();
  const hud = createHud(doc, { schedule: (fn) => fn() });
  const line = doc.getElementById("hud-line");
  line.heightFor = () => 70;
  hud.update(hudView({ ...EMPTY_HUD, line: "Existing reply" }), { lineId: 1 });
  hud.hold(true);
  line.clientHeight = 44;
  doc.writes.length = 0;
  hud.resize();
  assert.deepEqual(doc.writes, [], "parked HUD is not touched");
  hud.hold(false);
  assert.equal(line.classList.contains("scrolls"), true);
  assert.deepEqual(doc.writes, [["hud-line", "class", "scrolls", true]]);
});

test("HUD reveal remeasures even when only a heart update was pending", () => {
  const doc = fakeDocument();
  const hud = createHud(doc, { schedule: (fn) => fn() });
  const line = doc.getElementById("hud-line");
  line.heightFor = () => 70;
  const snapshot = { ...EMPTY_HUD, line: "Existing reply", bpm: 72 };
  hud.update(hudView(snapshot), { lineId: 1 });
  hud.hold(true);
  line.clientHeight = 44;
  hud.update(hudView({ ...snapshot, bpm: 75 }), { lineId: 1 });
  doc.writes.length = 0;
  hud.hold(false);
  assert.equal(line.classList.contains("scrolls"), true);
  assert.deepEqual(doc.writes, [["hud-heart", "text", "75 bpm"], ["hud-line", "class", "scrolls", true]]);
});
