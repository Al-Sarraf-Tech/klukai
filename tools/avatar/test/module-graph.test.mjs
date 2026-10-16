import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import http from 'node:http';
import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import * as avatar from '../../../flutter_app/web/companion/avatar3d.js';
import { viewerSources, coreNamesUsed } from '../vendor.mjs';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');

test('the public avatar module retains its original callable and constant interface', async () => {
  assert.deepEqual(Object.keys(avatar), [
    'CAMERA_ZOOM_RANGE', 'EYE_GUARD', 'LOOK_WEIGHT', 'MOOD_HOLD_MS', 'RIM_TINTS', 'SPRING',
    'STATES', 'STATE_CLIPS', 'STRONG_MOODS', 'blinkCurve', 'cameraFarPlane', 'cameraFraming',
    'closestOnSegment', 'collidePoint', 'coneClamp', 'createAvatar3D', 'createAvatarGesture',
    'createCameraView', 'createPassiveController', 'createReactionController', 'createStateMachine',
    'detectIOS', 'iosMajorVersion', 'lookSplit', 'mappedMoods', 'moodCoverage', 'moodGate',
    'moodToState', 'nextPixelRatio', 'nextQuality', 'nodCurve', 'parseTint', 'pickFidget',
    'poseJumped', 'renderSettings', 'softLimit', 'speechMouthDelta', 'speechMouthLevel',
    'springHang', 'springStep', 'springSubsteps', 'stepEnvelope', 'warmAvatar3D',
  ]);
  const errors = [];
  const stub = await avatar.createAvatar3D(null, { onError: (error) => errors.push(error.message) });
  assert.equal(stub.ready, false);
  assert.equal(stub.state, 'idle');
  assert.deepEqual(stub.getView(), { dTheta: 0, dPhi: 0, zoom: 1, panY: 0 });
  assert.equal(stub.react(), false);
  assert.deepEqual(errors, ['createAvatar3D: loadModel() is required']);
});

test('vendor discovery follows queried imports/reexports and cycles exactly once', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'klukai-module-graph-'));
  try {
    fs.writeFileSync(path.join(root, 'entry.js'), "import { boot } from './scene.js?v=pin'; export * from './controls.js'; void import('./late.js?revision=1');");
    fs.writeFileSync(path.join(root, 'scene.js'), "import './entry.js'; const renderer = new THREE.WebGLRenderer();");
    fs.writeFileSync(path.join(root, 'controls.js'), 'const ray = new THREE.Raycaster();');
    fs.writeFileSync(path.join(root, 'late.js'), 'const q = new THREE.Quaternion();');
    const sources = viewerSources([path.join(root, 'entry.js')], { root });
    assert.deepEqual(sources.map(({ file }) => path.basename(file)).sort(), ['controls.js', 'entry.js', 'late.js', 'scene.js']);
    assert.deepEqual(coreNamesUsed(sources.map(({ source }) => source).join('\n')), ['Quaternion', 'Raycaster', 'WebGLRenderer']);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('vendor discovery rejects escaped paths, symlinks and remote module origins', () => {
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'klukai-module-boundary-'));
  const root = path.join(temp, 'app'); fs.mkdirSync(root);
  const entry = path.join(root, 'entry.js');
  fs.writeFileSync(path.join(temp, 'outside.js'), 'new THREE.Texture();');
  try {
    fs.writeFileSync(entry, "import '../outside.js';");
    assert.throws(() => viewerSources([entry], { root }), /outside.*root/i);
    fs.symlinkSync(path.join(temp, 'outside.js'), path.join(root, 'linked.js'));
    fs.writeFileSync(entry, "export * from './linked.js';");
    assert.throws(() => viewerSources([entry], { root }), /outside.*root/i);
    fs.writeFileSync(entry, "import 'https://cdn.invalid/runtime.js';");
    assert.throws(() => viewerSources([entry], { root }), /local.*module/i);
    fs.writeFileSync(entry, "const selected = './linked.js'; import(selected);");
    assert.throws(() => viewerSources([entry], { root }), /local.*literal.*module/i);
  } finally { fs.rmSync(temp, { recursive: true, force: true }); }
});

test('the real viewer graph includes runtime materials without traversing generated vendor', () => {
  const sources = viewerSources();
  assert.ok(sources.some(({ file }) => file.endsWith('/avatar/runtime-materials.js')));
  assert.ok(sources.every(({ file }) => !file.includes('/vendor/')));
  const names = coreNamesUsed();
  for (const expected of ['WebGLRenderer', 'SkinnedMesh', 'ShaderMaterial', 'Quaternion', 'Raycaster']) {
    assert.ok(names.includes(expected), `missing runtime export ${expected}`);
  }
  assert.ok(sources.every(({ file }) => file.startsWith(path.join(repo, 'flutter_app/web/companion/'))));
});

