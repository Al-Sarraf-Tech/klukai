# 3D Klukai Avatar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Three.js-rendered 3D avatar of Klukai alongside the existing chat UI with mood-reactive animations, tap interaction, speech bubbles, and voice toggle.

**Architecture:** Three.js runs in an HTML canvas overlaid on the Flutter web app via `dart:js_interop`, following the existing bridge pattern (pretext_bridge.js, audio_recorder.js). The avatar panel sits in a Row split on desktop and a collapsible strip on mobile. Backend adds one new WebSocket message type (`tap_interact`).

**Tech Stack:** Flutter Web, Three.js (r168+), GLTFLoader, AnimationMixer, dart:js_interop, SharedPreferences, FastAPI WebSocket

**Prerequisite:** A `klukai.glb` file with named animation clips must be placed at `web-build/assets/models/klukai.glb`. The code is built to work with any .glb — animation clip names are mapped via constants. Until the real model is ready, a placeholder .glb (any rigged character) can be used for development.

---

### Task 1: Three.js Bridge — `klukai_3d.js`

**Files:**
- Create: `web-build/js/klukai_3d.js`

This is the core rendering layer. It loads the .glb, manages the Three.js scene, and exposes the bridge API that Flutter calls via JS interop.

- [ ] **Step 1: Create the Three.js bridge file**

```javascript
// web-build/js/klukai_3d.js
(function () {
  'use strict';

  // Import Three.js from the module loaded in index.html
  // We use the global THREE set up by the import map

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

  // Mood-to-animation-group mapping (matches spec)
  const MOOD_GROUPS = {
    // Relaxed
    relaxed: 'relaxed', calm: 'relaxed', content: 'relaxed', neutral: 'relaxed', composed: 'relaxed',
    // Happy
    happy: 'happy', playful: 'happy', teasing: 'happy', smug: 'happy', confident: 'happy',
    quietly_pleased: 'happy', amused: 'happy',
    // Serious
    focused: 'serious', analytical: 'serious', commanding: 'serious', determined: 'serious',
    vigilant: 'serious', calculating: 'serious', prideful: 'serious',
    // Shy
    shy: 'shy', flustered: 'shy', embarrassed: 'shy', bashful: 'shy',
    // Combat
    hunting: 'combat', aggressive: 'combat', fierce: 'combat', alert: 'combat',
    combat_ready: 'combat', battle_ready: 'combat', adrenaline: 'combat', competitive: 'combat',
    // Tender
    tender: 'tender', devoted: 'tender', affectionate: 'tender', warm: 'tender',
    vulnerable: 'tender', yearning: 'tender', longing: 'tender', protective: 'tender',
    // Drowsy
    drowsy: 'drowsy', sleepy: 'drowsy', exhausted: 'drowsy', lazy: 'drowsy',
    // Melancholy
    sad: 'melancholy', lonely: 'melancholy', distant: 'melancholy', nostalgic: 'melancholy',
    worried: 'melancholy', melancholic: 'melancholy', haunted: 'melancholy', conflicted: 'melancholy',
    exasperated: 'melancholy',
  };

  // Mood-group-to-rim-light color (hex)
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

  // Fidgets per mood group
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

  // Universal fidgets available to all groups
  const UNIVERSAL_FIDGETS = ['fidget_blink_hard', 'fidget_look_around', 'fidget_weight_shift'];

  function createScene(canvas) {
    scene = new THREE.Scene();
    clock = new THREE.Clock();

    // Camera — upper body framing
    camera = new THREE.PerspectiveCamera(30, canvas.clientWidth / canvas.clientHeight, 0.1, 100);
    camera.position.set(0, 1.2, 2.5);
    camera.lookAt(0, 1.0, 0);

    // Renderer — transparent background
    renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
    renderer.setSize(canvas.clientWidth, canvas.clientHeight);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.0;

    // Lighting
    const ambient = new THREE.AmbientLight(0xffffff, 0.6);
    scene.add(ambient);

    const dirLight = new THREE.DirectionalLight(0xffffff, 0.8);
    dirLight.position.set(2, 3, 2);
    scene.add(dirLight);

    // Mood-colored rim light
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

    // Center model
    const box = new THREE.Box3().setFromObject(model);
    const center = box.getCenter(new THREE.Vector3());
    model.position.sub(center);
    model.position.y += box.getSize(new THREE.Vector3()).y / 2;

    // Set up animation mixer
    mixer = new THREE.AnimationMixer(model);

    // Index all animation clips by name
    for (const clip of gltf.animations) {
      animations[clip.name] = clip;
    }

    // Start blink loop if available
    if (animations['blink']) {
      blinkAction = mixer.clipAction(animations['blink']);
      blinkAction.setLoop(THREE.LoopRepeat);
      blinkAction.weight = 1.0;
      blinkAction.play();
    }

    // Start default idle
    playMoodIdle('relaxed');

    // Start fidget timer
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

    // Update rim light color
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

    // Return to idle when done
    mixer.addEventListener('finished', function onFinished(e) {
      if (e.action === action) {
        mixer.removeEventListener('finished', onFinished);
        action.stop();
      }
    });
  }

  function scheduleFidget() {
    if (isDisposed) return;
    // Random interval 30-90 seconds
    const delay = (30 + Math.random() * 60) * 1000;
    fidgetTimer = setTimeout(() => {
      if (isDisposed || isDormMode) return;
      // Pick from mood-specific or universal fidgets
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

  // Head/eye look-at (clamped)
  let headBone = null;
  const maxRotX = 0.3;  // ~17 degrees
  const maxRotY = 0.4;  // ~23 degrees

  function updateLookAt(normX, normY) {
    if (!headBone || isDormMode) return;
    // normX, normY in range [-1, 1]
    const targetY = Math.max(-maxRotY, Math.min(maxRotY, normX * maxRotY));
    const targetX = Math.max(-maxRotX, Math.min(maxRotX, normY * maxRotX));
    // Smooth interpolation
    headBone.rotation.y += (targetY - headBone.rotation.y) * 0.1;
    headBone.rotation.x += (targetX - headBone.rotation.x) * 0.1;
  }

  // Public bridge API
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

      // Find head bone for look-at
      model.traverse((node) => {
        if (node.isBone && /head/i.test(node.name)) {
          headBone = node;
        }
      });

      // Handle resize
      const resizeObserver = new ResizeObserver(() => handleResize(canvas));
      resizeObserver.observe(canvas);

      // Start render loop
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
```

