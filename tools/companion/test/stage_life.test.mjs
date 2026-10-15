import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  applySubtitle, completedAnnouncement, clearDecorativeMotion, beginWake, portraitStatusNotice,
} from "../../../flutter_app/web/companion/stage_life.js";

function box({ scrollTop, scrollHeight, clientHeight }) {
  return { scrollTop, scrollHeight, clientHeight, textContent: "" };
}

test("follow-tail does not steal a reader who scrolled up", () => {
  const el = box({ scrollTop: 10, scrollHeight: 800, clientHeight: 120 });
  const placed = applySubtitle(el, "a long reply that grew further");
  assert.equal(placed.followed, false);
  assert.equal(el.scrollTop, 10);
  assert.equal(el.textContent, "a long reply that grew further");
});

test("follow-tail stays with a reader who was near the bottom", () => {
  const el = box({ scrollTop: 680, scrollHeight: 800, clientHeight: 120 });
  const placed = applySubtitle(el, "the live tail");
  assert.equal(placed.followed, true);
  assert.equal(el.scrollTop, 800);
});

test("Latest returns to the live tail", () => {
  const el = box({ scrollTop: 0, scrollHeight: 900, clientHeight: 100 });
  const placed = applySubtitle(el, "latest", { jumpToLatest: true });
  assert.equal(placed.followed, true);
  assert.equal(el.scrollTop, 900);
});

test("reduced motion clears decorative transforms", () => {
  const motion = {
    style: { translate: "6px 3px", transform: "rotate(2deg)", rotate: "2deg" },
    classList: { removed: "", remove(name) { this.removed = name; } },
  };
  const root = { querySelectorAll: () => [motion] };
  assert.equal(clearDecorativeMotion(root), true);
  assert.equal(motion.style.translate, "0px 0px");
  assert.equal(motion.style.transform, "none");
  assert.equal(motion.style.rotate, "0deg");
  assert.equal(motion.classList.removed, "parallax");
});

test("tokens do not flood the polite announcement", () => {
  assert.equal(completedAnnouncement({ type: "token", text: "Hel" }), null);
  assert.equal(completedAnnouncement({ type: "thinking", text: "…" }), null);
  assert.equal(completedAnnouncement({ type: "done", text: "Hello there" }), "Hello there");
});

test("wake keeps visual mode when audio is missing", () => {
  const plan = beginWake();
  assert.equal(plan.visual, true);
  assert.equal(plan.hideOverlay, true);
  assert.match(plan.audioMissing, /Text still works/);
});

test("portrait failure text names no provider", () => {
  const text = portraitStatusNotice("unavailable", { hasFrames: true });
  assert.match(text, /off-camera/);
  assert.equal(/gpu|comfy|agy|grok/i.test(text), false);
});

test("the stage wires follow-tail, motion reset, and wake", () => {
  const app = readFileSync(new URL("../../../flutter_app/web/companion/app.js", import.meta.url), "utf8");
  const css = readFileSync(new URL("../../../flutter_app/web/companion/companion.css", import.meta.url), "utf8");
  const html = readFileSync(new URL("../../../flutter_app/web/companion.html", import.meta.url), "utf8");
  assert.match(app, /from "\.\/stage_life\.js"/);
  assert.match(app, /applySubtitle\(/);
  assert.match(app, /clearDecorativeMotion\(/);
  assert.match(app, /beginWake\(/);
  assert.doesNotMatch(app, /the GPU is busy/);
  assert.match(css, /prefers-reduced-motion:\s*reduce/);
  assert.match(css, /translate:\s*0 0/);
  assert.match(html, /id="btn-latest"/);
  assert.match(html, /id="reply-live"/);
  assert.match(html, /aria-label="Open the channel"/);
  assert.doesNotMatch(html, /maximum-scale=1/);
  assert.doesNotMatch(html, /id="subtitle"[^>]*aria-live/);
});
