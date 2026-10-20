// The stage fetches her model once per stage load. Drives the real stage
// appearance and the real avatar module with a fake page and a fake API that
// counts model downloads. Node has no WebGL, so every viewer stops right
// after its download (the stage falls back to the portrait): exactly the part
// counted here.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createStageAppearance } from "../../../flutter_app/web/companion/stage_appearance.js";

function fakeEl() {
  return {
    style: {}, dataset: {}, hidden: false, value: "", textContent: "", disabled: false,
    appendChild() {}, remove() {}, addEventListener() {}, replaceChildren() {},
    removeAttribute() {}, showModal() {}, close() {},
  };
}

globalThis.document ??= {
  createElement: (tag) => (tag === "canvas" ? { getContext: () => null } : fakeEl()),
};

function stageWith(prefs) {
  const els = new Map();
  const $ = (id) => { if (!els.has(id)) els.set(id, fakeEl()); return els.get(id); };
  const downloads = [];
  const notices = [];
  const api = {
    models: async () => ({ models: [] }),
    model: (id, { signal } = {}) => { downloads.push({ id, signal }); return Promise.resolve(new ArrayBuffer(8)); },
  };
  const stage = createStageAppearance({
    $, api, prefs, savePrefs() {}, notice: (text) => notices.push(text),
    portrait: { setActive() {} }, stage: { dataset: {} },
    runtime: { avatar: null, state: { mood: "composed", thinking: false }, awake: true, hidden: false },
    syncButtons() {}, syncInsets() {},
  });
  return { stage, downloads, notices };
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
// The first import of the avatar module graph can take a few event-loop turns.
async function until(ready, turns = 200) {
  for (let i = 0; i < turns && !ready(); i++) await settle();
}

test("embedded boot: the load starts first and the warm-up joins it (one download)", async () => {
  const { stage, downloads } = stageWith({ mode: "model", appearance: "default" });
  const loading = stage.setMode("model"); // app.js, embed: setMode("model") …
  stage.warmModel();                      // … then the boot warm-up
  await loading;
  await settle();
  assert.deepEqual(downloads.map((d) => d.id), ["default"]);
  // Retrying 3D on this page (here: after the no-WebGL fallback) reuses the bytes.
  await stage.setMode("model");
  await settle();
  assert.equal(downloads.length, 1);
  stage.forgetModels();
  await settle();
});

test("full stage: the boot warm-up is the download the 3D view uses", async () => {
  const { stage, downloads } = stageWith({ mode: "model", appearance: "default" });
  stage.warmModel();
  await until(() => downloads.length > 0);
  assert.equal(downloads.length, 1);
  await stage.setMode("model");
  await settle();
  assert.equal(downloads.length, 1);
  assert.ok(downloads[0].signal instanceof AbortSignal, "the download gets the cache's signal");
  stage.forgetModels();
  await settle();
});

test("no warm-up while portrait is his view", async () => {
  const { stage, downloads } = stageWith({ mode: "portrait", appearance: "default" });
  stage.warmModel();
  await until(() => downloads.length > 0, 20);
  assert.equal(downloads.length, 0);
});

test("another appearance downloads its own model; his session ending drops it", async () => {
  const { stage, downloads } = stageWith({ mode: "model", appearance: "default" });
  await stage.setMode("model");
  await stage.setMode("model", "garf_dorm");
  await settle();
  assert.deepEqual(downloads.map((d) => d.id), ["default", "garf_dorm"]);
  stage.forgetModels(); // auth expired
  await settle();
  await stage.setMode("model", "garf_dorm");
  assert.deepEqual(downloads.map((d) => d.id), ["default", "garf_dorm", "garf_dorm"]);
  stage.forgetModels();
  await settle();
});

test("the avatar module has one import site, so the page has one model cache", async () => {
  // Two import URLs (say, ?v= hashes out of step) are two module instances,
  // each with its own cache: a second download per stage load.
  const { readFileSync } = await import("node:fs");
  const read = (name) => readFileSync(new URL(`../../../flutter_app/web/companion/${name}`, import.meta.url), "utf8");
  const app = read("app.js");
  const appearance = read("stage_appearance.js");
  assert.doesNotMatch(app, /avatar3d\.js/);
  assert.equal((appearance.match(/avatar3d\.js/g) || []).length, 1);
  assert.match(app, /warmModel\(\);/);
  assert.match(app, /const toLogin = \(\) => \{ clearToken\(\); forgetModel\(\); leave\(\); \};/);
});
