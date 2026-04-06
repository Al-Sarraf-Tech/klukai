(function () {
  'use strict';

  let scene, camera, renderer, clock;
  let model = null;
  let isDisposed = false;
  let fidgetTimer = null;
  let currentMoodGroup = 'relaxed';
  let isDormMode = false;
  let rimLight = null;
  let isTalking = false;
  let elapsed = 0;
  let skeleton = null;  // The active skeleton for update()

  // Bone references (found after model loads)
  let bones = {};
  // Shape key references { meshName: { keyName: { mesh, index } } }
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

  // Mood-specific idle parameters
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

  // Fidget state
  let fidgetActive = false;
  let fidgetStartTime = 0;
  let fidgetDuration = 0;
  let fidgetType = '';

  // Blink state
  let nextBlinkTime = 0;
  let blinkPhase = -1; // -1 = not blinking, 0-1 = progress

  // Look-at target (smoothed)
  let lookTargetX = 0, lookTargetY = 0;
  let lookCurrentX = 0, lookCurrentY = 0;

  // Tap reaction state
  let reactionActive = false;
  let reactionStart = 0;

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
    renderer.toneMappingExposure = 1.0;

    scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    const dirLight = new THREE.DirectionalLight(0xffffff, 0.8);
    dirLight.position.set(2, 3, 2);
    scene.add(dirLight);

    rimLight = new THREE.PointLight(RIM_COLORS.relaxed, 1.0, 5);
    rimLight.position.set(-1.5, 1.5, -0.5);
    scene.add(rimLight);
  }

  async function loadModel(url) {
    const GLTFLoader = window.GLTFLoader;
    const loader = new GLTFLoader();
    const gltf = await new Promise((resolve, reject) => {
      loader.load(url, resolve, undefined, reject);
    });

    model = gltf.scene;

    // Remove junk objects
    const toRemove = [];
    model.traverse((node) => {
      if (node.name === 'Cube' || node.name === 'Camera' || node.name === 'Light') {
        toRemove.push(node);
      }
    });
    toRemove.forEach(n => n.removeFromParent());

    scene.add(model);

    // Center and ground the model
    const box = new THREE.Box3().setFromObject(model);
    const center = box.getCenter(new THREE.Vector3());
    model.position.set(-center.x, -box.min.y, -center.z);

    // Find the 673-bone RIG skeleton that the Body mesh is skinned to
    let targetSkeleton = null;
    model.traverse((node) => {
      if (node.isSkinnedMesh && node.name === 'Body' && node.skeleton) {
        targetSkeleton = node.skeleton;
        console.log('[klukai_3d] Found Body skeleton:', node.skeleton.bones.length, 'bones');
      }
    });

    // Fallback: find the SkinnedMesh skeleton with the most bones (should be 673)
    if (!targetSkeleton) {
      let maxBones = 0;
      model.traverse((node) => {
        if (node.isSkinnedMesh && node.skeleton && node.skeleton.bones.length > maxBones) {
          maxBones = node.skeleton.bones.length;
          targetSkeleton = node.skeleton;
        }
      });
      if (targetSkeleton) console.log('[klukai_3d] Fallback skeleton:', maxBones, 'bones');
    }
    skeleton = targetSkeleton;

    if (!targetSkeleton) {
      console.error('[klukai_3d] No skinned mesh found!');
    } else {
      // Only index bones from the actual deformation skeleton
      const skeletonBoneNames = new Set(targetSkeleton.bones.map(b => b.name));
      console.log('[klukai_3d] Target skeleton bones:', [...skeletonBoneNames].slice(0, 20), '...');

      // DEF- bones are the ONLY ones that deform the mesh (Rigify convention)
      for (const bone of targetSkeleton.bones) {
        const n = bone.name;
        bone._initialRot = bone.rotation.clone();
        bone._initialPos = bone.position.clone();

        if (n === 'DEF-Head_M') bones.head = bone;
        else if (n === 'DEF-Neck_M') bones.neck = bone;
        else if (n === 'DEF-Chest_M') bones.chest = bone;
        else if (n === 'DEF-Spine2_M') bones.spine2 = bone;
        else if (n === 'DEF-Spine1_M') bones.spine1 = bone;
        else if (n === 'DEF-Root_M') bones.root = bone;
        else if (n === 'DEF-Shoulder_L') bones.shoulderL = bone;
        else if (n === 'DEF-Shoulder_R') bones.shoulderR = bone;
        else if (n === 'DEF-Chest_L') bones.chestL = bone;
        else if (n === 'DEF-Chest_R') bones.chestR = bone;
      }
    }

    // Index morph targets from all meshes (shape keys are fine to share)
    model.traverse((node) => {
      if (node.isMesh && node.morphTargetDictionary) {
        for (const [name, idx] of Object.entries(node.morphTargetDictionary)) {
          morphs[name] = { mesh: node, index: idx };
        }
      }
    });

    // Also find arm bones for rest pose
    if (targetSkeleton) {
      for (const bone of targetSkeleton.bones) {
        const n = bone.name;
        if (n === 'DEF-Shoulder_L') bones.shoulderL = bone;
        else if (n === 'DEF-Shoulder_L001') bones.shoulderL1 = bone;
        else if (n === 'DEF-Elbow_L') bones.elbowL = bone;
        else if (n === 'DEF-Elbow_L001') bones.elbowL1 = bone;
        else if (n === 'DEF-Wrist_L') bones.wristL = bone;
        else if (n === 'DEF-Shoulder_R') bones.shoulderR = bone;
        else if (n === 'DEF-Shoulder_R001') bones.shoulderR1 = bone;
        else if (n === 'DEF-Elbow_R') bones.elbowR = bone;
        else if (n === 'DEF-Elbow_R001') bones.elbowR1 = bone;
        else if (n === 'DEF-Wrist_R') bones.wristR = bone;
        else if (n === 'DEF-Hip_L') bones.hipL = bone;
        else if (n === 'DEF-Hip_R') bones.hipR = bone;
      }

      // Set natural rest pose — arms down at sides instead of T-pose
      // Shoulders rotate down (Z axis brings arms down from T-pose)
      if (bones.shoulderL) bones.shoulderL.rotation.z += 1.1;   // ~63° down
      if (bones.shoulderR) bones.shoulderR.rotation.z -= 1.1;
      if (bones.shoulderL1) bones.shoulderL1.rotation.z += 0.0;
      if (bones.shoulderR1) bones.shoulderR1.rotation.z -= 0.0;
      // Slight elbow bend for natural pose
      if (bones.elbowL) bones.elbowL.rotation.y -= 0.3;
      if (bones.elbowR) bones.elbowR.rotation.y += 0.3;
      // Force update to apply rest pose
      if (skeleton) skeleton.update();
      console.log('[klukai_3d] Applied natural rest pose (arms down)');
    }

    // Re-save initial rotations AFTER rest pose so idle animations are relative to it
    if (targetSkeleton) {
      for (const bone of Object.values(bones)) {
        if (bone) {
          bone._initialRot = bone.rotation.clone();
          bone._initialPos = bone.position.clone();
        }
      }
    }

    console.log('[klukai_3d] Bones found:', Object.keys(bones));
    console.log('[klukai_3d] Morphs found:', Object.keys(morphs));

    // Schedule first blink
    nextBlinkTime = 2 + Math.random() * 3;
  }

  // ── Morph target helpers ──────────────────────────────────

  function setMorph(name, value) {
    const m = morphs[name];
    if (m) {
      m.mesh.morphTargetInfluences[m.index] = Math.max(0, Math.min(1, value));
    }
  }

  // ── Procedural animation (called every frame) ─────────────

  function updateAnimations(dt) {
    elapsed += dt;
    const p = IDLE_PARAMS[currentMoodGroup] || IDLE_PARAMS.relaxed;

    // ── Breathing (spine/chest vertical oscillation) ──
    if (bones.spine1) {
      const breathOffset = Math.sin(elapsed * p.swaySpeed * 2.5) * p.breathAmp;
      bones.spine1.rotation.x = (bones.spine1._initialRot?.x || 0) + breathOffset;
    }

    // ── Body sway (root side-to-side) ──
    if (bones.root) {
      const swayZ = Math.sin(elapsed * p.swaySpeed) * p.swayAmp;
      const swayX = Math.cos(elapsed * p.swaySpeed * 0.7) * p.swayAmp * 0.3;
      bones.root.rotation.z = (bones.root._initialRot?.z || 0) + swayZ;
      bones.root.rotation.x = (bones.root._initialRot?.x || 0) + swayX;
    }

    // ── Head idle movement ──
    if (bones.head && !reactionActive) {
      const headY = Math.sin(elapsed * p.swaySpeed * 0.8) * p.headTilt;
      const headX = Math.cos(elapsed * p.swaySpeed * 0.5) * p.headTilt * 0.5;

      // Blend with look-at target
      lookCurrentX += (lookTargetX - lookCurrentX) * 0.05;
      lookCurrentY += (lookTargetY - lookCurrentY) * 0.05;

      const baseX = (bones.head._initialRot?.x || 0);
      const baseY = (bones.head._initialRot?.y || 0);
      bones.head.rotation.x = baseX + headX + lookCurrentY * 0.3;
      bones.head.rotation.y = baseY + headY + lookCurrentX * 0.4;
    }

    // ── Blink (shape keys) ──
    if (elapsed >= nextBlinkTime && blinkPhase < 0) {
      blinkPhase = 0;
    }
    if (blinkPhase >= 0) {
      blinkPhase += dt * 8; // ~125ms per blink
      let blinkVal = 0;
      if (blinkPhase < 0.5) {
        blinkVal = blinkPhase * 2; // closing
      } else if (blinkPhase < 1.0) {
        blinkVal = (1.0 - blinkPhase) * 2; // opening
      } else {
        blinkPhase = -1;
        blinkVal = 0;
        nextBlinkTime = elapsed + 2 + Math.random() * 4;
      }
      setMorph('Eyes_Close_Down_Right', blinkVal);
      setMorph('Eyes_Close_Down_Left', blinkVal);
      setMorph('Eyes_Close_Up_Right', blinkVal * 0.3);
      setMorph('Eyes_Close_Up_Left', blinkVal * 0.3);
    }

    // ── Talking (mouth shape keys) ──
    if (isTalking) {
      const mouthOpen = (Math.sin(elapsed * 12) * 0.3 + 0.3) *
                         (Math.sin(elapsed * 7.3) * 0.2 + 0.5);
      setMorph('Mouth_Happy', mouthOpen * 0.6);
      setMorph('Mouth_Smile', mouthOpen * 0.3);
    } else {
      setMorph('Mouth_Happy', 0);
      setMorph('Mouth_Smile', 0);
    }

    // ── Tap reaction ──
    if (reactionActive && bones.head) {
      const rt = elapsed - reactionStart;
      if (rt < 0.3) {
        // Quick tilt
        bones.head.rotation.y += Math.sin(rt * 10) * 0.1;
        bones.head.rotation.z = (bones.head._initialRot?.z || 0) + Math.sin(rt * 8) * 0.05;
      } else if (rt < 0.8) {
        // Settle back
        const settle = (rt - 0.3) / 0.5;
        bones.head.rotation.z = (bones.head._initialRot?.z || 0) * (1 - settle);
      } else {
        reactionActive = false;
      }
    }

    // ── Fidget ──
    if (fidgetActive && bones.head) {
      const ft = (elapsed - fidgetStartTime) / fidgetDuration;
      if (ft >= 1.0) {
        fidgetActive = false;
      } else {
        const ease = Math.sin(ft * Math.PI); // bell curve
        switch (fidgetType) {
          case 'look_around':
            bones.head.rotation.y += Math.sin(ft * Math.PI * 2) * 0.12 * ease;
            break;
          case 'head_tilt':
            bones.head.rotation.z += Math.sin(ft * Math.PI) * 0.06;
            break;
          case 'nod':
            bones.head.rotation.x += Math.sin(ft * Math.PI * 2) * 0.05 * ease;
            break;
          case 'weight_shift':
            if (bones.root) bones.root.position.x = (Math.sin(ft * Math.PI) * 0.03);
            break;
        }
      }
    }
  }

  function scheduleFidget() {
    if (isDisposed) return;
    const delay = (20 + Math.random() * 40) * 1000;
    fidgetTimer = setTimeout(() => {
      if (isDisposed || isDormMode || fidgetActive) return;
      const types = ['look_around', 'head_tilt', 'nod', 'weight_shift'];
      fidgetType = types[Math.floor(Math.random() * types.length)];
      fidgetActive = true;
      fidgetStartTime = elapsed;
      fidgetDuration = 1.5 + Math.random() * 1.5;
      scheduleFidget();
    }, delay);
  }

  function animate() {
    if (isDisposed) return;
    requestAnimationFrame(animate);
    const dt = clock.getDelta();
    updateAnimations(dt);
    // Force skeleton matrix recalculation after bone manipulation
    if (skeleton) skeleton.update();
    if (renderer && scene && camera) renderer.render(scene, camera);
  }

  function handleResize(canvas) {
    if (!renderer || !camera) return;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (w > 0 && h > 0 && (canvas.width !== w || canvas.height !== h)) {
      renderer.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    }
  }

  // ── Public bridge API ─────────────────────────────────────

  window.klukaiBridge = {
    async init(canvasId, modelUrl) {
      isDisposed = false;
      elapsed = 0;
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

      const resizeObserver = new ResizeObserver(() => handleResize(canvas));
      resizeObserver.observe(canvas);

      // Start render loop and fidgets
      animate();
      scheduleFidget();

      console.log('[klukai_3d] Canvas:', canvas.clientWidth, 'x', canvas.clientHeight);
      console.log('[klukai_3d] Initialized. Bones:', Object.keys(bones), 'Morphs:', Object.keys(morphs));
      return true;
    },

    setMood(moodName) {
      const group = MOOD_GROUPS[moodName] || 'relaxed';
      if (group !== currentMoodGroup) {
        currentMoodGroup = group;
        if (rimLight && RIM_COLORS[group]) {
          rimLight.color.setHex(RIM_COLORS[group]);
        }
      }
    },

    playReaction(reactionName) {
      reactionActive = true;
      reactionStart = elapsed;
    },

    setTalking(enabled) {
      isTalking = enabled;
    },

    setBlush(intensity) {
      setMorph('Mouth_Happy', Math.max(0, Math.min(1, intensity)));
    },

    lookAt(normX, normY) {
      if (!isDormMode) {
        lookTargetX = Math.max(-1, Math.min(1, normX));
        lookTargetY = Math.max(-1, Math.min(1, normY));
      }
    },

    setDormMode(enabled) {
      isDormMode = enabled;
      if (enabled) {
        currentMoodGroup = 'drowsy';
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
      if (renderer) {
        renderer.dispose();
        renderer.forceContextLoss();
      }
      scene = null; camera = null; renderer = null;
      model = null; skeleton = null; bones = {}; morphs = {};
      lookTargetX = 0; lookTargetY = 0;
      lookCurrentX = 0; lookCurrentY = 0;
      console.log('[klukai_3d] Disposed');
    },
  };

  console.log('[klukai_3d] Bridge ready');
})();
