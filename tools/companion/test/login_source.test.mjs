import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";

const root = new URL("../../../", import.meta.url);

function text(path) {
  return readFileSync(new URL(path, root), "utf8");
}

test("login is owned by flutter web source and deploy copies it", () => {
  const source = text("flutter_app/web/login.html");
  const published = text("web-build/login.html");
  const deploy = text("scripts/deploy-web.sh");
  assert.equal(source, published);
  assert.equal(createHash("sha256").update(source).digest("hex").length, 64);
  assert.match(deploy, /cp "\$FLUTTER_APP\/web\/login\.html" "\$OUT\/login\.html"/);
  assert.doesNotMatch(deploy, /login_backup/);
  assert.match(source, /<html lang="en">/);
});
