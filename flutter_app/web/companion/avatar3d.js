// Klukai 3D avatar window: her rigged model idling and reacting while you chat.
//
// ES module, no bundler. three.js is vendored under ./vendor/ (no CDN — privacy,
// Brave shields, offline). The GLB is personal-use, game-derived content: it is
// never committed and only reaches the page through `loadModel()` (the app's
// authenticated API), never a public URL.
//
//   import { createAvatar3D, moodToState } from './avatar3d.js';
//   const av = await createAvatar3D(el, { loadModel: () => api.avatarGlb(), onReady, onError });
//   av.setMood('quietly_pleased'); av.setSpeaking(level); av.setState('greet');
//
// The pure parts (moodToState, STATE_CLIPS) import nothing heavy, so they run
// under `node --test` (tools/avatar/test). three.js is loaded lazily.

import { MOODS, nextBlink } from './logic.js';

/** Viewer states. */
export const STATES = Object.freeze([
  'idle', 'talking', 'thinking', 'happy', 'bashful', 'sad', 'greet', 'sleep', 'excited', 'flustered',
]);

/**
 * State → GLB clip names (first = primary; extra entries are variants picked at
 * random for variety). `once: true` plays one-shot then settles back to idle.
 */
export const STATE_CLIPS = Object.freeze({
  idle: { clips: ['idle'], loop: true },
  talking: { clips: ['talking'], loop: true },
  thinking: { clips: ['thinking'], loop: true },
  happy: { clips: ['happy', 'thankful'], loop: true },
  bashful: { clips: ['bashful'], loop: true },
  sad: { clips: ['defeated'], loop: true },
  greet: { clips: ['salute'], loop: false, once: true },
  sleep: { clips: ['sleep'], loop: true },
  excited: { clips: ['excited'], loop: true },
  flustered: { clips: ['nervous', 'bashful'], loop: true },
});

// Every one of her moods → a viewer state. Kept explicit (not a fallback) so a
// test can assert each backend mood was considered.
const MOOD_STATE = Object.freeze({
  composed: 'idle',
  focused: 'thinking',
  prideful: 'idle',
  exasperated: 'idle',
  protective: 'idle',
  quietly_pleased: 'happy',
  competitive: 'excited',
  tender: 'bashful',
  longing: 'sad',
  battle_ready: 'idle',
  flustered: 'flustered',
  affectionate: 'happy',
  shy: 'bashful',
  yearning: 'bashful',
  devoted: 'happy',
  passionate: 'excited',
  jealous: 'idle',
  possessive: 'idle',
  smitten: 'bashful',
  infatuated: 'bashful',
  vigilant: 'thinking',
  calculating: 'thinking',
  hunting: 'thinking',
  adrenaline: 'excited',
  scared: 'flustered',
  terrified: 'flustered',
  panicked: 'flustered',
  desperate: 'sad',
  relieved: 'happy',
  content: 'happy',
  playful: 'happy',
  drowsy: 'sleep',
  amused: 'happy',
  bored: 'idle',
  excited: 'excited',
  melancholic: 'sad',
  haunted: 'sad',
  conflicted: 'thinking',
  guilty: 'sad',
  determined: 'idle',
  grieving: 'sad',
  furious: 'idle',
  nostalgic: 'thinking',
  curious: 'thinking',
  irritated: 'idle',
  defiant: 'idle',
  vulnerable: 'bashful',
  grateful: 'happy',
  worried: 'flustered',
  embarrassed: 'flustered',
});

/** Pure: one of her moods → viewer state (unknown/empty → 'idle'). */
export function moodToState(mood) {
  if (typeof mood !== 'string') return 'idle';
  return MOOD_STATE[mood.trim().toLowerCase()] || 'idle';
}

/** Pure: moods the mapping covers (for tests / diagnostics). */
export function mappedMoods() {
  return Object.keys(MOOD_STATE);
}

/** Pure: does the shared MOODS list (logic.js) match this mapping exactly? */
export function moodCoverage() {
  const mapped = new Set(Object.keys(MOOD_STATE));
  return {
    missing: MOODS.filter((m) => !mapped.has(m)),
    extra: [...mapped].filter((m) => !MOODS.includes(m)),
  };
}

/** Pure: device-dependent renderer settings. */
export function renderSettings({ devicePixelRatio = 1, width = 1024, height = 768, isIOS = false } = {}) {
  const small = Math.min(width, height) < 500;
  return {
    pixelRatio: Math.min(devicePixelRatio || 1, small ? 1.5 : 2),
    antialias: !isIOS,
  };
}