- [ ] **Step 2: Commit**

```bash
git add web-build/js/klukai_3d.js
git commit -m "feat: add Three.js bridge for 3D Klukai avatar"
```

---

### Task 2: Add Three.js and Script Tags to `index.html`

**Files:**
- Modify: `web-build/index.html`

Three.js needs to be loaded before klukai_3d.js. We use an import map so GLTFLoader is available as `THREE.GLTFLoader`.

- [ ] **Step 1: Download Three.js build files**

```bash
mkdir -p /home/jalsarraf/git/companion/web-build/js/three
curl -L -o /home/jalsarraf/git/companion/web-build/js/three/three.module.min.js \
  "https://cdn.jsdelivr.net/npm/three@0.168.0/build/three.module.min.js"
curl -L -o /home/jalsarraf/git/companion/web-build/js/three/GLTFLoader.js \
  "https://cdn.jsdelivr.net/npm/three@0.168.0/examples/jsm/loaders/GLTFLoader.js"
```

- [ ] **Step 2: Add a Three.js loader shim that exposes globals**

Create `web-build/js/three_loader.js`:

```javascript
// web-build/js/three_loader.js
// Loads Three.js as ES modules and exposes them as globals for klukai_3d.js
import * as THREE from './three/three.module.min.js';
import { GLTFLoader } from './three/GLTFLoader.js';
THREE.GLTFLoader = GLTFLoader;
window.THREE = THREE;
window.__threeReady = true;
console.log('[three_loader] Three.js ready');
```

- [ ] **Step 3: Add script tags to index.html**

In `web-build/index.html`, add these lines after the audio_recorder.js script and before flutter_bootstrap.js:

```html
  <!-- Three.js for 3D avatar -->
  <script type="module" src="js/three_loader.js"></script>
  <script src="js/klukai_3d.js" defer></script>
```

- [ ] **Step 4: Create assets/models directory**

```bash
mkdir -p /home/jalsarraf/git/companion/web-build/assets/models
```

- [ ] **Step 5: Commit**

```bash
git add web-build/js/three/ web-build/js/three_loader.js web-build/index.html web-build/assets/models/
git commit -m "feat: add Three.js loader and script tags for 3D avatar"
```

---

### Task 3: Flutter `KlukaiAvatar` Widget

**Files:**
- Create: `flutter_app/lib/widgets/klukai_avatar.dart`

This widget wraps an HTML canvas element and provides the Dart-side interop to call `klukaiBridge.*` methods.

- [ ] **Step 1: Create the widget**

