// Vendors three.js + the addons avatar3d.js needs into flutter_app/web/companion/vendor/.
// No CDN at runtime (privacy / Brave shields / offline). Bare 'three' imports are
// rewritten to the relative minified module so no import map is required.
import { build } from 'esbuild';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../..');
const out = path.join(repo, 'flutter_app/web/companion/vendor');
const three = path.join(here, 'node_modules/three');
fs.mkdirSync(out, { recursive: true });

// three r163+ ships three.module.js + three.core.js and no .min build; bundle+minify into one file.
await build({
  entryPoints: [path.join(three, 'build/three.module.js')],
  bundle: true, minify: true, format: 'esm', target: 'es2020', legalComments: 'inline',
  outfile: path.join(out, 'three.module.min.js'), logLevel: 'warning',
});

// Addons: copy, flatten relative imports, point 'three' at the vendored module.
const addons = {
  'GLTFLoader.js': 'examples/jsm/loaders/GLTFLoader.js',
  'BufferGeometryUtils.js': 'examples/jsm/utils/BufferGeometryUtils.js',
  'SkeletonUtils.js': 'examples/jsm/utils/SkeletonUtils.js',
  'OrbitControls.js': 'examples/jsm/controls/OrbitControls.js',
  'meshopt_decoder.module.js': 'examples/jsm/libs/meshopt_decoder.module.js',
};
for (const [name, rel] of Object.entries(addons)) {
  let src = fs.readFileSync(path.join(three, rel), 'utf8');
  src = src.replace(/from\s+'three'/g, "from './three.module.min.js'");
  src = src.replace(/from\s+'\.\.\/utils\/([A-Za-z]+\.js)'/g, "from './$1'");
  const banner = `// Vendored from three@${JSON.parse(fs.readFileSync(path.join(three, 'package.json'))).version} (${rel}) by tools/avatar/vendor.mjs. MIT License.\n`;
  fs.writeFileSync(path.join(out, name), banner + src);
}
fs.copyFileSync(path.join(three, 'LICENSE'), path.join(out, 'LICENSE.three.txt'));
for (const f of fs.readdirSync(out)) console.log(f.padEnd(28), fs.statSync(path.join(out, f)).size);
