(function () {
  'use strict';

  let scene, camera, renderer, clock, mixer;
  let model = null;
  let currentAction = null;
  let blinkAction = null;
  let talkAction = null;
  let animations = {};
  let isDisposed = false;
  let fidgetTimer = null;
  let currentMoodGroup = 'relaxed';
  let isDormMode = false;
  let rimLight = null;

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
    relaxed:    0x4FC3F7,
    happy:      0x6EE7B7,
    serious:    0x3B82F6,
    shy:        0xF9A8D4,
    combat:     0xEF4444,
    tender:     0xE88CA5,
    drowsy:     0x64748B,
    melancholy: 0x6366F1,
  };

  const FIDGETS = {
    relaxed:    ['fidget_hair', 'fidget_stretch', 'fidget_smile'],
    happy:      ['fidget_hair', 'fidget_stretch', 'fidget_smile'],
    serious:    ['fidget_weapon', 'fidget_scan'],
    shy:        ['fidget_tuck_hair', 'fidget_look_away'],
    combat:     ['fidget_weapon', 'fidget_scan'],
    tender:     ['fidget_hair', 'fidget_smile'],
    drowsy:     ['fidget_yawn', 'fidget_head_nod', 'fidget_rub_eyes'],
    melancholy: ['fidget_look_away', 'fidget_weight_shift'],
  };

  const UNIVERSAL_FIDGETS = ['fidget_blink_hard', 'fidget_look_around', 'fidget_weight_shift'];

  function createScene(canvas) {
    scene = new THREE.Scene();
    clock = new THREE.Clock();

    camera = new THREE.PerspectiveCamera(30, canvas.clientWidth / canvas.clientHeight, 0.1, 100);
    camera.position.set(0, 1.2, 2.5);
    camera.lookAt(0, 1.0, 0);

    renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
    renderer.setSize(canvas.clientWidth, canvas.clientHeight);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.0;

    const ambient = new THREE.AmbientLight(0xffffff, 0.6);
    scene.add(ambient);

    const dirLight = new THREE.DirectionalLight(0xffffff, 0.8);
    dirLight.position.set(2, 3, 2);
    scene.add(dirLight);

    rimLight = new THREE.PointLight(RIM_COLORS.relaxed, 1.0, 5);
    rimLight.position.set(-1.5, 1.5, -0.5);
    scene.add(rimLight);
  }

  async function loadModel(url) {
    const loader = new THREE.GLTFLoader();
    const gltf = await new Promise((resolve, reject) => {
      loader.load(url, resolve, undefined, reject);
    });

    model = gltf.scene;
    scene.add(model);

    const box = new THREE.Box3().setFromObject(model);
    const center = box.getCenter(new THREE.Vector3());
    model.position.sub(center);
    model.position.y += box.getSize(new THREE.Vector3()).y / 2;

    mixer = new THREE.AnimationMixer(model);

    for (const clip of gltf.animations) {
      animations[clip.name] = clip;
    }

    if (animations['blink']) {
      blinkAction = mixer.clipAction(animations['blink']);
      blinkAction.setLoop(THREE.LoopRepeat);
      blinkAction.weight = 1.0;
      blinkAction.play();
    }

    playMoodIdle('relaxed');
    scheduleFidget();
  }

  function playMoodIdle(group) {
    const clipName = 'idle_' + group;
    const clip = animations[clipName] || animations['idle_relaxed'];
    if (!clip) return;

    const newAction = mixer.clipAction(clip);
    newAction.setLoop(THREE.LoopRepeat);

    if (currentAction && currentAction !== newAction) {
      currentAction.crossFadeTo(newAction, 0.5, true);
    }

    newAction.play();
    currentAction = newAction;
    currentMoodGroup = group;

    if (rimLight && RIM_COLORS[group]) {
      rimLight.color.setHex(RIM_COLORS[group]);
    }
  }

  function playOneShot(clipName) {
    const clip = animations[clipName];
    if (!clip) return;

    const action = mixer.clipAction(clip);
    action.setLoop(THREE.LoopOnce);
    action.clampWhenFinished = false;
    action.reset().play();

    mixer.addEventListener('finished', function onFinished(e) {
      if (e.action === action) {
        mixer.removeEventListener('finished', onFinished);
        action.stop();
      }
    });
  }

  function scheduleFidget() {
    if (isDisposed) return;
    const delay = (30 + Math.random() * 60) * 1000;
    fidgetTimer = setTimeout(() => {
      if (isDisposed || isDormMode) return;
      const moodFidgets = FIDGETS[currentMoodGroup] || [];
      const allFidgets = [...moodFidgets, ...UNIVERSAL_FIDGETS];
      const pick = allFidgets[Math.floor(Math.random() * allFidgets.length)];
      if (animations[pick]) {
        playOneShot(pick);
      }
      scheduleFidget();
    }, delay);
  }

  function animate() {
    if (isDisposed) return;
    requestAnimationFrame(animate);
    const delta = clock.getDelta();
    if (mixer) mixer.update(delta);
    if (renderer && scene && camera) renderer.render(scene, camera);
  }

  function handleResize(canvas) {
    if (!renderer || !camera) return;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (canvas.width !== w || canvas.height !== h) {
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    }
  }

  let headBone = null;
  const maxRotX = 0.3;
  const maxRotY = 0.4;

  function updateLookAt(normX, normY) {
    if (!headBone || isDormMode) return;
    const targetY = Math.max(-maxRotY, Math.min(maxRotY, normX * maxRotY));
    const targetX = Math.max(-maxRotX, Math.min(maxRotX, normY * maxRotX));
    headBone.rotation.y += (targetY - headBone.rotation.y) * 0.1;
    headBone.rotation.x += (targetX - headBone.rotation.x) * 0.1;
  }

  window.klukaiBridge = {
    async init(canvasId, modelUrl) {
      isDisposed = false;
      const canvas = document.getElementById(canvasId);
      if (!canvas) {
        console.error('[klukai_3d] Canvas not found:', canvasId);
        return false;
      }

      createScene(canvas);

      try {
        await loadModel(modelUrl);
      } catch (e) {
        console.error('[klukai_3d] Failed to load model:', e);
        return false;
      }

      model.traverse((node) => {
        if (node.isBone && /head/i.test(node.name)) {
          headBone = node;
        }
      });

      const resizeObserver = new ResizeObserver(() => handleResize(canvas));
      resizeObserver.observe(canvas);

      animate();

      console.log('[klukai_3d] Initialized. Animations:', Object.keys(animations));
      return true;
    },

    setMood(moodName) {
      if (!mixer) return;
      const group = MOOD_GROUPS[moodName] || 'relaxed';
      if (group !== currentMoodGroup) {
        playMoodIdle(group);
      }
    },

    playReaction(reactionName) {
      if (!mixer) return;
      playOneShot(reactionName);
    },

    setTalking(enabled) {
      if (!mixer) return;
      if (enabled && animations['talking']) {
        if (!talkAction) {
          talkAction = mixer.clipAction(animations['talking']);
          talkAction.setLoop(THREE.LoopRepeat);
          talkAction.weight = 0.8;
        }
        talkAction.reset().play();
      } else if (talkAction) {
        talkAction.fadeOut(0.3);
        talkAction = null;
      }
    },

    setBlush(intensity) {
      if (!model) return;
      model.traverse((node) => {
        if (node.isMesh && node.morphTargetInfluences && node.morphTargetDictionary) {
          const idx = node.morphTargetDictionary['blush'];
          if (idx !== undefined) {
            node.morphTargetInfluences[idx] = Math.max(0, Math.min(1, intensity));
          }
        }
      });
    },

    lookAt(normX, normY) {
      updateLookAt(normX, normY);
    },

    setDormMode(enabled) {
      isDormMode = enabled;
      if (enabled) {
        playMoodIdle('drowsy');
        if (rimLight) {
          rimLight.intensity = 0.4;
          rimLight.color.setHex(0x64748B);
        }
      } else {
        if (rimLight) rimLight.intensity = 1.0;
      }
    },

    dispose() {
      isDisposed = true;
      if (fidgetTimer) clearTimeout(fidgetTimer);
      if (mixer) mixer.stopAllAction();
      if (renderer) {
        renderer.dispose();
        renderer.forceContextLoss();
      }
      scene = null;
      camera = null;
      renderer = null;
      mixer = null;
      model = null;
      animations = {};
      currentAction = null;
      blinkAction = null;
      talkAction = null;
      headBone = null;
      console.log('[klukai_3d] Disposed');
    },
  };

  console.log('[klukai_3d] Bridge ready');
})();
