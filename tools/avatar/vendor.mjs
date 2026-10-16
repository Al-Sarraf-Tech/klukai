// Vendors three.js for the companion's 3D window as ONE tree-shaken, minified ES
// module: flutter_app/web/companion/vendor/three-avatar.min.js. No CDN at
// runtime (privacy / Brave shields / offline), no import map, one request.
//
// The three.js exports are derived from `THREE.<Name>` uses throughout the
// local module graph rooted at avatar3d.js and motion-library.js
// (plus the addons it needs), so the bundle can't drift from the viewer; a node
// test re-checks this. ~680 KB min / ~170 KB gzip vs ~980 KB / ~245 KB for the
// six unbundled files it replaces.
import { build } from 'esbuild';
import { parse, tokenizer } from 'acorn';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../..');
const out = path.join(repo, 'flutter_app/web/companion/vendor');
const three = path.join(here, 'node_modules/three');
const viewer = path.join(repo, 'flutter_app/web/companion/avatar3d.js');
const motion = path.join(repo, 'flutter_app/web/companion/motion-library.js');
const companion = path.dirname(viewer);

export const BUNDLE = 'three-avatar.min.js';
export const ADDONS = Object.freeze({
  GLTFLoader: 'three/examples/jsm/loaders/GLTFLoader.js',
  MeshoptDecoder: 'three/examples/jsm/libs/meshopt_decoder.module.js',
  OrbitControls: 'three/examples/jsm/controls/OrbitControls.js',
});

/** Acorn owns JavaScript syntax/trivia/escape handling. Walk every AST node,
 * including deferred function bodies, without optimizing away policy inputs. */
function moduleImports(source) {
  const pending = [parse(source, { ecmaVersion: 'latest', sourceType: 'module' })];
  const imports = [];
  while (pending.length) {
    const node = pending.pop();
    if (!node || typeof node !== 'object') continue;
    if (node.type === 'ImportExpression' &&
        (node.source.type !== 'Literal' || typeof node.source.value !== 'string')) {
      throw new Error('Viewer requires a local literal module import');
    }
    if (['ImportExpression', 'ImportDeclaration', 'ExportNamedDeclaration', 'ExportAllDeclaration'].includes(node.type) && node.source) {
      const specifier = node.source.value;
      if (typeof specifier !== 'string' || (!specifier.startsWith('./') && !specifier.startsWith('../'))) {
        throw new Error('Viewer requires a local module import');
      }
      imports.push(specifier);
    }
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) pending.push(...value);
      else if (value && typeof value === 'object') pending.push(value);
    }
  }
  return imports;
}

/** Read the local application graph once; generated vendor code is a material,
 * not an input to deriving its own export list. Canonical paths contain both
 * lexical traversal and symlink escapes, and the visited set closes cycles. */
export function viewerSources(entrypoints = [viewer, motion], { root = companion } = {}) {
  const boundary = fs.realpathSync(root);
  const seen = new Set();
  const sources = [];
  const visit = (requested) => {
    const file = fs.realpathSync(requested);
    const relative = path.relative(boundary, file);
    if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
      throw new Error('Viewer module is outside the application root');
    }
    if (relative.startsWith(`vendor${path.sep}`) || seen.has(file)) return;
    seen.add(file);
    const source = fs.readFileSync(file, 'utf8');
    sources.push({ file, source });
    for (const specifier of moduleImports(source)) {
      // Use the browser's URL resolution/percent-decoding rules before applying
      // canonical filesystem containment, including imports with query/hash.
      visit(fileURLToPath(new URL(specifier, pathToFileURL(file))));
    }
  };
  for (const entry of entrypoints) visit(entry);
  return sources;
}

/** Concrete THREE.Foo / THREE['Foo'] uses, sorted; lexer tokens exclude decoys.
 * Tokenizing also accepts callers' concatenated module source without imposing
 * a false shared binding scope on those independent modules. */
export function coreNamesUsed(src = viewerSources().map(({ source }) => source).join('\n')) {
  const tokens = [...tokenizer(src, { ecmaVersion: 'latest', sourceType: 'module' })];
  const names = new Set();
  for (let i = 0; i < tokens.length; i++) {
    if (tokens[i].type.label !== 'name' || tokens[i].value !== 'THREE') continue;
    const operator = tokens[i + 1]?.type.label;
    const property = tokens[i + 2];
    const direct = (operator === '.' || operator === '?.') && property?.type.label === 'name';
    const quoted = operator === '[' && property?.type.label === 'string' && tokens[i + 3]?.type.label === ']';
    if ((direct || quoted) && /^[A-Z][A-Za-z0-9_]*$/.test(property.value)) names.add(property.value);
  }
  return [...names].sort();
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