```dart
// flutter_app/lib/widgets/klukai_avatar.dart
import 'dart:js_interop';
import 'package:flutter/material.dart';
import 'package:web/web.dart' as web;

@JS('klukaiBridge.init')
external JSPromise<JSBoolean> _jsInit(JSString canvasId, JSString modelUrl);

@JS('klukaiBridge.setMood')
external void _jsSetMood(JSString mood);

@JS('klukaiBridge.playReaction')
external void _jsPlayReaction(JSString reaction);

@JS('klukaiBridge.setTalking')
external void _jsSetTalking(JSBoolean enabled);

@JS('klukaiBridge.setBlush')
external void _jsSetBlush(JSNumber intensity);

@JS('klukaiBridge.lookAt')
external void _jsLookAt(JSNumber normX, JSNumber normY);

@JS('klukaiBridge.setDormMode')
external void _jsSetDormMode(JSBoolean enabled);

@JS('klukaiBridge.dispose')
external void _jsDispose();

@JS('window.__threeReady')
external JSBoolean? get _jsThreeReady;

class KlukaiAvatarController {
  bool _initialized = false;

  bool get isInitialized => _initialized;

  Future<bool> init(String canvasId, String modelUrl) async {
    if (_jsThreeReady?.toDart != true) {
      debugPrint('[KlukaiAvatar] Three.js not ready');
      return false;
    }
    try {
      final result = await _jsInit(canvasId.toJS, modelUrl.toJS).toDart;
      _initialized = result.toDart;
      return _initialized;
    } catch (e) {
      debugPrint('[KlukaiAvatar] Init failed: $e');
      return false;
    }
  }

  void setMood(String mood) {
    if (!_initialized) return;
    _jsSetMood(mood.toJS);
  }

  void playReaction(String reaction) {
    if (!_initialized) return;
    _jsPlayReaction(reaction.toJS);
  }

  void setTalking(bool enabled) {
    if (!_initialized) return;
    _jsSetTalking(enabled.toJS);
  }

  void setBlush(double intensity) {
    if (!_initialized) return;
    _jsSetBlush(intensity.toJS);
  }

  void lookAt(double normX, double normY) {
    if (!_initialized) return;
    _jsLookAt(normX.toJS, normY.toJS);
  }

  void setDormMode(bool enabled) {
    if (!_initialized) return;
    _jsSetDormMode(enabled.toJS);
  }

  void dispose() {
    if (!_initialized) return;
    _jsDispose();
    _initialized = false;
  }
}

class KlukaiAvatar extends StatefulWidget {
  final String modelUrl;
  final KlukaiAvatarController controller;
  final VoidCallback? onTap;

  const KlukaiAvatar({
    super.key,
    required this.modelUrl,
    required this.controller,
    this.onTap,
  });

  @override
  State<KlukaiAvatar> createState() => _KlukaiAvatarState();
}

class _KlukaiAvatarState extends State<KlukaiAvatar> {
  static int _nextId = 0;
  late final String _canvasId;
  bool _ready = false;

  @override
  void initState() {
    super.initState();
    _canvasId = 'klukai-canvas-${_nextId++}';
    // Wait for the HTML element to be in the DOM, then init Three.js
    WidgetsBinding.instance.addPostFrameCallback((_) => _initCanvas());
  }

  Future<void> _initCanvas() async {
    // Small delay to ensure the platform view is mounted
    await Future.delayed(const Duration(milliseconds: 200));
    final success = await widget.controller.init(_canvasId, widget.modelUrl);
    if (mounted) {
      setState(() => _ready = success);
    }
  }

  @override
  void dispose() {
    widget.controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: widget.onTap,
      onPanUpdate: (details) {
        // Head tracking — normalize to [-1, 1]
        final box = context.findRenderObject() as RenderBox?;
        if (box == null) return;
        final size = box.size;
        final normX = (details.localPosition.dx / size.width) * 2 - 1;
        final normY = -((details.localPosition.dy / size.height) * 2 - 1);
        widget.controller.lookAt(normX, normY);
      },
      child: HtmlElementView.fromTagName(
        tagName: 'canvas',
        onElementCreated: (element) {
          final canvas = element as web.HTMLCanvasElement;
          canvas.id = _canvasId;
          canvas.style.width = '100%';
          canvas.style.height = '100%';
          canvas.style.display = 'block';
        },
      ),
    );
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add flutter_app/lib/widgets/klukai_avatar.dart
git commit -m "feat: add KlukaiAvatar Flutter widget with JS interop"
```

---

### Task 4: Speech Bubble Widget

**Files:**
- Create: `flutter_app/lib/widgets/speech_bubble.dart`

Floating speech bubble that streams text and auto-fades.

- [ ] **Step 1: Create the widget**