// Literal imports are a source policy, so syntax trivia and quoted decoys
// must not change admission or hide a runtime dependency.
test('import syntax trivia and escaped literals retain the complete graph', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'klukai-import-syntax-'));
  const entry = path.join(root, 'entry.js');
  fs.writeFileSync(path.join(root, 'late.js'), 'new THREE.Texture();');
  try {
    for (const source of [
      "void import ('./late.js');", "void import\n('./late.js');",
      "void import/* gap */('./late.js');", "void import( /* before */ './late.js' /* after */ ,);",
      "import /* gap */ './late.js';", "export /* gap */ * /* gap */ from /* gap */ './late.js';",
      String.raw`void import ('./l\u0061te.js');`,
      "void import ('./late.js?version=1#module');",
    ]) {
      fs.writeFileSync(entry, source);
      assert.deepEqual(viewerSources([entry], { root }).map(({ file }) => path.basename(file)).sort(), ['entry.js', 'late.js'], source);
    }
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('literal import policy rejects remote and expression imports with every trivia form', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'klukai-import-rejection-'));
  const entry = path.join(root, 'entry.js');
  try {
    for (const source of [
      "void import ('https://cdn.invalid/remote.js');",
      "void import\n/* comment */('https://cdn.invalid/remote.js');",
      String.raw`void import ('\u0068ttps://cdn.invalid/remote.js');`,
      "const target='./late.js'; void import (target);",
      "const target='./late.js'; void import /* comment */ (target);",
      "void import ('./late.js' + '');", "void import (`./late.js`);",
      "const name='late'; void import (`./${name}.js`);",
      "if (false) import ('https://cdn.invalid/unreachable.js');",
    ]) {
      fs.writeFileSync(entry, source);
      assert.throws(() => viewerSources([entry], { root }), /local.*(?:literal.*)?module/i, source);
    }
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('missing and URL-encoded escaped modules fail closed before bundling', () => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'klukai-import-url-'));
  const root = path.join(parent, 'app'); fs.mkdirSync(root);
  const entry = path.join(root, 'entry.js');
  fs.writeFileSync(path.join(parent, 'outside.js'), 'new THREE.Texture();');
  try {
    fs.writeFileSync(entry, "void import /* gap */ ('./missing.js');");
    assert.throws(() => viewerSources([entry], { root }), /ENOENT/);
    fs.writeFileSync(entry, "void import ('./%2e%2e/outside.js');");
    assert.throws(() => viewerSources([entry], { root }), /outside.*root/i);
    fs.writeFileSync(entry, 'void import (');
    assert.throws(() => viewerSources([entry], { root }), SyntaxError);
  } finally { fs.rmSync(parent, { recursive: true, force: true }); }
});