/**
 * Pure: camera placement for a framing. Units are metres; she stands ~1.52 m
 * tall with the head bone at ~1.33-1.38 m. Leaves headroom for the cap.
 */
export function cameraFraming({ framing = 'upper', state = 'idle', aspect = 1, headY = 1.36 } = {}) {
  const fov = 26;
  const half = Math.tan((fov / 2) * Math.PI / 180); // visible half-height per metre
  const portrait = aspect < 0.8;
  if (state === 'sleep') { // lying on her back along −z (head at z≈−0.45): view from her side
    const dist = portrait ? 4.4 : 2.7;
    return { fov, targetY: 0.15, camY: portrait ? 1.5 : 1.05, dist, side: true, targetZ: 0.1 };
  }
  if (framing === 'full') {
    const dist = portrait ? 3.7 / Math.max(aspect / 0.5, 0.7) : 3.4;
    if (!portrait) return { fov, targetY: 0.79, camY: 0.95, dist: 3.35 };
    return { fov, targetY: 0.8, camY: 0.95, dist: Math.max(3.2, dist) };
  }
  // upper body: head + chest (landscape) / head to hips (portrait)
  const dist = portrait ? 1.9 : 1.35;
  const top = headY + 0.22; // cap crown + margin
  const targetY = top - dist * half;
  return { fov, targetY, camY: targetY + 0.06, dist };
}

export function detectIOS(nav = globalThis.navigator) {
  if (!nav) return false;
  const ua = nav.userAgent || '';
  return /iP(hone|ad|od)/.test(ua) || (/Macintosh/.test(ua) && (nav.maxTouchPoints || 0) > 1);
}

const ROLE_FALLBACK = (name) => (/hair/i.test(name) ? 'hair' : /face|eye/i.test(name) ? 'face' : /cloth/i.test(name) ? 'cloth' : 'skin');

/**
 * Create the avatar view inside `container`.
 * @param {HTMLElement} container
 * @param {{ loadModel: () => Promise<ArrayBuffer>, onReady?: Function, onError?: Function,
 *           framing?: 'upper'|'full', orbit?: boolean }} opts
 */
