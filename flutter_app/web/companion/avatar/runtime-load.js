import { ROLE_FALLBACK } from './shaders.js';

// Initialize load at its original point in the lifecycle.
export async function loadScene(ctx) {
  // ── load ─────────────────────────────────────────────────────────────────
  ctx.onAbort = () => ctx.teardown();
  ctx.opts.signal?.addEventListener('abort', ctx.onAbort, { once: true });
  try {
    const buf = ctx.modelBuffer;
    if (ctx.disposed) return false;
    if (!(buf instanceof ArrayBuffer) && !ArrayBuffer.isView(buf)) throw new Error('loadModel() must resolve to an ArrayBuffer');
    const data = ArrayBuffer.isView(buf) ? buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength) : buf;
    const loader = new ctx.GLTFLoader().setMeshoptDecoder(ctx.MeshoptDecoder);
    const gltf = await new Promise((resolve, reject) => loader.parse(data, '', resolve, reject));
    ctx.mark('parseMs');
    if (ctx.disposed) { ctx.disposeObject(gltf.scene); return false; }
    ctx.root = gltf.scene;
    if (!ctx.opts.framing && ctx.root.userData?.preferredFraming === 'full') ctx.framing = 'full';
    ctx.root.traverse((o) => { if (o.isSkinnedMesh) ctx.skinned.push(o); });
    const chainData = Array.isArray(ctx.root.userData?.springChains) ? ctx.root.userData.springChains : [];
    ctx.hasChains = chainData.some(ctx.chainUsable); // same rule buildSprings applies: never sway off with no springs
    const skin = ctx.skinned[0]?.skeleton;
    const hc = skin ? ctx.headCenterFromSkin(skin) : null;
    if (hc) { ctx.shared.uHeadCenter.value.copy(hc.center).applyMatrix4(ctx.skinned[0].bindMatrixInverse); ctx.unitsPerMetre = hc.upm; ctx.hasHeadCenter = true; }
    const hairGeos = [];
    ctx.root.traverse((o) => {
      if (!o.isMesh) return;
      o.frustumCulled = false; // skinned bounds are bind-pose; avoid popping
      const mats = Array.isArray(o.material) ? o.material : [o.material];
      const next = mats.map((m) => {
        const role = m.userData?.role || ROLE_FALLBACK(m.name);
        const t = ctx.toonFrom(m, role);
        if (role === 'hair' && !Array.isArray(o.material)) hairGeos.push(o.geometry);
        m.dispose();
        return t;
      });
      o.material = Array.isArray(o.material) ? next : next[0];
    });
    ctx.setSwayBox(hairGeos);
    ctx.scene.add(ctx.root);
    ctx.head = ctx.root.getObjectByName('mixamorigHead');
    ctx.neck = ctx.root.getObjectByName('mixamorigNeck');
    ctx.headJoint = Boolean(ctx.head && skin?.bones.includes(ctx.head));
    for (const n of ['Hips', 'Spine1', 'Spine2', 'LeftShoulder', 'RightShoulder', 'LeftArm', 'RightArm', 'LeftForeArm', 'RightForeArm', 'LeftHand', 'RightHand', 'LeftToeBase', 'RightToeBase', 'LeftFoot', 'RightFoot']) {
      ctx.bones[n] = ctx.root.getObjectByName(`mixamorig${n}`) || null;
    }
    for (const m of ctx.skinned) {
      const idx = m.morphTargetDictionary?.blink;
      if (idx !== undefined) ctx.blinkIdx = idx;
    }
    ctx.buildSpeechMouth();
    ctx.addOutlines();
    ctx.captureRest();
    ctx.springs = ctx.hasChains ? ctx.buildSprings(ctx.root.userData) : null;
    let extraClips = [];
    try {
      const { loadMotionClips } = await import('../motion-library.js?v=6c7c0d59a925');
      const library = await loadMotionClips({ THREE: ctx.THREE, root: ctx.root, signal: ctx.opts.signal });
      extraClips = library.clips;
      ctx.motionCatalog = library.available;
      ctx.motionLibraryStatus = extraClips.length ? 'ready' : 'unavailable';
    } catch {
      // An optional gesture download must never remove the model or text chat.
      ctx.motionLibraryStatus = 'unavailable';
    }
    if (ctx.disposed) return false;
    ctx.mixer = new ctx.THREE.AnimationMixer(ctx.root);
    for (const clip of [...gltf.animations, ...extraClips]) {
      const a = ctx.mixer.clipAction(clip);
      a.enabled = true;
      ctx.actions.set(clip.name, a);
    }
    ctx.buildFidgets(gltf.animations);
    for (const entry of ctx.motionCatalog) {
      if (entry.passive && ctx.actions.has(entry.clip)) ctx.fidgetActions.set(entry.clip, ctx.actions.get(entry.clip));
    }
    ctx.mixer.addEventListener('finished', ctx.onFinished);
    ctx.talkAction = ctx.actions.get('talking') || null;
    if (ctx.talkAction) { ctx.talkAction.setEffectiveWeight(0); ctx.talkAction.play(); }
    ctx.shadowMesh = ctx.makeContactShadow();
  } catch (e) {
    ctx.fail(e);
    ctx.teardown();
    return false;
  }

  return true;

}