```dart
// flutter_app/lib/widgets/speech_bubble.dart
import 'dart:async';
import 'package:flutter/material.dart';
import '../main.dart';

class SpeechBubble extends StatefulWidget {
  final String text;
  final bool isStreaming;
  final VoidCallback? onDismiss;

  const SpeechBubble({
    super.key,
    required this.text,
    this.isStreaming = false,
    this.onDismiss,
  });

  @override
  State<SpeechBubble> createState() => _SpeechBubbleState();
}

class _SpeechBubbleState extends State<SpeechBubble>
    with SingleTickerProviderStateMixin {
  late AnimationController _fadeController;
  Timer? _fadeTimer;

  @override
  void initState() {
    super.initState();
    _fadeController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 500),
      value: 1.0,
    );
    _scheduleFade();
  }

  @override
  void didUpdateWidget(SpeechBubble old) {
    super.didUpdateWidget(old);
    if (widget.text != old.text || widget.isStreaming != old.isStreaming) {
      // New content — reset visibility
      _fadeController.value = 1.0;
      _scheduleFade();
    }
  }

  void _scheduleFade() {
    _fadeTimer?.cancel();
    if (!widget.isStreaming && widget.text.isNotEmpty) {
      _fadeTimer = Timer(const Duration(seconds: 5), () {
        if (mounted) {
          _fadeController.reverse().then((_) {
            widget.onDismiss?.call();
          });
        }
      });
    }
  }

  @override
  void dispose() {
    _fadeTimer?.cancel();
    _fadeController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (widget.text.isEmpty) return const SizedBox.shrink();

    // Truncate to ~100 chars for display
    final display = widget.text.length > 100
        ? '${widget.text.substring(0, 100)}...'
        : widget.text;

    return GestureDetector(
      onTap: () {
        _fadeController.reverse().then((_) {
          widget.onDismiss?.call();
        });
      },
      child: FadeTransition(
        opacity: _fadeController,
        child: Container(
          constraints: const BoxConstraints(maxWidth: 240),
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          decoration: BoxDecoration(
            color: GFL2Colors.surface,
            borderRadius: const BorderRadius.only(
              topLeft: Radius.circular(12),
              topRight: Radius.circular(12),
              bottomRight: Radius.circular(12),
              bottomLeft: Radius.circular(4),
            ),
            border: Border.all(
              color: GFL2Colors.primary.withValues(alpha: 0.4),
            ),
            boxShadow: [
              BoxShadow(
                color: GFL2Colors.primary.withValues(alpha: 0.1),
                blurRadius: 8,
              ),
            ],
          ),
          child: Text(
            display,
            style: const TextStyle(
              color: GFL2Colors.textPrimary,
              fontSize: 12,
              height: 1.4,
              fontFamily: 'monospace',
            ),
          ),
        ),
      ),
    );
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add flutter_app/lib/widgets/speech_bubble.dart
git commit -m "feat: add SpeechBubble widget with auto-fade and streaming"
```

---

### Task 5: Avatar Panel (Desktop) and Avatar Strip (Mobile)

**Files:**
- Create: `flutter_app/lib/widgets/klukai_avatar_panel.dart`
- Create: `flutter_app/lib/widgets/klukai_avatar_strip.dart`

- [ ] **Step 1: Create the desktop avatar panel**

```dart
// flutter_app/lib/widgets/klukai_avatar_panel.dart
import 'package:flutter/material.dart';
import '../main.dart';
import 'klukai_avatar.dart';
import 'speech_bubble.dart';

class KlukaiAvatarPanel extends StatelessWidget {
  final KlukaiAvatarController controller;
  final String modelUrl;
  final String speechText;
  final bool isSpeechStreaming;
  final bool audioEnabled;
  final Color moodGlowColor;
  final VoidCallback onTap;
  final VoidCallback onAudioToggle;
  final VoidCallback onSpeechDismiss;

  const KlukaiAvatarPanel({
    super.key,
    required this.controller,
    required this.modelUrl,
    required this.speechText,
    required this.isSpeechStreaming,
    required this.audioEnabled,
    required this.moodGlowColor,
    required this.onTap,
    required this.onAudioToggle,
    required this.onSpeechDismiss,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [
            const Color(0xFF0A0D14),
            GFL2Colors.background,
            const Color(0xFF141822),
          ],
        ),
        border: Border(
          right: BorderSide(
            color: GFL2Colors.border.withValues(alpha: 0.4),
          ),
        ),
      ),
      child: Stack(
        children: [
          // Mood glow background
          Positioned.fill(
            child: Center(
              child: AnimatedContainer(
                duration: const Duration(milliseconds: 600),
                width: 200,
                height: 200,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  boxShadow: [
                    BoxShadow(
                      color: moodGlowColor.withValues(alpha: 0.15),
                      blurRadius: 80,
                      spreadRadius: 20,
                    ),
                  ],
                ),
              ),
            ),
          ),
          // 3D model
          Positioned.fill(
            child: Padding(
              padding: const EdgeInsets.only(bottom: 100),
              child: KlukaiAvatar(
                modelUrl: modelUrl,
                controller: controller,
                onTap: onTap,
              ),
            ),
          ),
          // Speech bubble — above the model
          Positioned(
            left: 12,
            right: 12,
            bottom: 60,
            child: SpeechBubble(
              text: speechText,
              isStreaming: isSpeechStreaming,
              onDismiss: onSpeechDismiss,
            ),
          ),
          // Audio toggle
          Positioned(
            top: 8,
            right: 8,
            child: IconButton(
              onPressed: onAudioToggle,
              icon: Icon(
                audioEnabled ? Icons.volume_up : Icons.volume_off,
                color: audioEnabled
                    ? GFL2Colors.primary
                    : GFL2Colors.textDim.withValues(alpha: 0.4),
                size: 20,
              ),
              style: IconButton.styleFrom(
                backgroundColor: GFL2Colors.surface.withValues(alpha: 0.6),
                fixedSize: const Size(32, 32),
              ),
            ),
          ),
          // Tap hint (shows initially, fades)
          Positioned(
            bottom: 16,
            left: 0,
            right: 0,
            child: Center(
              child: Text(
                'TAP TO INTERACT',
                style: TextStyle(
                  color: GFL2Colors.textDim.withValues(alpha: 0.3),
                  fontSize: 9,
                  letterSpacing: 2,
                  fontFamily: 'monospace',
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
```

