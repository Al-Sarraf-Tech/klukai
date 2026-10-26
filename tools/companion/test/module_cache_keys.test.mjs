// Content keys propagate from a changed child through every importer to the
// HTML entry. Keep one canonical URL per module, especially shared modelCache.
// This models URL-keyed HTTP caching; it is not a physical-browser cache test.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';

const root = new URL('../../../flutter_app/web/', import.meta.url);
const origin = 'https://companion.invalid/app/';
const keyedModules = [
  'logic.js', 'music.js', 'portrait.js', 'ambience.js', 'voice.js',
  'stage_appearance.js', 'avatar3d.js', 'avatar/state.js',
  'avatar/runtime-presence.js', 'avatar/runtime-api.js',
  'avatar/runtime-motion.js', 'avatar/runtime.js',
];

function moduleReferences(source) {
  // Production imports use literal specifiers. Ignore full-line comments so
  // the avatar3d usage example is not mistaken for a self import.
  const code = source.replace(/^\s*\/\/.*$/gm, '').replace(/\/\*[\s\S]*?\*\//g, '');
  return [...code.matchAll(/\bfrom\s*['"]([^'"]+)['"]|\bimport\s*\(\s*['"]([^'"]+)['"]/g)]
    .map(match => match[1] || match[2]).filter(specifier => specifier.startsWith('.'));
}

function productionGraph() {
  const page = readFileSync(new URL('companion.html', root), 'utf8');
  const entry = page.match(/src="(companion\/app\.js\?v=[a-f0-9]+)"/)[1];
  const pending = [new URL(entry, origin)];
  const graph = new Map();
  while (pending.length) {
    const url = pending.pop();
    if (graph.has(url.href)) continue;
    const relative = url.pathname.slice('/app/'.length);
    const bytes = readFileSync(new URL(relative, root));
    graph.set(url.href, { url, relative, bytes });
    if (relative.includes('/vendor/')) continue;
    for (const reference of moduleReferences(bytes.toString('utf8'))) {
      const dependency = new URL(reference, url);
      if (dependency.pathname.endsWith('.js')) pending.push(dependency);
    }
  }
  return graph;
}

test('production modules have a single canonical URL including the shared model loader', () => {
  const urlsByPath = new Map();
  for (const { url } of productionGraph().values()) {
    const previous = urlsByPath.get(url.pathname);
    assert.ok(!previous || previous === url.href, `duplicate module identities: ${previous} and ${url.href}`);
    urlsByPath.set(url.pathname, url.href);
  }
  assert.equal(urlsByPath.get('/app/companion/avatar/loader.js'), `${origin}companion/avatar/loader.js`);
});

test('every content-keyed production import agrees with the current source bytes', () => {
  for (const { url, bytes } of productionGraph().values()) {
    const key = url.searchParams.get('v');
    if (!key) continue;
    assert.match(key, /^[a-f0-9]{8,12}$/);
    const actual = createHash('sha256').update(bytes).digest('hex').slice(0, key.length);
    assert.equal(key, actual, `refresh every importer key for ${url.pathname}`);
  }
});

test('changed companion modules and their ancestors all carry content keys', () => {
  const graph = [...productionGraph().values()];
  for (const path of keyedModules) {
    const node = graph.find(({ relative }) => relative === `companion/${path}`);
    assert.ok(node, `production graph must reach ${path}`);
    assert.ok(node.url.searchParams.get('v'), `${path} must not use a cached bare URL`);
  }
});

test('a warm HTTP cache of pre-fix bare child URLs cannot mask new companion code', () => {
  const leaves = ['logic.js', 'music.js', 'portrait.js', 'ambience.js'];
  const warmCache = new Map(leaves.map(path => [`${origin}companion/${path}`, 'previous release bytes']));
  const graph = [...productionGraph().values()];
  for (const path of leaves) {
    const requested = graph.find(({ relative }) => relative === `companion/${path}`).url.href;
    assert.equal(warmCache.has(requested), false, `${path} incorrectly reused a warm pre-fix URL`);
  }
});