export async function createAvatar3D(container, opts = {}) {
  const { loadModel, onReady, onError } = opts;
  let framing = opts.framing === 'full' ? 'full' : 'upper';
  const fail = (err) => {
    try { onError?.(err instanceof Error ? err : new Error(String(err))); } catch { /* caller's handler threw */ }
  };
  const stub = {
    setState() {}, setMood() {}, setSpeaking() {}, setFraming() {}, resize() {}, dispose() {},
    get ready() { return false; }, get state() { return 'idle'; },
  };
  if (typeof loadModel !== 'function') { fail(new Error('createAvatar3D: loadModel() is required')); return stub; }
  if (!hasWebGL()) { fail(new Error('WebGL is not available')); return stub; }

  let THREE; let GLTFLoader; let MeshoptDecoder; let OrbitControls;
  try {
    THREE = await import('./vendor/three.module.min.js');
    ({ GLTFLoader } = await import('./vendor/GLTFLoader.js'));
    ({ MeshoptDecoder } = await import('./vendor/meshopt_decoder.module.js'));
    if (opts.orbit !== false) ({ OrbitControls } = await import('./vendor/OrbitControls.js'));
  } catch (e) { fail(e); return stub; }

  const isIOS = detectIOS();
  const size = () => ({ w: Math.max(1, container.clientWidth || 1), h: Math.max(1, container.clientHeight || 1) });
  let { w, h } = size();
  const rs = renderSettings({ devicePixelRatio: globalThis.devicePixelRatio, width: w, height: h, isIOS });

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: rs.antialias, alpha: true, premultipliedAlpha: true, powerPreference: 'low-power' });
  } catch (e) { fail(e); return stub; }
  renderer.setPixelRatio(rs.pixelRatio);
  renderer.setSize(w, h);
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  const canvas = renderer.domElement;
  canvas.style.display = 'block';
  canvas.style.width = '100%';
  canvas.style.height = '100%';
  canvas.style.touchAction = 'pan-y';
  canvas.setAttribute('aria-label', 'Klukai');
  container.appendChild(canvas);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(26, w / h, 0.05, 30);
  scene.add(new THREE.HemisphereLight(0xfff4f0, 0x6a6f8a, 1.6));
  const key = new THREE.DirectionalLight(0xffffff, 1.9);
  key.position.set(0.6, 2.2, 2.4);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xc8d8ff, 0.5);
  fill.position.set(-1.5, 1.2, 1.0);
  scene.add(fill);

  // ── state ────────────────────────────────────────────────────────────────
  let disposed = false;
  let ready = false;
  let paused = false;
  let rafId = 0;
  let root = null;
  let mixer = null;
  let skinned = [];
  let head = null;
  let neck = null;
  const actions = new Map();
  let current = null; // { state, action }
  let talkAction = null;
  let talkWeight = 0;
  let speaking = 0;
  let speakingTarget = 0;
  let blinkIdx = -1;
  let blinkT = 0; // ms since blink start; <0 = waiting
  let nextBlinkAt = 0;
  let pendingDouble = false;
  let controls = null;
  const clock = new THREE.Clock();
  const ownedTextures = new Set();

  // ── materials: toon + soft rim ───────────────────────────────────────────
  const ramp = (stops) => {
    const data = new Uint8Array(stops.length * 4);
    stops.forEach((v, i) => { data.set([v, v, v, 255], i * 4); });
    const t = new THREE.DataTexture(data, stops.length, 1, THREE.RGBAFormat);
    t.minFilter = THREE.NearestFilter; t.magFilter = THREE.NearestFilter; t.generateMipmaps = false; t.needsUpdate = true;
    ownedTextures.add(t);
    return t;
  };
  const bodyRamp = ramp([150, 215, 255]);
  const faceRamp = ramp([215, 255]);
  const rimUniforms = { uRimColor: { value: new THREE.Color(0xd9e8ff) }, uRimStrength: { value: 0.35 }, uRimPower: { value: 3.0 } };

  function toonFrom(src, role) {
    if (role === 'overlay') {
      return new THREE.MeshBasicMaterial({
        name: src.name, map: src.map, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false,
      });
    }
    const m = new THREE.MeshToonMaterial({
      name: src.name,
      map: src.map || null,
      color: src.map ? 0xffffff : src.color,
      gradientMap: role === 'face' ? faceRamp : bodyRamp,
      side: src.side,
    });
    if (role === 'cloth') { // win depth ties against the skin underneath
      m.polygonOffset = true; m.polygonOffsetFactor = -1; m.polygonOffsetUnits = -2;
    }
    m.onBeforeCompile = (shader) => {
      Object.assign(shader.uniforms, rimUniforms);
      shader.fragmentShader = shader.fragmentShader
        .replace('#include <common>', '#include <common>\nuniform vec3 uRimColor; uniform float uRimStrength; uniform float uRimPower;')
        .replace('#include <opaque_fragment>',
          'outgoingLight += uRimColor * pow(1.0 - saturate(dot(normal, geometryViewDir)), uRimPower) * uRimStrength;\n#include <opaque_fragment>');
    };
    m.customProgramCacheKey = () => `klukai-toon-${role}`;
    return m;
  }

  // ── load ─────────────────────────────────────────────────────────────────
  try {
    const buf = await loadModel();
    if (disposed) return stub;
    if (!(buf instanceof ArrayBuffer) && !ArrayBuffer.isView(buf)) throw new Error('loadModel() must resolve to an ArrayBuffer');
    const data = ArrayBuffer.isView(buf) ? buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) : buf;
    const loader = new GLTFLoader().setMeshoptDecoder(MeshoptDecoder);
    const gltf = await new Promise((resolve, reject) => loader.parse(data, '', resolve, reject));
    if (disposed) { disposeObject(gltf.scene); return stub; }
    root = gltf.scene;
    root.traverse((o) => {
      if (!o.isMesh) return;
      o.frustumCulled = false; // skinned bounds are bind-pose; avoid popping
      if (o.isSkinnedMesh) skinned.push(o);
      const mats = Array.isArray(o.material) ? o.material : [o.material];
      const next = mats.map((m) => {
        const role = m.userData?.role || ROLE_FALLBACK(m.name);
        const t = toonFrom(m, role);
        m.dispose();
        return t;
      });
      o.material = Array.isArray(o.material) ? next : next[0];
    });
    scene.add(root);
    head = root.getObjectByName('mixamorigHead');
    neck = root.getObjectByName('mixamorigNeck');
    for (const m of skinned) {
      const idx = m.morphTargetDictionary?.blink;
      if (idx !== undefined) blinkIdx = idx;
    }
    mixer = new THREE.AnimationMixer(root);
    for (const clip of gltf.animations) {
      const a = mixer.clipAction(clip);
      a.enabled = true;
      actions.set(clip.name, a);
    }
    mixer.addEventListener('finished', onFinished);
    talkAction = actions.get('talking') || null;
    if (talkAction) { talkAction.setEffectiveWeight(0); talkAction.play(); }
  } catch (e) {
    fail(e);
    teardown();
    return stub;
  }

  // ── camera framing + gentle orbit ────────────────────────────────────────
  const target = new THREE.Vector3();
  function frame() {
    root.updateMatrixWorld(true);
    const hp = new THREE.Vector3(0, 1.38, 0);
    head?.getWorldPosition(hp);
    const f = cameraFraming({ framing, state: stateName, aspect: w / h, headY: hp.y });
    const tz = f.targetZ || 0;
    target.set(0, f.targetY, tz);
    camera.fov = f.fov;
    if (f.side) camera.position.set(f.dist, f.camY, tz); else camera.position.set(0, f.camY, f.dist);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    camera.lookAt(target);
    if (controls) {
      controls.target.copy(target);
      const pol = Math.atan2(f.dist, f.camY - f.targetY);
      const az0 = f.side ? Math.PI / 2 : 0;
      controls.minAzimuthAngle = az0 - 0.45; controls.maxAzimuthAngle = az0 + 0.45;
      controls.minPolarAngle = pol - 0.22; controls.maxPolarAngle = pol + 0.12;
      controls.update();
    }
  }
  if (OrbitControls) {
    controls = new OrbitControls(camera, canvas);
    controls.enablePan = false;
    controls.enableZoom = false;
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.rotateSpeed = 0.5;
  }

  // ── animation control ────────────────────────────────────────────────────
  const FADE = 0.45;
  let stateName = 'idle';

  function pickClip(state) {
    const spec = STATE_CLIPS[state] || STATE_CLIPS.idle;
    const avail = spec.clips.filter((c) => actions.has(c));
    if (!avail.length) return null;
    return avail[Math.floor(Math.random() * avail.length)];
  }

  function play(state, { fade = FADE } = {}) {
    const spec = STATE_CLIPS[state] || STATE_CLIPS.idle;
    const clipName = pickClip(state) || pickClip('idle');
    const next = clipName ? actions.get(clipName) : null;
    if (!next) return;
    if (current?.action === next && next.isRunning()) { current.state = state; return; }
    next.reset();
    next.setLoop(spec.loop ? THREE.LoopRepeat : THREE.LoopOnce, spec.loop ? Infinity : 1);
    next.clampWhenFinished = !spec.loop;
    next.setEffectiveTimeScale(1);
    next.setEffectiveWeight(1);
    next.play();
    if (current?.action && current.action !== next) {
      current.action.crossFadeTo(next, fade, false);
    } else {
      next.fadeIn(fade);
    }
    current = { state, action: next, once: !!spec.once };
  }

  function onFinished(e) {
    if (current && e.action === current.action && current.once) play('idle');
  }

  play('idle', { fade: 0 });
  stateName = 'idle';
  frame();

  // ── loop ─────────────────────────────────────────────────────────────────
  function scheduleBlink(now) {
    const b = nextBlink();
    nextBlinkAt = now + b.delay;
    pendingDouble = b.double;
  }
  scheduleBlink(performance.now());
  blinkT = -1;

  function blinkValue(t) { // ms → 0..1..0 over ~140 ms (fast close, slower open)
    const close = 55; const hold = 25; const open = 90;
    if (t < close) return t / close;
    if (t < close + hold) return 1;
    if (t < close + hold + open) return 1 - (t - close - hold) / open;
    return -1;
  }

  const headTilt = new THREE.Euler();
  function tick() {
    rafId = 0;
    if (disposed || paused) return;
    const dt = Math.min(clock.getDelta(), 0.1);
    const now = performance.now();

    // speaking: smooth the level, layer the talking clip over the base state
    speaking += (speakingTarget - speaking) * Math.min(1, dt * 12);
    if (talkAction && stateName !== 'talking' && stateName !== 'sleep') {
      const goal = speaking > 0.04 ? Math.min(0.85, 0.35 + speaking) : 0;
      talkWeight += (goal - talkWeight) * Math.min(1, dt * 4);
      talkAction.setEffectiveWeight(talkWeight);
    } else if (talkAction) {
      talkWeight = 0;
      if (stateName !== 'talking') talkAction.setEffectiveWeight(0);
    }
    mixer.update(dt);

    // subtle head motion while speaking (applied after the mixer pose)
    if (head && speaking > 0.02) {
      const t = now / 1000;
      headTilt.set(Math.sin(t * 7.3) * 0.035 * speaking, Math.sin(t * 2.1) * 0.03 * speaking, Math.sin(t * 3.7) * 0.02 * speaking);
      head.rotation.x += headTilt.x; head.rotation.y += headTilt.y; head.rotation.z += headTilt.z;
    }

    // procedural blink on the "blink" morph target (none while asleep)
    if (blinkIdx >= 0) {
      let v = 0;
      if (stateName === 'sleep') v = 1;
      else if (blinkT >= 0) {
        blinkT += dt * 1000;
        v = blinkValue(blinkT);
        if (v < 0) {
          v = 0;
          if (pendingDouble) { pendingDouble = false; blinkT = -120; } else { blinkT = -1; scheduleBlink(now); }
        }
      } else if (blinkT < -1) { // gap before the second of a double blink
        blinkT += dt * 1000;
        if (blinkT >= -1) blinkT = 0;
      } else if (now >= nextBlinkAt) blinkT = 0;
      for (const m of skinned) if (m.morphTargetInfluences) m.morphTargetInfluences[blinkIdx] = v;
    }

    controls?.update();
    renderer.render(scene, camera);
    rafId = requestAnimationFrame(tick);
  }
  function start() { if (!rafId && !disposed && !paused) { clock.getDelta(); rafId = requestAnimationFrame(tick); } }
  function stop() { if (rafId) cancelAnimationFrame(rafId); rafId = 0; }

  // ── lifecycle ────────────────────────────────────────────────────────────
  const onVisibility = () => {
    paused = document.hidden || contextLost;
    if (paused) stop(); else start();
  };
  let contextLost = false;
  const onLost = (e) => { e.preventDefault(); contextLost = true; paused = true; stop(); };
  const onRestored = () => { contextLost = false; paused = document.hidden; frame(); start(); };
  document.addEventListener('visibilitychange', onVisibility);
  canvas.addEventListener('webglcontextlost', onLost, false);
  canvas.addEventListener('webglcontextrestored', onRestored, false);
  let ro = null;
  if (typeof ResizeObserver === 'function') { ro = new ResizeObserver(() => api.resize()); ro.observe(container); }

  function disposeObject(obj) {
    obj?.traverse?.((o) => {
      if (o.geometry) o.geometry.dispose();
      const mats = o.material ? (Array.isArray(o.material) ? o.material : [o.material]) : [];
      for (const m of mats) {
        for (const k of Object.keys(m)) { const v = m[k]; if (v && v.isTexture) v.dispose(); }
        m.dispose();
      }
    });
  }
  function teardown() {
    disposed = true;
    stop();
    document.removeEventListener('visibilitychange', onVisibility);
    canvas.removeEventListener('webglcontextlost', onLost);
    canvas.removeEventListener('webglcontextrestored', onRestored);
    ro?.disconnect();
    controls?.dispose();
    if (mixer) { mixer.removeEventListener('finished', onFinished); mixer.stopAllAction(); mixer.uncacheRoot(root); }
    disposeObject(root);
    for (const t of ownedTextures) t.dispose();
    renderer.dispose();
    try { renderer.forceContextLoss(); } catch { /* already lost */ }
    canvas.remove();
    actions.clear(); skinned = []; root = null; mixer = null;
  }

  const api = {
    get ready() { return ready; },
    get state() { return stateName; },
    setState(name) {
      if (disposed || !STATES.includes(name)) return;
      const prev = stateName;
      if (prev === 'talking' && name !== 'talking') talkWeight = 1; // fade the layer out, no pop
      stateName = name;
      play(name);
      if ((prev === 'sleep') !== (name === 'sleep')) frame();
    },
    setMood(mood) { api.setState(moodToState(mood)); },
    setSpeaking(level) {
      const v = Number(level);
      speakingTarget = Number.isFinite(v) ? Math.min(1, Math.max(0, v)) : 0;
    },
    setFraming(f) { framing = f === 'full' ? 'full' : 'upper'; if (root) frame(); },
    resize() {
      if (disposed) return;
      ({ w, h } = size());
      const s = renderSettings({ devicePixelRatio: globalThis.devicePixelRatio, width: w, height: h, isIOS });
      renderer.setPixelRatio(s.pixelRatio);
      renderer.setSize(w, h, false);
      frame();
      if (paused) renderer.render(scene, camera);
    },
    dispose() { if (!disposed) teardown(); },
  };

  ready = true;
  paused = typeof document !== 'undefined' && document.hidden;
  start();
  try { onReady?.(api); } catch { /* caller's handler threw */ }
  return api;
}

function hasWebGL() {
  try {
    const c = document.createElement('canvas');
    return !!(c.getContext('webgl2') || c.getContext('webgl'));
  } catch { return false; }
}
