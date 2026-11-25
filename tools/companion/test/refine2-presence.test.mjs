// The stage boots as one module graph. A content key that is missing, or that
// names an export the module does not provide, leaves the page blank: the
// browser keeps the previous URL and the new import fails.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { portraitRetryMs } from "../../../flutter_app/web/companion/stage_life.js";

const companion = new URL("../../../flutter_app/web/companion/", import.meta.url);

function source(name) {
  return readFileSync(new URL(name, companion), "utf8");
}

function contentKey(name) {
  return createHash("sha256").update(readFileSync(new URL(name, companion))).digest("hex").slice(0, 12);
}

function exportedNames(text) {
  const code = text.replace(/^\s*\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "");
  const names = new Set();
  for (const match of code.matchAll(/export\s+(?:async\s+)?function\s+(\w+)/g)) names.add(match[1]);
  for (const match of code.matchAll(/export\s+(?:const|let|var|class)\s+(\w+)/g)) names.add(match[1]);
  for (const match of code.matchAll(/export\s*\{([^}]+)\}/g)) {
    const after = code.slice(match.index + match[0].length, match.index + match[0].length + 40);
    if (/^\s*from\s*['"]/.test(after)) continue;
    for (const part of match[1].split(",")) {
      const bits = part.trim().split(/\s+as\s+/);
      if (bits[0]) names.add((bits[1] || bits[0]).trim());
    }
  }
  return names;
}

test("portraitRetryMs is loaded from the current stage_life module, not a cached copy", () => {
  const spec = `./stage_life.js?v=${contentKey("stage_life.js")}`;
  const app = source("app.js");
  const hud = source("hud.js");
  assert.equal(typeof portraitRetryMs, "function");
  assert.equal(exportedNames(source("stage_life.js")).has("portraitRetryMs"), true);
  assert.ok(app.includes(`portraitRetryMs`), "app.js asks for the portrait poll helper");
  assert.ok(app.includes(`from "${spec}"`), "app.js must not reuse an unkeyed stage_life.js");
  assert.ok(hud.includes(`from "${spec}"`), "hud.js must share that same module URL");
  assert.equal(app.includes('from "./stage_life.js";'), false);
  assert.equal(hud.includes('from "./stage_life.js";'), false);
});

test("every cache-busted companion import names an export that module provides", () => {
  const pending = ["app.js"];
  const seen = new Set();
  while (pending.length) {
    const relative = pending.pop();
    if (seen.has(relative)) continue;
    seen.add(relative);
    const code = source(relative).replace(/^\s*\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "");
    const specRe = /(?:import\s*([\s\S]*?)\s*from|export\s*\{([^}]+)\}\s*from)\s*['"](\.[^'"]+)['"]/g;
    const queueChild = (spec) => {
      const target = new URL(spec.split("?")[0], new URL(relative, companion));
      const child = target.pathname.slice(companion.pathname.length);
      if (child.endsWith(".js") && !child.includes("/vendor/")) pending.push(child);
      return child;
    };
    for (const dyn of code.matchAll(/import\s*\(\s*['"](\.[^'"]+)['"]\s*\)/g)) queueChild(dyn[1]);
    for (const match of code.matchAll(specRe)) {
      const spec = match[3];
      const child = queueChild(spec);
      if (!spec.includes("?v=")) continue;
      const clause = match[1] || "";
      const brace = /\{([\s\S]*)\}/.exec(clause) || (match[2] ? [null, match[2]] : null);
      if (!brace) continue;
      const provided = exportedNames(source(child));
      for (const part of brace[1].split(",")) {
        const imported = part.trim().split(/\s+as\s+/)[0].trim();
        if (!imported) continue;
        assert.ok(provided.has(imported), `${relative} imports ${imported} from ${spec}, which does not export it`);
      }
    }
  }
  assert.ok(seen.has("stage_life.js"));
});