test('import syntax in comments and string or regex literals is inert', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'klukai-import-decoy-'));
  const entry = path.join(root, 'entry.js');
  try {
    fs.writeFileSync(entry, String.raw`
      // import ('https://cdn.invalid/comment.js');
      /* import('./missing.js'); */
      const text = "import('https://cdn.invalid/text.js')";
      const template = \`import ('https://cdn.invalid/template.js')\`;
      const pattern = /import\('https:\/\/cdn.invalid\/regex.js'\)/;
    `.replaceAll('\\`', '`'));
    assert.deepEqual(viewerSources([entry], { root }).map(({ file }) => path.basename(file)), ['entry.js']);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('concrete THREE symbol union includes quoted properties and ignores textual decoys', () => {
  assert.deepEqual(coreNamesUsed(`
    const decoy = 'THREE.Forbidden'; // THREE.AlsoForbidden
    new THREE['Texture'](); new THREE.Quaternion();
  `), ['Quaternion', 'Texture']);
});

// A synthetic rig/triangle exercises the actual browser module graph, material
// compilation, procedural bones/springs, camera API and disposal. No account,
// backend, personal model or image provider participates in this fixture.
function rigFixture() {
  const geometry = new Float32Array([
    -0.2, 0, 0, 0.2, 0, 0, 0, 0.4, 0,
    0, 0, 1, 0, 0, 1, 0, 0, 1,
    0, 1,
    0, 0.8, 0, 0, 0.8, 0,
  ]);
  return {
    asset: { version: '2.0' }, scene: 0,
    scenes: [{ nodes: [0], extras: {
      springChains: [{ name: 'fixture_long_hair', bones: ['fixture_hair', 'fixture_tip'], tail: [0, -0.1, 0] }],
      springColliders: [{ bone: 'mixamorigHead', radius: 0.08 }],
    } }],
    nodes: [
      { name: 'fixture_root', children: [1, 6] },
      { name: 'mixamorigHips', translation: [0, 0.8, 0], children: [2] },
      { name: 'mixamorigNeck', translation: [0, 0.4, 0], children: [3] },
      { name: 'mixamorigHead', translation: [0, 0.16, 0], children: [4] },
      { name: 'fixture_hair', translation: [0, 0.1, 0], children: [5] },
      { name: 'fixture_tip', translation: [0, -0.12, 0] },
      { mesh: 0, translation: [0, 0.8, 0] },
    ],
    buffers: [{ byteLength: geometry.byteLength, uri: `data:application/octet-stream;base64,${Buffer.from(geometry.buffer).toString('base64')}` }],
    bufferViews: [
      { buffer: 0, byteOffset: 0, byteLength: 36 }, { buffer: 0, byteOffset: 36, byteLength: 36 },
      { buffer: 0, byteOffset: 72, byteLength: 8 }, { buffer: 0, byteOffset: 80, byteLength: 24 },
    ],
    accessors: [
      { bufferView: 0, componentType: 5126, count: 3, type: 'VEC3', min: [-0.2, 0, 0], max: [0.2, 0.4, 0] },
      { bufferView: 1, componentType: 5126, count: 3, type: 'VEC3' },
      { bufferView: 2, componentType: 5126, count: 2, type: 'SCALAR', min: [0], max: [1] },
      { bufferView: 3, componentType: 5126, count: 2, type: 'VEC3' },
    ],
    meshes: [{ primitives: [{ attributes: { POSITION: 0, NORMAL: 1 }, material: 0 }] }],
    materials: [{ name: 'fixture_cloth', doubleSided: true }],
    animations: [{
      name: 'idle', channels: [{ sampler: 0, target: { node: 1, path: 'translation' } }],
      samplers: [{ input: 2, output: 3, interpolation: 'LINEAR' }],
    }],
  };
}

test('isolated Chrome loads the real rig, preserves camera/state and releases WebGL', { timeout: 30000 }, async (t) => {
  const root = path.resolve(process.env.KLUKAI_AVATAR_TEST_ROOT || path.join(repo, 'flutter_app/web/companion'));
  const requests = [];
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, 'http://127.0.0.1').pathname;
    requests.push(pathname);
    if (pathname === '/') {
      response.setHeader('content-type', 'text/html');
      response.end('<div id="host" style="width:500px;height:700px"></div><canvas id="message"></canvas><script src="/pretext_bridge.js"></script><script type="module">import * as avatar from "/avatar3d.js"; import * as pretext from "/pretext/layout.js"; window.__pretext = pretext; window.fixtureAvatar = avatar;</script>');
      return;
    }
    const fileRoot = pathname.startsWith('/pretext/') || pathname === '/pretext_bridge.js'
      ? path.resolve(root, '../js') : root;
    const file = path.resolve(fileRoot, `.${pathname}`);
    if (!file.startsWith(`${fileRoot}${path.sep}`) || !fs.existsSync(file)) {
      response.writeHead(404); response.end(); return;
    }
    response.setHeader('content-type', pathname.endsWith('.json') ? 'application/json' : 'text/javascript');
    fs.createReadStream(file).pipe(response);
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'klukai-avatar-browser-'));
  const browser = spawn(process.env.KLUKAI_CHROME_BINARY || '/usr/bin/google-chrome', [
    '--headless', '--no-sandbox', '--password-store=basic', '--remote-debugging-port=0',
    '--disable-background-networking', '--disable-component-update', '--disable-sync',
    '--disable-extensions', '--no-first-run', '--no-default-browser-check',
    '--use-angle=swiftshader', `--user-data-dir=${profile}`,
    `http://127.0.0.1:${server.address().port}/`,
  ], { stdio: ['ignore', 'ignore', 'pipe'], detached: true });
  const exited = new Promise((resolve) => browser.once('exit', resolve));
  let stderr = ''; browser.stderr.on('data', (chunk) => { stderr += chunk; });
  let socket;
  t.after(async () => {
    socket?.close();
    try { process.kill(-browser.pid, 'SIGTERM'); } catch { /* already exited */ }
    await Promise.race([exited, delay(1000)]);
    if (browser.exitCode === null && browser.signalCode === null) {
      try { process.kill(-browser.pid, 'SIGKILL'); } catch { /* already exited */ }
      await exited;
    }
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    fs.rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
  });
  const deadline = Date.now() + 10000;
  const portFile = path.join(profile, 'DevToolsActivePort');
  while (!fs.existsSync(portFile) && Date.now() < deadline) await delay(25);
  assert.ok(fs.existsSync(portFile), `Chrome did not start: ${stderr}`);
  const port = fs.readFileSync(portFile, 'utf8').split('\n')[0];
  const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const page = pages.find((entry) => entry.type === 'page');
  assert.ok(page, 'Chrome must expose the fixture page');
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); });
  let nextId = 0; const pending = new Map();
  socket.addEventListener('message', ({ data }) => {
    const message = JSON.parse(data);
    if (!pending.has(message.id)) return;
    const { resolve, reject, timer } = pending.get(message.id); pending.delete(message.id); clearTimeout(timer);
    if (message.error) reject(new Error(JSON.stringify(message.error))); else resolve(message.result);
  });
  const evaluate = async (expression) => {
    const id = ++nextId;
    const result = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(id); reject(new Error('Browser evaluation timed out')); }, 10000);
      pending.set(id, { resolve, reject, timer });
      socket.send(JSON.stringify({ id, method: 'Runtime.evaluate', params: { expression, awaitPromise: true, returnByValue: true } }));
    });
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };
  while (!(await evaluate('Boolean(window.fixtureAvatar)')) && Date.now() < deadline) await delay(25);
  assert.equal(await evaluate('Boolean(window.fixtureAvatar)'), true, `Module failed to load: ${stderr}`);
  const result = await evaluate(`(async () => {
    const errors = [];
    const av = await window.fixtureAvatar.createAvatar3D(document.getElementById('host'), {
      loadModel: async () => new TextEncoder().encode(JSON.stringify(${JSON.stringify(rigFixture())})).buffer,
      orbit: false, debug: true, adaptiveResolution: false, onError: (e) => errors.push(e.message)
    });
    if (!av.ready) return { ready: false, errors };
    av.setPaused(true);
    av.setState('thinking'); const thinking = av.state;
    av.setMood('tender'); const tender = av.state;
    av.setView({ zoom: 0.7, dTheta: 0.4, dPhi: 0, panY: 0.1 });
    const before = av.getView();
    const probe = av.debug.run(0.2, { speaking: 0.6 });
    av.resize(); const after = av.getView();
    const materials = av.debug.materials().length;
    const draws = av.stats.drawCalls;
    const handle = window.pretextBridge.prepare('Ready, Commander.', '15px monospace');
    const layout = window.pretextBridge.layout(handle, 110, 21);
    const rendered = window.pretextBridge.renderToCanvas(handle, 'message', 110, 21, '#ffffff');
    const message = document.getElementById('message');
    const pixels = message.getContext('2d').getImageData(0, 0, message.width, message.height).data.some((value, index) => index % 4 === 3 && value > 0);
    window.pretextBridge.release(handle);
    const released = window.pretextBridge.layout(handle, 110, 21);
    av.dispose(); av.dispose();
    return { ready: true, errors, thinking, tender, before, after, materials, draws,
      chains: probe.springs?.chains, canvases: document.querySelectorAll('#host canvas').length,
      pretext: { ...layout, rendered, pixels, released } };
  })()`);
  assert.equal(result.ready, true, JSON.stringify(result));
  assert.deepEqual(result.errors, []);
  assert.equal(result.thinking, 'thinking');
  assert.equal(result.tender, 'bashful');
  assert.deepEqual(result.before, { dTheta: 0.4, dPhi: 0, zoom: 0.7, panY: 0.1 });
  assert.deepEqual(result.after, result.before, 'resize must retain the chosen camera');
  assert.ok(result.materials > 0, 'the fixture must exercise toon material compilation');
  assert.ok(result.draws > 0, 'a real WebGL frame must render');
  assert.deepEqual(result.chains, ['fixture_long_hair']);
  assert.equal(result.canvases, 0, 'dispose must release the host canvas');
  assert.deepEqual(result.pretext, { lineCount: 2, height: 42, rendered: true, pixels: true, released: null });
  assert.ok(requests.includes('/vendor/three-avatar.min.js'), 'Three must load from the same origin');
});
