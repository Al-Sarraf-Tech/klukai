import { installMaterials } from './runtime-materials.js';
import { initializeMotion, installMotion } from './runtime-motion.js';
import { initializePresence, installPresence } from './runtime-presence.js';
import { initializeSprings, installSprings } from './runtime-springs.js';
import { initializeCamera, installCamera } from './runtime-camera.js';
import { initializeLifecycle, initializeLoop, installLifecycle } from './runtime-lifecycle.js';
import { BASE_FOV, cameraFarPlane, createCameraView, detectIOS, iosMajorVersion, renderSettings } from './camera.js';
import { importLibs, modelPrefetch } from './loader.js';
import { awaitWithAbort } from '../appearance.js';
import { LIGHT } from './shaders.js';
import { RIM_TINTS } from './state.js';
import { loadScene } from './runtime-load.js';
import { createPublicApi } from './runtime-api.js';

// One runtime context owns renderer, model, animation, input and disposal.
export async function createAvatar3D(container, opts = {}) {
  const ctx = { container, opts };
  installMaterials(ctx);
  installMotion(ctx);
  installPresence(ctx);
  installSprings(ctx);
  installCamera(ctx);
  installLifecycle(ctx);

  ({ loadModel: ctx.loadModel, onReady: ctx.onReady, onError: ctx.onError } = ctx.opts);
  ctx.framing = ctx.opts.framing === 'full' ? 'full' : 'upper';
  ctx.fail = (err) => {
    try { ctx.onError?.(err instanceof Error ? err : new Error(String(err))); } catch { /* caller's handler threw */ }
  };
  // Returned whenever the view can't be built; `ready` is false so the caller
  // can tell it from a live avatar (and try again later instead of keeping it).
  ctx.stub = {
    setState() {}, setMood() {}, setSpeaking() {}, setFraming() {}, setPaused() {}, setTint() {}, setInsets() {},
    zoomBy() {}, rotateBy() {}, moveBy() {}, resetView() {}, setView() {}, getView() { return createCameraView().state; }, react() { return false; }, getReactions() { return []; },
    resize() {}, dispose() {},
    get ready() { return false; }, get state() { return 'idle'; },
  };
  if (typeof ctx.loadModel !== 'function') { ctx.fail(new Error('createAvatar3D: loadModel() is required')); return ctx.stub; }

  ctx.stats = { t0: performance.now() };
  ctx.mark = (k) => { ctx.stats[k] = Math.round(performance.now() - ctx.stats.t0); };
  // Model download and library imports run in parallel (and may already be
  // in flight from warmAvatar3D()).
  ctx.modelPromise = modelPrefetch.take(ctx.opts.modelKey || 'default', ctx.loadModel);
  ctx.modelPromise.then(() => ctx.mark('fetchMs'), () => {});
  ctx.THREE = undefined; ctx.GLTFLoader = undefined; ctx.MeshoptDecoder = undefined; ctx.OrbitControls = undefined;
  try {
    ({ THREE: ctx.THREE, GLTFLoader: ctx.GLTFLoader, MeshoptDecoder: ctx.MeshoptDecoder, OrbitControls: ctx.OrbitControls } = await importLibs());
    if (ctx.opts.orbit === false) ctx.OrbitControls = null;
    ctx.mark('importMs');
  } catch (e) { ctx.modelPromise.catch(() => {}); ctx.fail(e); return ctx.stub; }

  // A superseded or stalled download must not retain a WebGL context.
  ctx.modelBuffer = undefined;
  try { ctx.modelBuffer = await awaitWithAbort(ctx.modelPromise, ctx.opts.signal); }
  catch (e) { ctx.fail(e); return ctx.stub; }
  if (ctx.opts.signal?.aborted) return ctx.stub;
  if (!hasWebGL()) { ctx.fail(new Error('WebGL is not available')); return ctx.stub; }

  ctx.isIOS = detectIOS();
  ctx.size = () => ({ w: Math.max(1, ctx.container.clientWidth || 1), h: Math.max(1, ctx.container.clientHeight || 1) });
  ({ w: ctx.w, h: ctx.h } = ctx.size());
  ctx.iosMajor = ctx.isIOS ? iosMajorVersion() : 0;
  ctx.rs = renderSettings({ devicePixelRatio: globalThis.devicePixelRatio, width: ctx.w, height: ctx.h, isIOS: ctx.isIOS, iosMajor: ctx.iosMajor });

  ctx.renderer = undefined;
  try {
    ctx.renderer = new ctx.THREE.WebGLRenderer({ antialias: ctx.rs.antialias, alpha: true, premultipliedAlpha: true, powerPreference: 'low-power' });
  } catch (e) { ctx.fail(e); return ctx.stub; }
  ctx.renderer.setPixelRatio(ctx.rs.pixelRatio);
  ctx.renderer.setSize(ctx.w, ctx.h);
  ctx.renderer.setClearColor(0x000000, 0);
  ctx.renderer.outputColorSpace = ctx.THREE.SRGBColorSpace; // NoToneMapping (default): albedo 1:1 at "fully lit"
  ctx.canvas = ctx.renderer.domElement;
  ctx.canvas.style.display = 'block';
  ctx.canvas.style.width = '100%';
  ctx.canvas.style.height = '100%';
  ctx.canvas.style.touchAction = 'none';
  ctx.canvas.setAttribute('aria-label', 'Klukai');
  ctx.container.appendChild(ctx.canvas);

  ctx.scene = new ctx.THREE.Scene();
  ctx.camera = new ctx.THREE.PerspectiveCamera(BASE_FOV, ctx.w / ctx.h, 0.05, cameraFarPlane());
  // Two lights only (cheaper per pixel on phones), calibrated so the lit side
  // is ~1.0× albedo: a dim sky/ground fill and one warm key from the front-left
  // (keeps her face in its lit band). The back rim is a shader term (uRimDir).
  ctx.hemi = new ctx.THREE.HemisphereLight(LIGHT.sky, LIGHT.ground, LIGHT.hemi);
  ctx.scene.add(ctx.hemi);
  ctx.key = new ctx.THREE.DirectionalLight(LIGHT.key, LIGHT.keyIntensity);
  ctx.key.position.set(...LIGHT.keyPos);
  ctx.scene.add(ctx.key);
  ctx.skyBase = new ctx.THREE.Color(LIGHT.sky);

  // ── state ────────────────────────────────────────────────────────────────
  ctx.disposed = false;
  ctx.ready = false;
  ctx.paused = false;
  ctx.userPaused = false; // api.setPaused(): parked behind another view
  ctx.contextLost = false;
  // Assigned in the lifecycle section below, after the model loads — but
  // teardown() also runs from the load `catch`. Declared here (not `const`
  // down there) or that call hit the temporal dead zone, threw, and the
  // renderer/canvas were never disposed: one leaked WebGL context per retry.
  ctx.onVisibility = null;
  ctx.onLost = null;
  ctx.onRestored = null;
  ctx.onPointer = null;
  ctx.onPointerLeave = null;
  ctx.onMotionPref = null;
  ctx.onControlStart = null;
  ctx.onControlEnd = null;
  ctx.onViewPointerDown = null;
  ctx.onViewPointerMove = null;
  ctx.onViewPointerUp = null;
  ctx.onViewPointerCancel = null;
  ctx.viewNotifyTimer = 0;
  ctx.ro = null;
  ctx.rafId = 0;
  ctx.frameAcc = 0; ctx.frameN = 0; ctx.fpsAvg = 0; ctx.lastNow = 0; // adaptive resolution
  ctx.root = null;
  ctx.mixer = null;
  ctx.skinned = [];
  ctx.outlineMeshes = [];
  ctx.outlinesOn = ctx.opts.outlines !== false;
  ctx.head = null;
  ctx.neck = null;
  ctx.headJoint = false;
  ctx.bones = {};
  ctx.actions = new Map();
  ctx.fidgetActions = new Map();
  ctx.current = null; // { state, action, once, fidget }
  ctx.talkAction = null;
  ctx.talkWeight = 0;
  ctx.speaking = 0;
  ctx.speakingTarget = 0;
  ctx.mouthOpen = 0;
  ctx.mouthMode = 'none';
  ctx.mouthTargets = [];
  ctx.reactions = null;
  ctx.passives = null;
  ctx.motionCatalog = [];
  ctx.motionLibraryStatus = 'loading';
  ctx.blinkIdx = -1;
  ctx.blinkT = -1; // ms since blink start; <0 = waiting
  ctx.blinkSlow = false;
  ctx.lastBlinkAt = 0;
  ctx.nextBlinkAt = 0;
  ctx.pendingDouble = false;
  ctx.controls = null;
  ctx.stateName = 'idle'; // mirrors sm.state (createStateMachine below)
  ctx.insetsPx = { top: 0, bottom: 0 };
  ctx.shadowMesh = null;
  ctx.springs = null; // hair spring chains (buildSprings), null without chains
  ctx.simClock = performance.now() / 1000; // debug.run()'s synthetic clock (s)
  ctx.timer = new ctx.THREE.Timer();
  ctx.timer.connect?.(document);
  ctx.ownedTextures = new Set();
  ctx.life = ctx.opts.life !== false;
  ctx.motionMq = typeof globalThis.matchMedia === 'function' ? globalThis.matchMedia('(prefers-reduced-motion: reduce)') : null;
  ctx.reducedMotion = ctx.opts.reducedMotion === true || Boolean(ctx.motionMq?.matches);

  // ── materials ────────────────────────────────────────────────────────────
  ctx.shared = {
    uHeadCenter: { value: new ctx.THREE.Vector3(0, 1.38, 0) },
    uRimColor: { value: new ctx.THREE.Color(RIM_TINTS.base) },
    uRimStrength: { value: LIGHT.rimStrength },
    uRimDir: { value: new ctx.THREE.Vector3(...LIGHT.rimDir).normalize() },
    uKeySide: { value: new ctx.THREE.Vector2(-1, 0) }, // key light direction on screen (view xy), per frame
    uKeyRim: { value: LIGHT.keyRim },
    uTime: { value: 0 },
    uSway: { value: new ctx.THREE.Vector2() },
    uSwayBox: { value: new ctx.THREE.Vector4(1, 0, 0, -1) },
    uSwayAmp: { value: 0 },
  };
  ctx.outlineU = {
    uRes: { value: new ctx.THREE.Vector2(1, 1) },
    uWClamp: { value: new ctx.THREE.Vector2(1.2, 2.6) }, // constant px width at upper-body distances, thinner far away
    uDpr: { value: 1 },
  };
  // scratch objects (no per-frame allocation) and rest-pose references
  ctx._v1 = new ctx.THREE.Vector3(); ctx._v2 = new ctx.THREE.Vector3(); ctx._v3 = new ctx.THREE.Vector3();
  ctx._vRay = new ctx.THREE.Vector3(); // lookTarget's pointer ray (its `out` is _v3)
  ctx._q1 = new ctx.THREE.Quaternion(); ctx._q2 = new ctx.THREE.Quaternion(); ctx._q3 = new ctx.THREE.Quaternion();
  ctx._sph = new ctx.THREE.Spherical();
  ctx.AX = { x: new ctx.THREE.Vector3(1, 0, 0), y: new ctx.THREE.Vector3(0, 1, 0), z: new ctx.THREE.Vector3(0, 0, 1) };
  ctx._sph2 = new ctx.THREE.Spherical(); ctx._f1 = new ctx.THREE.Vector3(); ctx._f2 = new ctx.THREE.Vector3();
  ctx.restFwd = new ctx.THREE.Vector3(0, 0, 1); // head's local forward (face direction at rest)
  ctx.hipsFwd = new ctx.THREE.Vector3(0, 0, 1);
  ctx.restUp = new ctx.THREE.Vector3(0, 1, 0); // face bone's local "up" (crown direction at rest)
  ctx.eyeLocal = new ctx.THREE.Vector3(0, 0.1, 0.05); // eye level in the face bone's local space (m)
  ctx.faceBone = null;
  ctx.clean = []; // { bone, q }: bones the procedural layers rotate, with their clean clip pose
  ctx.headRestY = 1.36; // standing head height (rest pose) for framing — never the live pose
  ctx.rimGoal = new ctx.THREE.Color(RIM_TINTS.base);
  ctx.rimDirWorld = new ctx.THREE.Vector3(...LIGHT.rimDir).normalize();
  ctx.unitsPerMetre = 1; // GLB positions are quantized: shader maths on `position` uses these units
  ctx.hasHeadCenter = false;
  ctx.hasChains = false;

  if (!(await loadScene(ctx))) return ctx.stub;

  initializeCamera(ctx);

  initializeMotion(ctx);

  initializePresence(ctx);

  initializeSprings(ctx);

  ctx.shadowA = 0;

  initializeLoop(ctx);

  initializeLifecycle(ctx);

  createPublicApi(ctx);

  // Compile shaders off the critical path where the browser supports it
  // (KHR_parallel_shader_compile), so the first frame doesn't hitch.
  try { if (ctx.renderer.compileAsync) await ctx.renderer.compileAsync(ctx.scene, ctx.camera); } catch { /* fall back to lazy compile */ }
  if (ctx.disposed) return ctx.stub;
  ctx.mark('compileMs');
  ctx.updateTint(0);
  ctx.renderer.render(ctx.scene, ctx.camera);
  ctx.mark('firstFrameMs');
  ctx.ready = true;
  ctx.syncPaused();
  try { ctx.onReady?.(ctx.api); } catch { /* caller's handler threw */ }
  return ctx.api;

}

function hasWebGL() {
  try {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl2') || c.getContext('webgl');
    gl?.getExtension('WEBGL_lose_context')?.loseContext();
    return !!gl;
  } catch { return false; }
}