- [ ] **Step 2: Create the mobile avatar strip**

```dart
// flutter_app/lib/widgets/klukai_avatar_strip.dart
import 'package:flutter/material.dart';
import '../main.dart';
import 'klukai_avatar.dart';
import 'speech_bubble.dart';

class KlukaiAvatarStrip extends StatefulWidget {
  final KlukaiAvatarController controller;
  final String modelUrl;
  final String speechText;
  final bool isSpeechStreaming;
  final bool audioEnabled;
  final Color moodGlowColor;
  final VoidCallback onTap;
  final VoidCallback onAudioToggle;
  final VoidCallback onSpeechDismiss;

  const KlukaiAvatarStrip({
    super.key,
    required this.controller,
    required this.modelUrl,
    required this.speechText,
    required this.isSpeechStreaming,
    required this.audioEnabled,
    required this.moodGlowColor,
    required this.onTap,
    required this.onAudioToggle,
    required this.onSpeechDismiss,
  });

  @override
  State<KlukaiAvatarStrip> createState() => _KlukaiAvatarStripState();
}

class _KlukaiAvatarStripState extends State<KlukaiAvatarStrip> {
  bool _collapsed = false;

  @override
  Widget build(BuildContext context) {
    if (_collapsed) {
      return GestureDetector(
        onTap: () => setState(() => _collapsed = false),
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 300),
          height: 4,
          decoration: BoxDecoration(
            gradient: LinearGradient(
              colors: [
                widget.moodGlowColor.withValues(alpha: 0.6),
                widget.moodGlowColor.withValues(alpha: 0.1),
              ],
            ),
          ),
        ),
      );
    }

    return AnimatedContainer(
      duration: const Duration(milliseconds: 300),
      height: 120,
      decoration: BoxDecoration(
        gradient: LinearGradient(
          colors: [const Color(0xFF0A0D14), GFL2Colors.background],
        ),
        border: Border(
          bottom: BorderSide(
            color: GFL2Colors.border.withValues(alpha: 0.4),
          ),
        ),
      ),
      child: Row(
        children: [
          // 3D model compact view
          SizedBox(
            width: 90,
            child: Stack(
              children: [
                KlukaiAvatar(
                  modelUrl: widget.modelUrl,
                  controller: widget.controller,
                  onTap: widget.onTap,
                ),
                // Mood glow
                Positioned.fill(
                  child: IgnorePointer(
                    child: Container(
                      decoration: BoxDecoration(
                        boxShadow: [
                          BoxShadow(
                            color: widget.moodGlowColor.withValues(alpha: 0.1),
                            blurRadius: 20,
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
          // Speech bubble
          Expanded(
            child: Padding(
              padding: const EdgeInsets.all(8),
              child: SpeechBubble(
                text: widget.speechText,
                isStreaming: widget.isSpeechStreaming,
                onDismiss: widget.onSpeechDismiss,
              ),
            ),
          ),
          // Controls column
          Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              // Audio toggle
              IconButton(
                onPressed: widget.onAudioToggle,
                icon: Icon(
                  widget.audioEnabled ? Icons.volume_up : Icons.volume_off,
                  color: widget.audioEnabled
                      ? GFL2Colors.primary
                      : GFL2Colors.textDim.withValues(alpha: 0.4),
                  size: 18,
                ),
                iconSize: 18,
                constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
              ),
              // Collapse chevron
              IconButton(
                onPressed: () => setState(() => _collapsed = true),
                icon: Icon(
                  Icons.keyboard_arrow_up,
                  color: GFL2Colors.textDim.withValues(alpha: 0.4),
                  size: 18,
                ),
                iconSize: 18,
                constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
```

- [ ] **Step 3: Commit**

```bash
git add flutter_app/lib/widgets/klukai_avatar_panel.dart flutter_app/lib/widgets/klukai_avatar_strip.dart
git commit -m "feat: add KlukaiAvatarPanel (desktop) and KlukaiAvatarStrip (mobile)"
```

---

### Task 6: Modify ChatScreen — Layout Split, Toggle, Tap Routing

**Files:**
- Modify: `flutter_app/lib/screens/chat_screen.dart`

This is the largest task. Adds: avatar toggle state, audio toggle state, speech bubble state, tap interaction routing, responsive Row/Column layout, dorm mode sync.

- [ ] **Step 1: Add imports and new state variables**

At the top of `chat_screen.dart`, after the existing imports (line 19), add:

```dart
import '../widgets/klukai_avatar.dart';
import '../widgets/klukai_avatar_panel.dart';
import '../widgets/klukai_avatar_strip.dart';
import '../widgets/speech_bubble.dart';
import 'package:shared_preferences/shared_preferences.dart';
```

