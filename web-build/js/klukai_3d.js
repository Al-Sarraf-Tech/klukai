(function () {
  'use strict';

  let scene, camera, renderer, clock;
  let model = null;
  let skeleton = null;
  let isDisposed = false;
  let fidgetTimer = null;
  let currentMoodGroup = 'relaxed';
  let isDormMode = false;
  let rimLight = null;
  let isTalking = false;
  let elapsed = 0;

  let bones = {};
  let morphs = {};

  const MOOD_GROUPS = {
    relaxed: 'relaxed', calm: 'relaxed', content: 'relaxed', neutral: 'relaxed', composed: 'relaxed',
    happy: 'happy', playful: 'happy', teasing: 'happy', smug: 'happy', confident: 'happy',
    quietly_pleased: 'happy', amused: 'happy',
    focused: 'serious', analytical: 'serious', commanding: 'serious', determined: 'serious',
    vigilant: 'serious', calculating: 'serious', prideful: 'serious',
    shy: 'shy', flustered: 'shy', embarrassed: 'shy', bashful: 'shy',
    hunting: 'combat', aggressive: 'combat', fierce: 'combat', alert: 'combat',
    combat_ready: 'combat', battle_ready: 'combat', adrenaline: 'combat', competitive: 'combat',
    tender: 'tender', devoted: 'tender', affectionate: 'tender', warm: 'tender',
    vulnerable: 'tender', yearning: 'tender', longing: 'tender', protective: 'tender',
    drowsy: 'drowsy', sleepy: 'drowsy', exhausted: 'drowsy', lazy: 'drowsy',
    sad: 'melancholy', lonely: 'melancholy', distant: 'melancholy', nostalgic: 'melancholy',
    worried: 'melancholy', melancholic: 'melancholy', haunted: 'melancholy', conflicted: 'melancholy',
    exasperated: 'melancholy',
  };

  const RIM_COLORS = {
    relaxed: 0x4FC3F7, happy: 0x6EE7B7, serious: 0x3B82F6, shy: 0xF9A8D4,
    combat: 0xEF4444, tender: 0xE88CA5, drowsy: 0x64748B, melancholy: 0x6366F1,
  };

  const IDLE_PARAMS = {
    relaxed:    { swayAmp: 0.015, swaySpeed: 0.4, breathAmp: 0.008, headTilt: 0.02 },
    happy:      { swayAmp: 0.025, swaySpeed: 0.6, breathAmp: 0.010, headTilt: 0.03 },
    serious:    { swayAmp: 0.006, swaySpeed: 0.3, breathAmp: 0.005, headTilt: 0.01 },
    shy:        { swayAmp: 0.020, swaySpeed: 0.5, breathAmp: 0.008, headTilt: 0.04 },
    combat:     { swayAmp: 0.010, swaySpeed: 0.35, breathAmp: 0.007, headTilt: 0.01 },
    tender:     { swayAmp: 0.018, swaySpeed: 0.45, breathAmp: 0.009, headTilt: 0.03 },
    drowsy:     { swayAmp: 0.035, swaySpeed: 0.25, breathAmp: 0.012, headTilt: 0.05 },
    melancholy: { swayAmp: 0.012, swaySpeed: 0.35, breathAmp: 0.007, headTilt: 0.02 },
  };

  let fidgetActive = false, fidgetStartTime = 0, fidgetDuration = 0, fidgetType = '';
  let nextBlinkTime = 0, blinkPhase = -1;
  let lookTargetX = 0, lookTargetY = 0, lookCurrentX = 0, lookCurrentY = 0;
  let reactionActive = false, reactionStart = 0;

  function createScene(canvas) {
    scene = new THREE.Scene();
    clock = new THREE.Clock();
    camera = new THREE.PerspectiveCamera(30, canvas.clientWidth / canvas.clientHeight, 0.01, 100);
    camera.position.set(0, 1.2, 3.5);
    camera.lookAt(0, 1.0, 0);
    renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
    renderer.setSize(canvas.clientWidth, canvas.clientHeight);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    const dl = new THREE.DirectionalLight(0xffffff, 0.8);
    dl.position.set(2, 3, 2);
    scene.add(dl);
    rimLight = new THREE.PointLight(RIM_COLORS.relaxed, 1.0, 5);
    rimLight.position.set(-1.5, 1.5, -0.5);
    scene.add(rimLight);
  }

  async function loadModel(url) {
    const loader = new (window.GLTFLoader || THREE.GLTFLoader)();
    const gltf = await new Promise((resolve, reject) => {
      loader.load(url, resolve, undefined, reject);
    });

    model = gltf.scene;

    // Remove junk
    const junk = [];
    model.traverse(n => { if (n.name === 'Cube' || n.name === 'Camera' || n.name === 'Light') junk.push(n); });
    junk.forEach(n => n.removeFromParent());

    scene.add(model);

    // Center and ground
    const box = new THREE.Box3().setFromObject(model);
    const center = box.getCenter(new THREE.Vector3());
    model.position.set(-center.x, -box.min.y, -center.z);

    // Find the skeleton from the Body mesh
    model.traverse(n => {
      if (n.isSkinnedMesh && n.name === 'Body' && n.skeleton) {
        skeleton = n.skeleton;
      }
    });
    if (!skeleton) {
      model.traverse(n => {
        if (n.isSkinnedMesh && n.skeleton && (!skeleton || n.skeleton.bones.length > skeleton.bones.length))
          skeleton = n.skeleton;
      });
    }

    console.log('[klukai_3d] Skeleton:', skeleton ? skeleton.bones.length + ' bones' : 'NONE');

    // Index bones — try both DEF- prefix and bare names
    if (skeleton) {
      const boneMap = {
        'DEF-Head_M': 'head', 'Head_M': 'head',
        'DEF-Neck_M': 'neck', 'Neck_M': 'neck',
        'DEF-Chest_M': 'chest', 'Chest_M': 'chest',
        'DEF-Spine2_M': 'spine2', 'Spine2_M': 'spine2',
        'DEF-Spine1_M': 'spine1', 'Spine1_M': 'spine1',
        'DEF-Root_M': 'root', 'Root_M': 'root',
        'DEF-Shoulder_L': 'shoulderL', 'Shoulder_L': 'shoulderL',
        'DEF-Shoulder_R': 'shoulderR', 'Shoulder_R': 'shoulderR',
        'DEF-Elbow_L': 'elbowL', 'Elbow_L': 'elbowL',
        'DEF-Elbow_R': 'elbowR', 'Elbow_R': 'elbowR',
      };
      for (const bone of skeleton.bones) {
        const role = boneMap[bone.name];
        if (role && !bones[role]) {
          bones[role] = bone;
        }
      }

      // Save initial quaternions and positions
      for (const b of Object.values(bones)) {
        if (b) {
          b._iq = b.quaternion.clone();
          b._ip = b.position.clone();
        }
      }

      // Set arms-down rest pose using quaternion
      if (bones.shoulderL) {
        const q = new THREE.Quaternion();
        q.setFromAxisAngle(new THREE.Vector3(0, 0, 1), 1.2);
        bones.shoulderL.quaternion.multiply(q);
      }
      if (bones.shoulderR) {
        const q = new THREE.Quaternion();
        q.setFromAxisAngle(new THREE.Vector3(0, 0, 1), -1.2);
        bones.shoulderR.quaternion.multiply(q);
      }
      if (bones.elbowL) {
        const q = new THREE.Quaternion();
        q.setFromAxisAngle(new THREE.Vector3(1, 0, 0), 0.3);
        bones.elbowL.quaternion.multiply(q);
      }
      if (bones.elbowR) {
        const q = new THREE.Quaternion();
        q.setFromAxisAngle(new THREE.Vector3(1, 0, 0), 0.3);
        bones.elbowR.quaternion.multiply(q);
      }

      // Propagate world matrices — do NOT call calculateInverses() or bind()
      // as those destroy the glTF's pre-computed inverseBindMatrices
      model.updateMatrixWorld(true);

      // Re-save quaternions AFTER rest pose
      for (const b of Object.values(bones)) {
        if (b) {
          b._iq = b.quaternion.clone();
          b._ip = b.position.clone();
        }
      }

      console.log('[klukai_3d] Bones:', Object.keys(bones));
    }

    // Index morphs
    model.traverse(n => {
      if (n.isMesh && n.morphTargetDictionary) {
        for (const [name, idx] of Object.entries(n.morphTargetDictionary)) {
          if (!morphs[name]) morphs[name] = { mesh: n, index: idx };
        }
      }
    });
    console.log('[klukai_3d] Morphs:', Object.keys(morphs));

    // Weight magnitude diagnostic — check if weights are diluted
    if (skeleton && bones.shoulderL) {
      const shoulderIdx = skeleton.bones.indexOf(bones.shoulderL);
      const spineIdx = skeleton.bones.indexOf(bones.spine1);
      console.log('[klukai_3d] === WEIGHT DIAGNOSTIC ===');
      console.log('[klukai_3d] Shoulder bone index:', shoulderIdx, 'Spine1 bone index:', spineIdx);
      model.traverse(n => {
        if (n.isSkinnedMesh && n.name === 'Body') {
          const si = n.geometry.attributes.skinIndex;
          const sw = n.geometry.attributes.skinWeight;
          let shoulderMax = 0, shoulderCount = 0;
          let spineMax = 0, spineCount = 0;
          for (let v = 0; v < si.count; v++) {
            for (let c = 0; c < 4; c++) {
              const bIdx = Math.round(si.getComponent(v, c));
              const w = sw.getComponent(v, c);
              if (bIdx === shoulderIdx && w > 0.001) { shoulderCount++; shoulderMax = Math.max(shoulderMax, w); }
              if (bIdx === spineIdx && w > 0.001) { spineCount++; spineMax = Math.max(spineMax, w); }
            }
          }
          console.log('[klukai_3d] DEF-Shoulder_L: ' + shoulderCount + ' verts, max weight: ' + shoulderMax.toFixed(4));
          console.log('[klukai_3d] DEF-Spine1_M:   ' + spineCount + ' verts, max weight: ' + spineMax.toFixed(4));
          if (shoulderMax < 0.1) console.error('[klukai_3d] CONFIRMED: Shoulder weights are diluted! Max=' + shoulderMax.toFixed(4));
        }
      });
    }

    nextBlinkTime = 2 + Math.random() * 3;
  }

  function setMorph(name, value) {
    const m = morphs[name];
    if (m) m.mesh.morphTargetInfluences[m.index] = Math.max(0, Math.min(1, value));
  }

  function updateAnimations(dt) {
    elapsed += dt;
    const p = IDLE_PARAMS[currentMoodGroup] || IDLE_PARAMS.relaxed;

    if (bones.spine1) {
      const breathQ = new THREE.Quaternion();
      breathQ.setFromAxisAngle(new THREE.Vector3(1, 0, 0), Math.sin(elapsed * p.swaySpeed * 2.5) * p.breathAmp);
      bones.spine1.quaternion.copy(bones.spine1._iq).multiply(breathQ);
    }

    if (bones.root) {
      const swayQ = new THREE.Quaternion();
      swayQ.setFromAxisAngle(new THREE.Vector3(0, 0, 1), Math.sin(elapsed * p.swaySpeed) * p.swayAmp);
      bones.root.quaternion.copy(bones.root._iq).multiply(swayQ);
    }

    if (bones.head && !reactionActive) {
      lookCurrentX += (lookTargetX - lookCurrentX) * 0.05;
      lookCurrentY += (lookTargetY - lookCurrentY) * 0.05;
      const headQ = new THREE.Quaternion();
      const tiltY = Math.sin(elapsed * p.swaySpeed * 0.8) * p.headTilt + lookCurrentX * 0.4;
      const tiltX = Math.cos(elapsed * p.swaySpeed * 0.5) * p.headTilt * 0.5 + lookCurrentY * 0.3;
      headQ.setFromEuler(new THREE.Euler(tiltX, tiltY, 0));
      bones.head.quaternion.copy(bones.head._iq).multiply(headQ);
    }

    // Blink
    if (elapsed >= nextBlinkTime && blinkPhase < 0) blinkPhase = 0;
    if (blinkPhase >= 0) {
      blinkPhase += dt * 8;
      let val = blinkPhase < 0.5 ? blinkPhase * 2 : blinkPhase < 1.0 ? (1 - blinkPhase) * 2 : 0;
      if (blinkPhase >= 1.0) { blinkPhase = -1; nextBlinkTime = elapsed + 2 + Math.random() * 4; val = 0; }
      setMorph('Eyes_Close_Down_Right', val);
      setMorph('Eyes_Close_Down_Left', val);
      setMorph('Eyes_Close_Up_Right', val * 0.3);
      setMorph('Eyes_Close_Up_Left', val * 0.3);
    }

    // Talking
    if (isTalking) {
      const m = (Math.sin(elapsed * 12) * 0.3 + 0.3) * (Math.sin(elapsed * 7.3) * 0.2 + 0.5);
      setMorph('Mouth_Happy', m * 0.6);
      setMorph('Mouth_Smile', m * 0.3);
    } else {
      setMorph('Mouth_Happy', 0);
      setMorph('Mouth_Smile', 0);
    }

    // Tap reaction
    if (reactionActive && bones.head) {
      const rt = elapsed - reactionStart;
      if (rt < 0.8) {
        const rq = new THREE.Quaternion();
        const angle = rt < 0.3 ? Math.sin(rt * 10) * 0.1 : Math.sin((rt - 0.3) * 3) * 0.03;
        rq.setFromAxisAngle(new THREE.Vector3(0, 1, 0), angle);
        bones.head.quaternion.copy(bones.head._iq).multiply(rq);
      } else {
        reactionActive = false;
      }
    }

    // Fidget
    if (fidgetActive && bones.head) {
      const ft = (elapsed - fidgetStartTime) / fidgetDuration;
      if (ft >= 1.0) { fidgetActive = false; }
      else {
        const ease = Math.sin(ft * Math.PI);
        const fq = new THREE.Quaternion();
        if (fidgetType === 'look_around') fq.setFromAxisAngle(new THREE.Vector3(0, 1, 0), Math.sin(ft * Math.PI * 2) * 0.12 * ease);
        else if (fidgetType === 'head_tilt') fq.setFromAxisAngle(new THREE.Vector3(0, 0, 1), Math.sin(ft * Math.PI) * 0.06);
        else if (fidgetType === 'nod') fq.setFromAxisAngle(new THREE.Vector3(1, 0, 0), Math.sin(ft * Math.PI * 2) * 0.05 * ease);
        bones.head.quaternion.copy(bones.head._iq).multiply(fq);
      }
    }
  }

  function scheduleFidget() {
    if (isDisposed) return;
    fidgetTimer = setTimeout(() => {
      if (isDisposed || isDormMode || fidgetActive) return;
      const types = ['look_around', 'head_tilt', 'nod'];
      fidgetType = types[Math.floor(Math.random() * types.length)];
      fidgetActive = true;
      fidgetStartTime = elapsed;
      fidgetDuration = 1.5 + Math.random() * 1.5;
      scheduleFidget();
    }, (20 + Math.random() * 40) * 1000);
  }

  function animate() {
    if (isDisposed) return;
    requestAnimationFrame(animate);
    const dt = clock.getDelta();
    updateAnimations(dt);
    if (model) model.updateMatrixWorld(true);
    if (skeleton) skeleton.update();
    if (renderer && scene && camera) renderer.render(scene, camera);
  }

  function handleResize(canvas) {
    if (!renderer || !camera) return;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (w > 0 && h > 0 && (canvas.width !== w || canvas.height !== h)) {
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    }
  }

  window.klukaiBridge = {
    async init(canvasId, modelUrl) {
      isDisposed = false;
      elapsed = 0;
      const canvas = document.getElementById(canvasId);
      if (!canvas) { console.error('[klukai_3d] Canvas not found:', canvasId); return false; }
      createScene(canvas);
      try { await loadModel(modelUrl); }
      catch (e) { console.error('[klukai_3d] Failed to load model:', e); return false; }
      const ro = new ResizeObserver(() => handleResize(canvas));
      ro.observe(canvas);
      animate();
      scheduleFidget();
      console.log('[klukai_3d] Initialized');
      return true;
    },
    setMood(moodName) {
      const g = MOOD_GROUPS[moodName] || 'relaxed';
      if (g !== currentMoodGroup) { currentMoodGroup = g; if (rimLight && RIM_COLORS[g]) rimLight.color.setHex(RIM_COLORS[g]); }
    },
    playReaction() { reactionActive = true; reactionStart = elapsed; },
    setTalking(enabled) { isTalking = enabled; },
    setBlush(intensity) { setMorph('Mouth_Happy', Math.max(0, Math.min(1, intensity))); },
    lookAt(normX, normY) { if (!isDormMode) { lookTargetX = Math.max(-1, Math.min(1, normX)); lookTargetY = Math.max(-1, Math.min(1, normY)); } },
    setDormMode(enabled) { isDormMode = enabled; if (enabled) { currentMoodGroup = 'drowsy'; if (rimLight) { rimLight.intensity = 0.4; rimLight.color.setHex(0x64748B); } } else { if (rimLight) rimLight.intensity = 1.0; } },
    dispose() {
      isDisposed = true;
      if (fidgetTimer) clearTimeout(fidgetTimer);
      if (renderer) { renderer.dispose(); renderer.forceContextLoss(); }
      scene = null; camera = null; renderer = null; model = null; skeleton = null; bones = {}; morphs = {};
      console.log('[klukai_3d] Disposed');
    },
  };
  console.log('[klukai_3d] Bridge ready');
})();
