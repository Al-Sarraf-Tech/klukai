// Vendors three.js for the companion's 3D window as ONE tree-shaken, minified ES
// module: flutter_app/web/companion/vendor/three-avatar.min.js. No CDN at
// runtime (privacy / Brave shields / offline), no import map, one request.
//
// The three.js exports are derived from the `THREE.<Name>` uses in avatar3d.js
// (plus the addons it needs), so the bundle can't drift from the viewer; a node
// test re-checks this. ~680 KB min / ~170 KB gzip vs ~980 KB / ~245 KB for the
// six unbundled files it replaces.
import { build } from 'esbuild';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../..');
const out = path.join(repo, 'flutter_app/web/companion/vendor');
const three = path.join(here, 'node_modules/three');
const viewer = path.join(repo, 'flutter_app/web/companion/avatar3d.js');

export const BUNDLE = 'three-avatar.min.js';
export const ADDONS = Object.freeze({
  GLTFLoader: 'three/examples/jsm/loaders/GLTFLoader.js',
  MeshoptDecoder: 'three/examples/jsm/libs/meshopt_decoder.module.js',
  OrbitControls: 'three/examples/jsm/controls/OrbitControls.js',
});

/** three.js core names the viewer uses (THREE.Foo), sorted. */
export function coreNamesUsed(src = fs.readFileSync(viewer, 'utf8')) {
  return [...new Set([...src.matchAll(/\bTHREE\.([A-Z][A-Za-z0-9_]*)/g)].map((m) => m[1]))].sort();
}

async function main() {
  const core = coreNamesUsed();
  const entry = [
    `export { ${core.join(', ')} } from 'three';`,
    ...Object.entries(ADDONS).map(([name, mod]) => `export { ${name} } from '${mod}';`),
  ].join('\n');
  fs.mkdirSync(out, { recursive: true });
  const version = JSON.parse(fs.readFileSync(path.join(three, 'package.json'))).version;
  await build({
    stdin: { contents: entry, resolveDir: here, sourcefile: 'three-avatar-entry.js', loader: 'js' },
    bundle: true, minify: true, format: 'esm', target: 'es2020', legalComments: 'inline',
    banner: { js: `/* three.js r${version} subset for avatar3d.js — MIT License, see LICENSE.three.txt. Built by tools/avatar/vendor.mjs. */` },
    outfile: path.join(out, BUNDLE), logLevel: 'warning',
  });
  fs.copyFileSync(path.join(three, 'LICENSE'), path.join(out, 'LICENSE.three.txt'));
  // Remove the previous unbundled files (superseded by the bundle).
  for (const old of ['three.module.min.js', 'GLTFLoader.js', 'BufferGeometryUtils.js', 'SkeletonUtils.js', 'OrbitControls.js', 'meshopt_decoder.module.js']) {
    fs.rmSync(path.join(out, old), { force: true });
  }
  for (const f of fs.readdirSync(out)) console.log(f.padEnd(24), fs.statSync(path.join(out, f)).size);
  console.log('exports:', [...core, ...Object.keys(ADDONS)].join(', '));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