In `_ChatScreenState`, after `final bool _soundMuted = false;` (line 49), add:

```dart
  // 3D Avatar state
  bool _avatarEnabled = false;
  bool _audioEnabled = false;
  final _avatarController = KlukaiAvatarController();
  String _speechBubbleText = '';
  bool _isSpeechStreaming = false;
  DateTime? _lastAssistantMessageTime;
  DateTime? _lastTapTime;
  String _lastAssistantContent = '';
```

- [ ] **Step 2: Add preference loading in initState**

Replace the existing `initState` (lines 98-103) with:

```dart
  @override
  void initState() {
    super.initState();
    _loadHistory();
    _loadAffection();
    _connectWS();
    _loadAvatarPrefs();
  }

  Future<void> _loadAvatarPrefs() async {
    final prefs = await SharedPreferences.getInstance();
    setState(() {
      _avatarEnabled = prefs.getBool('avatar_enabled') ?? false;
      _audioEnabled = prefs.getBool('avatar_audio_enabled') ?? false;
    });
  }

  Future<void> _toggleAvatar() async {
    final prefs = await SharedPreferences.getInstance();
    final newVal = !_avatarEnabled;
    await prefs.setBool('avatar_enabled', newVal);
    setState(() {
      _avatarEnabled = newVal;
      if (!newVal) {
        _avatarController.dispose();
        _speechBubbleText = '';
      }
    });
  }

  Future<void> _toggleAudio() async {
    final prefs = await SharedPreferences.getInstance();
    final newVal = !_audioEnabled;
    await prefs.setBool('avatar_audio_enabled', newVal);
    setState(() => _audioEnabled = newVal);
  }
```

- [ ] **Step 3: Add tap interaction handler**

After `_toggleAudio`, add:

```dart
  void _handleAvatarTap() {
    // Anti-spam: 5 second cooldown
    final now = DateTime.now();
    if (_lastTapTime != null &&
        now.difference(_lastTapTime!).inSeconds < 5) {
      return;
    }
    _lastTapTime = now;

    // Play tap reaction animation
    _avatarController.playReaction('reaction_tap');

    // Smart routing: recent message = replay, stale = proactive
    if (_lastAssistantMessageTime != null &&
        now.difference(_lastAssistantMessageTime!).inSeconds < 30 &&
        _lastAssistantContent.isNotEmpty) {
      // Replay last message
      setState(() {
        _speechBubbleText = _lastAssistantContent;
        _isSpeechStreaming = false;
      });
      if (_audioEnabled) {
        _playTTS(_lastAssistantContent);
      }
    } else {
      // Proactive comment
      _ws.send({'type': 'tap_interact'});
    }
  }

  Future<void> _playTTS(String text) async {
    _avatarController.setTalking(true);
    try {
      final response = await http.post(
        Uri.parse('${widget.serverUrl}/api/tts'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({'text': text, 'language': 'en'}),
      );
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        final audioData = data['audio'] as String?;
        if (audioData != null) {
          final dataUrl = 'data:audio/wav;base64,$audioData';
          final audio = web.HTMLAudioElement()..src = dataUrl;
          audio.onEnded.listen((_) {
            _avatarController.setTalking(false);
          });
          audio.play();
          return;
        }
      }
    } catch (e) {
      debugPrint('TTS failed: $e');
    }
    _avatarController.setTalking(false);
  }

  void _dismissSpeechBubble() {
    setState(() => _speechBubbleText = '');
  }
```

- [ ] **Step 4: Update `_handleWSMessage` to sync avatar state**

In `_handleWSMessage`, update the `case 'token':` block (around line 158). After `_scrollToBottom(instant: true);` add:

```dart
        // Mirror streaming to speech bubble
        if (_avatarEnabled) {
          setState(() {
            _speechBubbleText = _streamingBuffer;
            _isSpeechStreaming = true;
          });
        }
```

Update the `case 'done':` block (around line 179). After `_playNotificationSound();` add:

```dart
        // Update avatar state
        if (_avatarEnabled) {
          _lastAssistantMessageTime = DateTime.now();
          final completedMsg = completedIdx != null && completedIdx! >= 0
              ? _messages[completedIdx!]
              : null;
          if (completedMsg != null) {
            _lastAssistantContent = completedMsg.content;
          }
          setState(() => _isSpeechStreaming = false);
          // Auto-TTS if audio enabled
          if (_audioEnabled && _lastAssistantContent.isNotEmpty) {
            _playTTS(_lastAssistantContent);
          }
        }
```

Update the `case 'mood':` block (around line 203). After the existing setState, add:

```dart
        if (_avatarEnabled) {
          _avatarController.setMood(msg['mood'] as String? ?? 'composed');
        }
```

Update the `case 'proactive':` block (around line 277). After `_playNotificationSound();` add:

