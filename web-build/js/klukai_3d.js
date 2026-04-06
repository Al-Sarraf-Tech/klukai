(function () {
  'use strict';

  let scene, camera, renderer, clock, mixer, model, skeleton;
  let isDisposed = false;
  let morphs = {};
  let elapsed = 0;
  let nextBlink = 2;

  const RIM_COLORS = {
    relaxed: 0x4FC3F7, happy: 0x6EE7B7, serious: 0x3B82F6, shy: 0xF9A8D4,
    combat: 0xEF4444, tender: 0xE88CA5, drowsy: 0x64748B, melancholy: 0x6366F1,
  };

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

  let rimLight = null;
  let currentMoodGroup = 'relaxed';
  let isTalking = false;

  function setMorph(name, value) {
    const m = morphs[name];
    if (m) m.mesh.morphTargetInfluences[m.index] = Math.max(0, Math.min(1, value));
  }

  window.klukaiBridge = {
    async init(canvasId, modelUrl) {
      isDisposed = false;
      elapsed = 0;

      const canvas = document.getElementById(canvasId);
      if (!canvas) return false;

      // Scene
      scene = new THREE.Scene();
      clock = new THREE.Clock();
      camera = new THREE.PerspectiveCamera(30, 1, 0.01, 100); // aspect updated after canvas sizes
      camera.position.set(0, 1.3, 2.0);
      camera.lookAt(0, 1.1, 0);

      // Wait for canvas to have actual dimensions (Flutter platform view may delay sizing)
      for (let i = 0; i < 50 && (canvas.clientWidth === 0 || canvas.clientHeight === 0); i++) {
        await new Promise(r => setTimeout(r, 100));
      }
      const w = canvas.clientWidth || canvas.parentElement?.clientWidth || 400;
      const h = canvas.clientHeight || canvas.parentElement?.clientHeight || 600;
      console.log('[klukai_3d] Canvas size:', w, 'x', h);

      renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
      renderer.setSize(w, h);
      renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
      renderer.outputColorSpace = THREE.SRGBColorSpace;
      camera.aspect = w / h;
      camera.updateProjectionMatrix();

      scene.add(new THREE.AmbientLight(0xffffff, 0.6));
      const dl = new THREE.DirectionalLight(0xffffff, 0.8);
      dl.position.set(2, 3, 2);
      scene.add(dl);
      rimLight = new THREE.PointLight(0x4FC3F7, 1.0, 5);
      rimLight.position.set(-1.5, 1.5, -0.5);
      scene.add(rimLight);

      // Load model
      try {
        const gltf = await new Promise((resolve, reject) => {
          new (window.GLTFLoader)().load(modelUrl, resolve, undefined, reject);
        });

        model = gltf.scene;

        // Hide junk and duplicate meshes
        const seen = new Set();
        model.traverse(n => {
          if (n.name === 'Cube') { n.removeFromParent(); return; }
          if (n.isMesh) {
            const name = n.name.toLowerCase();
            if (name.includes('dorm') || name.includes('fight') ||
                name.includes('ssr0102') || name.includes('ssr0103') || name.includes('ssr0101')) {
              n.visible = false;
              return;
            }
            const base = name.replace(/\.\d+$/, '');
            if (name.includes('face') || name.includes('hair')) {
              if (seen.has(base)) { n.visible = false; return; }
              seen.add(base);
            }
          }
        });

        scene.add(model);

        // Center and auto-frame camera
        const box = new THREE.Box3().setFromObject(model);
        const sz = box.getSize(new THREE.Vector3());
        const center = box.getCenter(new THREE.Vector3());
        // Scale up if model is tiny (Mixamo FBX exports at 0.01 scale)
        if (sz.y < 0.1) {
          const scale = 1.7 / sz.y;  // normalize to ~1.7 units tall
          model.scale.setScalar(scale);
          box.setFromObject(model);
          sz.copy(box.getSize(new THREE.Vector3()));
          center.copy(box.getCenter(new THREE.Vector3()));
          console.log('[klukai_3d] Scaled up by', scale.toFixed(0) + 'x');
        }
        model.position.set(-center.x, -box.min.y, -center.z);
        console.log('[klukai_3d] Model size:', sz.x.toFixed(2), 'x', sz.y.toFixed(2), 'x', sz.z.toFixed(2));

        // Auto-frame: position camera based on actual model height
        camera.position.set(0, sz.y * 0.59, sz.y * 2.2);
        camera.lookAt(0, sz.y * 0.50, 0);
        camera.updateProjectionMatrix();

        // Find skeleton
        model.traverse(n => {
          if (n.isSkinnedMesh && n.skeleton && (!skeleton || n.skeleton.bones.length > skeleton.bones.length))
            skeleton = n.skeleton;
        });
        console.log('[klukai_3d] Skeleton:', skeleton ? skeleton.bones.length + ' bones' : 'NONE');

        // Index morphs
        model.traverse(n => {
          if (n.isMesh && n.morphTargetDictionary) {
            for (const [name, idx] of Object.entries(n.morphTargetDictionary)) {
              if (!morphs[name]) morphs[name] = { mesh: n, index: idx };
            }
          }
        });

        // Play idle animation
        if (gltf.animations.length > 0) {
          mixer = new THREE.AnimationMixer(model);
          const clip = gltf.animations.find(a => a.name === 'idle') || gltf.animations[0];
          const action = mixer.clipAction(clip);
          action.setLoop(THREE.LoopRepeat);
          action.play();
          console.log('[klukai_3d] Playing:', clip.name, clip.duration.toFixed(1) + 's');
        }

        // Resize handling
        new ResizeObserver(() => {
          if (!renderer || !camera) return;
          const w = canvas.clientWidth, h = canvas.clientHeight;
          if (w > 0 && h > 0) {
            renderer.setSize(w, h, false);
            camera.aspect = w / h;
            camera.updateProjectionMatrix();
          }
        }).observe(canvas);

        // Render loop
        const animate = () => {
          if (isDisposed) return;
          requestAnimationFrame(animate);
          const dt = clock.getDelta();
          elapsed += dt;

          // Tick animation mixer (handles all bone transforms)
          if (mixer) mixer.update(dt);

          // Blink (morph targets — not in the baked animation)
          if (elapsed >= nextBlink) {
            const bp = (elapsed - nextBlink) * 8;
            let v = bp < 0.5 ? bp * 2 : bp < 1 ? (1 - bp) * 2 : 0;
            if (bp >= 1) { nextBlink = elapsed + 2 + Math.random() * 4; v = 0; }
            setMorph('Eyes_Close_Down_Right', v);
            setMorph('Eyes_Close_Down_Left', v);
            setMorph('Eyes_Close_Up_Right', v * 0.3);
            setMorph('Eyes_Close_Up_Left', v * 0.3);
          }

          // Talking mouth
          if (isTalking) {
            const m = (Math.sin(elapsed * 12) * 0.3 + 0.3) * (Math.sin(elapsed * 7.3) * 0.2 + 0.5);
            setMorph('Mouth_Happy', m * 0.6);
          } else {
            setMorph('Mouth_Happy', 0);
          }

          if (model) model.updateMatrixWorld(true);
          if (skeleton) skeleton.update();
          renderer.render(scene, camera);
        };
        animate();

        console.log('[klukai_3d] Initialized');
        return true;

      } catch (e) {
        console.error('[klukai_3d] Load failed:', e);
        return false;
      }
    },

    setMood(moodName) {
      const g = MOOD_GROUPS[moodName] || 'relaxed';
      if (g !== currentMoodGroup) {
        currentMoodGroup = g;
        if (rimLight && RIM_COLORS[g]) rimLight.color.setHex(RIM_COLORS[g]);
      }
    },

    playReaction() {},
    setTalking(enabled) { isTalking = enabled; },
    setBlush(intensity) { setMorph('Mouth_Happy', Math.max(0, Math.min(1, intensity))); },
    lookAt() {},

    setDormMode(enabled) {
      if (enabled) {
        currentMoodGroup = 'drowsy';
        if (rimLight) { rimLight.intensity = 0.4; rimLight.color.setHex(0x64748B); }
      } else {
        if (rimLight) rimLight.intensity = 1.0;
      }
    },

    dispose() {
      isDisposed = true;
      if (mixer) mixer.stopAllAction();
      if (renderer) { renderer.dispose(); renderer.forceContextLoss(); }
      scene = camera = renderer = model = skeleton = mixer = null;
      morphs = {};
      console.log('[klukai_3d] Disposed');
    },
  };

  console.log('[klukai_3d] Bridge ready');
})();