```dart
        if (_avatarEnabled) {
          _lastAssistantMessageTime = DateTime.now();
          _lastAssistantContent = message;
          setState(() {
            _speechBubbleText = message;
            _isSpeechStreaming = false;
          });
          if (_audioEnabled) {
            _playTTS(message);
          }
        }
```

- [ ] **Step 5: Update the build method for responsive layout**

Replace the `build` method (starting at line 472) with:

```dart
  @override
  Widget build(BuildContext context) {
    final isWide = MediaQuery.of(context).size.width > 768;

    // Sync dorm mode with avatar
    if (_avatarEnabled && _avatarController.isInitialized) {
      _avatarController.setDormMode(_isDormMode);
    }

    return Scaffold(
      backgroundColor: _bgColor,
      body: SafeArea(
        child: isWide && _avatarEnabled
            ? _buildDesktopLayout()
            : _buildMobileLayout(),
      ),
    );
  }

  Widget _buildDesktopLayout() {
    return Row(
      children: [
        // Left: 3D avatar panel (35%)
        SizedBox(
          width: MediaQuery.of(context).size.width * 0.35,
          child: KlukaiAvatarPanel(
            controller: _avatarController,
            modelUrl: 'assets/models/klukai.glb',
            speechText: _speechBubbleText,
            isSpeechStreaming: _isSpeechStreaming,
            audioEnabled: _audioEnabled,
            moodGlowColor: _moodGlowColor,
            onTap: _handleAvatarTap,
            onAudioToggle: _toggleAudio,
            onSpeechDismiss: _dismissSpeechBubble,
          ),
        ),
        // Right: existing chat UI (65%)
        Expanded(
          child: Column(
            children: [
              _buildHeader(),
              Expanded(child: _buildMessageList()),
              if (_activeTools.isNotEmpty) _buildToolStatus(),
              if (_state.isTyping && _streamingId == null)
                _buildProcessingIndicator(),
              _buildInputBar(),
            ],
          ),
        ),
      ],
    );
  }

  Widget _buildMobileLayout() {
    return Column(
      children: [
        _buildHeader(),
        if (_avatarEnabled)
          KlukaiAvatarStrip(
            controller: _avatarController,
            modelUrl: 'assets/models/klukai.glb',
            speechText: _speechBubbleText,
            isSpeechStreaming: _isSpeechStreaming,
            audioEnabled: _audioEnabled,
            moodGlowColor: _moodGlowColor,
            onTap: _handleAvatarTap,
            onAudioToggle: _toggleAudio,
            onSpeechDismiss: _dismissSpeechBubble,
          ),
        Expanded(child: _buildMessageList()),
        if (_activeTools.isNotEmpty) _buildToolStatus(),
        if (_state.isTyping && _streamingId == null)
          _buildProcessingIndicator(),
        _buildInputBar(),
      ],
    );
  }
```

- [ ] **Step 6: Add avatar toggle button to header**

In `_buildHeader`, inside the `Row` after the `Expanded` column (around line 617 area, after `MoodIndicator`), but actually in the main Row that contains the portrait + expanded column, add the avatar toggle button. Find the closing `],` of the inner Row's children and add before it:

```dart
              // Avatar toggle
              const SizedBox(width: 8),
              IconButton(
                onPressed: _toggleAvatar,
                icon: Icon(
                  _avatarEnabled ? Icons.view_in_ar : Icons.view_in_ar_outlined,
                  color: _avatarEnabled
                      ? GFL2Colors.primary
                      : GFL2Colors.textDim.withValues(alpha: 0.4),
                  size: 20,
                ),
                style: IconButton.styleFrom(
                  fixedSize: const Size(32, 32),
                  padding: EdgeInsets.zero,
                ),
                tooltip: _avatarEnabled ? 'Hide 3D Avatar' : 'Show 3D Avatar',
              ),
```

- [ ] **Step 7: Update dispose to clean up avatar**

In `dispose()` (around line 463), add before `super.dispose()`:

```dart
    if (_avatarEnabled) {
      _avatarController.dispose();
    }
```

- [ ] **Step 8: Commit**

```bash
git add flutter_app/lib/screens/chat_screen.dart
git commit -m "feat: integrate 3D avatar into ChatScreen with toggle, tap routing, and responsive layout"
```

---

### Task 7: Backend — `tap_interact` WebSocket Handler

**Files:**
- Modify: `docker/core/app/main.py` (lines 447-456, the message type dispatch)

- [ ] **Step 1: Add tap_interact handler in the WebSocket dispatch**

In `main.py`, find the message type dispatch block (around line 447):

```python
            if msg_type == "message":
                await _handle_message(data.get("content", ""), session, user_id)
            elif msg_type == "typing":
                pass
            elif msg_type == "voice_end":
                audio = data.get("audio")
                if audio:
                    await _handle_voice(audio, session)
```

Add after the `voice_end` block:

```python
            elif msg_type == "tap_interact":
                await _handle_tap_interact(session, user_id)
```

- [ ] **Step 2: Add the `_handle_tap_interact` function**

Add this function before the `websocket_endpoint` function:

```python
async def _handle_tap_interact(session, user_id: str) -> None:
    """Handle tap interaction — generate a short proactive comment."""
    if proactive_engine and not proactive_engine._can_send():
        return
    # Generate a short contextual line
    prompt = (
        "The Commander just tapped you to get your attention. "
        "Respond with a single short, natural line — a brief comment, observation, or greeting "
        "appropriate to your current mood. Keep it under 30 words. Do not ask a question."
    )
    try:
        await _handle_message(prompt, session, user_id, hidden=True)
    except Exception as e:
        log.warning(f"tap_interact failed: {e}")
```

- [ ] **Step 3: Verify `_handle_message` supports a `hidden` parameter**

Check if `_handle_message` already has a `hidden` parameter. If not, add `hidden: bool = False` to its signature and skip saving the user message to history when `hidden=True`. The exact change depends on the function signature — the hidden prompt should not appear as a user message in chat history.

If `_handle_message` doesn't support `hidden`, an alternative is to directly call the LLM with the proactive prompt and stream the response, similar to how `proactive.py` generates messages. In that case, replace `_handle_tap_interact` with:

```python
async def _handle_tap_interact(session, user_id: str) -> None:
    """Handle tap interaction — generate a short proactive comment via existing proactive system."""
    if proactive_engine:
        await proactive_engine.trigger_tap()
```

And add a `trigger_tap` method to the ProactiveEngine class in `proactive.py`:

```python
    async def trigger_tap(self) -> None:
        """Generate a tap-interaction response."""
        if not self._can_send():
            return
        messages = [
            "Hm? Need something, Commander?",
            "Right here.",
            "You have my attention.",
        ]
        # Use LLM for contextual response if available, else pick random
        import random
        await self._deliver(random.choice(messages))
```

- [ ] **Step 4: Commit**

```bash
git add docker/core/app/main.py docker/core/app/proactive.py
git commit -m "feat: add tap_interact WebSocket handler for avatar interaction"
```

---

### Task 8: Nginx Cache Headers for .glb

**Files:**
- Modify: `gateway/nginx.conf` (or equivalent nginx config)

- [ ] **Step 1: Add cache headers for model assets**

Find the location block that serves static files (likely `location /app/`) and add:

```nginx
    location ~* \.glb$ {
        add_header Cache-Control "public, max-age=604800, immutable";
    }
```

- [ ] **Step 2: Commit**

```bash
git add gateway/
git commit -m "feat: add nginx cache headers for .glb model assets"
```

---

### Task 9: Build and Verify

- [ ] **Step 1: Add `shared_preferences` to pubspec.yaml if missing**

Check `flutter_app/pubspec.yaml` for `shared_preferences`. If missing:

```bash
cd /home/jalsarraf/git/companion/flutter_app && /home/jalsarraf/flutter/bin/flutter pub add shared_preferences
```

- [ ] **Step 2: Build the Flutter web app**

```bash
export PATH="$PATH:$HOME/flutter/bin"
cd /home/jalsarraf/git/companion/flutter_app && flutter build web --release --base-href /app/
```

- [ ] **Step 3: Copy build to web-build (preserving custom JS files)**

```bash
cd /home/jalsarraf/git/companion
# Copy Flutter build output
cp -r flutter_app/build/web/* web-build/
# Verify custom JS files survived (they live in web-build/js/ which Flutter doesn't touch)
ls -la web-build/js/klukai_3d.js web-build/js/three_loader.js web-build/js/three/
```

- [ ] **Step 4: Verify index.html has the Three.js script tags**

```bash
grep -n "three_loader\|klukai_3d" web-build/index.html
```

Expected: two lines showing the script tags.

- [ ] **Step 5: Commit the build**

```bash
git add flutter_app/pubspec.yaml flutter_app/pubspec.lock
git commit -m "feat: add shared_preferences dependency for avatar toggle persistence"
```

---

### Notes

**Model file (`klukai.glb`):** This plan implements all the infrastructure. The actual model file must be created separately via the asset pipeline described in the design spec (extract GFL2 → Blender → .glb). The code works with any .glb that has the expected animation clip names. Missing clips are handled gracefully (the bridge skips them).

**Expected animation clip names in the .glb:**
- Idles: `idle_relaxed`, `idle_happy`, `idle_serious`, `idle_shy`, `idle_combat`, `idle_tender`, `idle_drowsy`, `idle_melancholy`
- Fidgets: `fidget_hair`, `fidget_stretch`, `fidget_smile`, `fidget_weapon`, `fidget_scan`, `fidget_tuck_hair`, `fidget_look_away`, `fidget_yawn`, `fidget_head_nod`, `fidget_rub_eyes`, `fidget_blink_hard`, `fidget_look_around`, `fidget_weight_shift`
- Reactions: `reaction_tap`, `reaction_milestone`, `reaction_surprise`
- Other: `blink`, `talking`
