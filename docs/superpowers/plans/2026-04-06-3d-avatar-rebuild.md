# 3D Klukai Avatar Rebuild — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Get Klukai's 3D model out of T-pose with a proper idle animation (hands on hips or arms relaxed at sides), blinking, breathing, and clean rendering — no visual artifacts.

**Architecture:** Use Mixamo to auto-rig and provide an idle animation for the Klukai model. Export from Blender as a single clean .glb with the animation baked in. Three.js AnimationMixer plays it. No manual bone rotation in JS — the Blender glTF exporter handles all coordinate conversion automatically.

**Tech Stack:** Mixamo (free, browser-based), Blender 5.0, Three.js r168 GLTFLoader + AnimationMixer

---

## Why Mixamo?

Every previous attempt failed because we tried to manually compute bone rotations in either Blender Python or Three.js JavaScript. The Blender-to-glTF bone coordinate conversion is bone-specific (depends on each bone's rest orientation matrix) with no simple formula. Mixamo solves this entirely:

1. Upload the model → Mixamo auto-rigs it with a standard humanoid skeleton
2. Pick an idle animation from Mixamo's library (hundreds available)
3. Download as FBX with the animation baked in
4. Import to Blender → export as .glb
5. Three.js AnimationMixer plays it — coordinates are correct because the exporter handles everything

This is the workflow used by every Three.js developer who loads animated characters (documented by Don McCurdy, Three.js maintainer).

---

### Task 1: Prepare Klukai Model for Mixamo Upload

**Files:**
- Create: `tools/prepare_for_mixamo.py` (Blender script)
- Output: `/tmp/klukai_for_mixamo.fbx`

Mixamo needs a clean mesh in FBX format — no multiple armatures, no duplicate meshes, no accessories that confuse the auto-rigger.

- [ ] **Step 1: Write the Blender cleanup + FBX export script**

```python
"""
Blender script: Clean Klukai model and export FBX for Mixamo upload.
Usage: blender --background --python tools/prepare_for_mixamo.py -- /Youtube/Klukai_V1.blend /tmp/klukai_for_mixamo.fbx
"""
import bpy
import sys

argv = sys.argv
if "--" in argv:
    argv = argv[argv.index("--") + 1:]
INPUT = argv[0]
OUTPUT = argv[1]

bpy.ops.wm.open_mainfile(filepath=INPUT)

# Make everything visible
for col in bpy.data.collections:
    col.hide_viewport = False
    col.hide_render = False
for obj in bpy.data.objects:
    obj.hide_viewport = False
    obj.hide_render = False
    try: obj.hide_set(False)
    except: pass
def enable_lc(lc):
    lc.exclude = False
    lc.hide_viewport = False
    for c in lc.children: enable_lc(c)
enable_lc(bpy.context.view_layer.layer_collection)

# Delete everything except: Body mesh, Face mesh, Hair mesh, and Klukai armature
# Keep only the essential default-skin meshes
KEEP_OBJECTS = {'Klukai'}  # The armature
KEEP_MESH_KEYWORDS = ['Body', 'Face', 'Hair', 'Jacket', 'Legs', 'Feet', 'Gloves', 'Shoes']
REMOVE_KEYWORDS = ['Dorm', 'Speed Star', 'Astral', 'Cerulean', 'Fight',
                   'Pistol', 'HK416', 'Axe', 'Baton', 'Magazine', 'Holster',
                   'Flashbang', 'Radio', 'Bag', 'Ring', 'Bracelet', 'Watch',
                   'Cube', 'Camera', 'Silencer', 'Grenade', 'Rig']

for obj in list(bpy.data.objects):
    if obj.name in KEEP_OBJECTS:
        continue
    if obj.type == 'ARMATURE' and obj.name != 'Klukai':
        bpy.data.objects.remove(obj, do_unlink=True)
        continue
    if obj.type == 'MESH':
        # Remove if matches remove keywords
        if any(kw in obj.name for kw in REMOVE_KEYWORDS):
            bpy.data.objects.remove(obj, do_unlink=True)
            continue
        # Keep if matches keep keywords
        if any(kw in obj.name for kw in KEEP_MESH_KEYWORDS):
            continue
        # Remove everything else
        bpy.data.objects.remove(obj, do_unlink=True)
        continue
    # Remove non-mesh, non-armature objects
    if obj.type not in ('MESH', 'ARMATURE'):
        bpy.data.objects.remove(obj, do_unlink=True)

remaining = [obj.name for obj in bpy.data.objects]
print(f"[prep] Remaining objects ({len(remaining)}): {remaining}")

# Join all remaining meshes into one object for Mixamo
meshes = [obj for obj in bpy.data.objects if obj.type == 'MESH']
if meshes:
    bpy.ops.object.select_all(action='DESELECT')
    for m in meshes:
        m.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.object.join()
    bpy.context.active_object.name = 'Klukai_Body'
    print(f"[prep] Joined {len(meshes)} meshes into Klukai_Body")

# Export as FBX
bpy.ops.export_scene.fbx(
    filepath=OUTPUT,
    use_selection=False,
    apply_scale_options='FBX_SCALE_ALL',
    add_leaf_bones=False,
    bake_anim=False,
)
print(f"[prep] Exported: {OUTPUT}")
```

- [ ] **Step 2: Run the script**

```bash
blender --background --python tools/prepare_for_mixamo.py -- /Youtube/Klukai_V1.blend /tmp/klukai_for_mixamo.fbx
```

Expected: FBX file with one mesh + one armature.

- [ ] **Step 3: Commit**

```bash
git add tools/prepare_for_mixamo.py
git commit -m "feat: add Mixamo preparation script for Klukai model"
```

---

### Task 2: Upload to Mixamo, Auto-Rig, Download Idle Animation

This is a manual step — Mixamo is browser-based.

- [ ] **Step 1: Upload to Mixamo**

1. Go to https://www.mixamo.com (free Adobe account required)
2. Click "Upload Character"
3. Upload `/tmp/klukai_for_mixamo.fbx`
4. Wait for auto-rigging (~2 min)
5. Place the skeleton markers (chin, wrists, elbows, knees, groin) if auto-detect fails

- [ ] **Step 2: Download idle animation**

1. In the Animations tab, search "Idle"
2. Pick "Happy Idle" or "Standing Idle" — preview to see hands-on-hips or relaxed
3. Download settings:
   - Format: **FBX Binary (.fbx)**
   - Skin: **With Skin**
   - Frames per second: **30**
   - Keyframe Reduction: **none**
4. Save as `/tmp/klukai_idle_mixamo.fbx`

- [ ] **Step 3: Verify the FBX**

```bash
blender --background --python-expr "
import bpy
bpy.ops.import_scene.fbx(filepath='/tmp/klukai_idle_mixamo.fbx')
for obj in bpy.data.objects:
    if obj.type == 'ARMATURE':
        print(f'Armature: {obj.name}, bones: {len(obj.data.bones)}')
        if obj.animation_data and obj.animation_data.action:
            a = obj.animation_data.action
            print(f'Action: {a.name}')
print('Animations:', len(bpy.data.actions))
" 2>&1 | grep -E "Armature|Action|Animation"
```

Expected: One armature with ~65 Mixamo bones, one action.

---

### Task 3: Import Mixamo FBX into Blender and Export as .glb

**Files:**
- Create: `tools/export_mixamo_glb.py`
- Output: `web-build/assets/models/klukai.glb`

- [ ] **Step 1: Write the Blender import + glTF export script**

```python
"""
Import Mixamo FBX and export as .glb for Three.js.
Usage: blender --background --python tools/export_mixamo_glb.py -- /tmp/klukai_idle_mixamo.fbx web-build/assets/models/klukai.glb
"""
import bpy
import sys
import os

argv = sys.argv
if "--" in argv:
    argv = argv[argv.index("--") + 1:]
INPUT_FBX = argv[0]
OUTPUT_GLB = argv[1]

# Start clean
bpy.ops.wm.read_factory_settings(use_empty=True)

# Import FBX
bpy.ops.import_scene.fbx(filepath=INPUT_FBX)
print(f"[export] Imported {INPUT_FBX}")

# List what we have
for obj in bpy.data.objects:
    print(f"[export] Object: {obj.name} type={obj.type}")
    if obj.type == 'ARMATURE' and obj.animation_data:
        action = obj.animation_data.action
        if action:
            print(f"[export]   Action: {action.name}")

# Remove default cube/camera/light
for name in ['Cube', 'Camera', 'Light']:
    obj = bpy.data.objects.get(name)
    if obj:
        bpy.data.objects.remove(obj, do_unlink=True)

# Export as GLB
os.makedirs(os.path.dirname(OUTPUT_GLB) or '.', exist_ok=True)
bpy.ops.export_scene.gltf(
    filepath=OUTPUT_GLB,
    export_format='GLB',
    export_animations=True,
    export_animation_mode='ACTIONS',
    export_anim_slide_to_zero=True,
    export_texcoords=True,
    export_normals=True,
    export_materials='EXPORT',
    export_skins=True,
    export_morph=True,
    export_lights=False,
    export_cameras=False,
)

size = os.path.getsize(OUTPUT_GLB)
print(f"[export] Done! {OUTPUT_GLB} ({size / 1024 / 1024:.1f} MB)")
```

- [ ] **Step 2: Run the export**

```bash
blender --background --python tools/export_mixamo_glb.py -- /tmp/klukai_idle_mixamo.fbx /home/jalsarraf/git/companion/web-build/assets/models/klukai.glb
```

- [ ] **Step 3: Verify the .glb has the animation**

```bash
python3 -c "
import json, struct
with open('web-build/assets/models/klukai.glb', 'rb') as f:
    m,v,l = struct.unpack('<III', f.read(12))
    cl,ct = struct.unpack('<II', f.read(8))
    j = json.loads(f.read(cl))
    print(f'Skins: {len(j.get(\"skins\",[]))}')
    for s in j.get('skins',[]): print(f'  {s.get(\"name\",\"?\")} — {len(s.get(\"joints\",[]))} joints')
    print(f'Animations: {len(j.get(\"animations\",[]))}')
    for a in j.get('animations',[]): print(f'  {a.get(\"name\",\"?\")} — {len(a.get(\"channels\",[]))} channels')
"
```

Expected: 1 skin (~65 joints), 1 animation with channels.

- [ ] **Step 4: Verify in gltf-viewer**

Open https://gltf-viewer.donmccurdy.com/ and drag the .glb file in. The model should appear posed (NOT in T-pose) with the idle animation playing. **If it doesn't look right here, don't proceed — fix the export first.**

- [ ] **Step 5: Commit**

```bash
git add tools/export_mixamo_glb.py
git commit -m "feat: add Mixamo FBX to GLB export script"
```

---

### Task 4: Verify in Three.js Debug Page

**Files:**
- Modify: `web-build/debug_3d.html`

- [ ] **Step 1: Write minimal debug page**

```html
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Klukai Test</title>
<style>body{margin:0;background:#12151E}</style></head>
<body>
<script type="importmap">
{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.168.0/build/three.module.min.js","three/addons/":"https://cdn.jsdelivr.net/npm/three@0.168.0/examples/jsm/"}}
</script>
<script type="module">
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x12151E);
const camera = new THREE.PerspectiveCamera(30, innerWidth/innerHeight, 0.01, 100);
const renderer = new THREE.WebGLRenderer({antialias:true});
renderer.setSize(innerWidth, innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
document.body.appendChild(renderer.domElement);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
scene.add(new THREE.AmbientLight(0xffffff, 0.8));
const dl = new THREE.DirectionalLight(0xffffff, 1);
dl.position.set(2,3,2); scene.add(dl);

const gltf = await new Promise((resolve, reject) => {
  new GLTFLoader().load('/app/assets/models/klukai.glb?v=6', resolve, undefined, reject);
});
const model = gltf.scene;
scene.add(model);

const box = new THREE.Box3().setFromObject(model);
const sz = box.getSize(new THREE.Vector3());
const ct = box.getCenter(new THREE.Vector3());
model.position.set(-ct.x, -box.min.y, -ct.z);
camera.position.set(0, sz.y*0.5, sz.y*1.5);
controls.target.set(0, sz.y*0.4, 0);
controls.update();

let mixer = null;
if (gltf.animations.length > 0) {
  mixer = new THREE.AnimationMixer(model);
  const action = mixer.clipAction(gltf.animations[0]);
  action.play();
  console.log('Playing:', gltf.animations[0].name);
}

const clock = new THREE.Clock();
function animate() {
  requestAnimationFrame(animate);
  if (mixer) mixer.update(clock.getDelta());
  controls.update();
  renderer.render(scene, camera);
}
animate();
</script>
</body>
</html>
```

- [ ] **Step 2: Deploy and test**

```bash
rsync -avz web-build/debug_3d.html wsl2:~/companion/web-build/
ssh wsl2 "cd ~/companion && docker compose build companion-core && docker compose up -d companion-core"
```

Open http://192.168.50.5:8300/app/debug_3d.html — model should be animated, NOT in T-pose.

- [ ] **Step 3: Commit**

```bash
git add -f web-build/debug_3d.html
git commit -m "feat: minimal debug viewer for Mixamo-animated model"
```

---

### Task 5: Update klukai_3d.js Bridge

**Files:**
- Rewrite: `web-build/js/klukai_3d.js`

Only after Task 4 confirms the model+animation works in the debug page.

- [ ] **Step 1: Write clean bridge**

The bridge should:
- Load model, play idle animation via AnimationMixer
- Handle morph targets (blink, mouth) — if the Mixamo model preserves them
- Expose mood/talking/dispose API for Flutter integration
- No manual bone rotation code at all

(Code is already written in current klukai_3d.js — just verify AnimationMixer path works with the new Mixamo .glb)

- [ ] **Step 2: Cache bust and deploy**

Update `chat_screen.dart` model URL to `?v=6`, rebuild Flutter, deploy.

- [ ] **Step 3: Full deploy + test**

```bash
export PATH="$PATH:$HOME/flutter/bin"
cd ~/git/companion/flutter_app && flutter build web --release --base-href /app/
cp -r build/web/* ../web-build/
rsync -avz --exclude .git --exclude flutter_app ~/git/companion/ wsl2:~/companion/
ssh wsl2 "cd ~/companion && docker compose build companion-core && docker compose up -d companion-core"
```

- [ ] **Step 4: Commit**

```bash
git add -f web-build/js/klukai_3d.js flutter_app/lib/screens/chat_screen.dart
git commit -m "feat: Mixamo-animated Klukai with clean Three.js bridge"
```

---

## Fallback: If Mixamo Can't Rig the Model

Mixamo auto-rigging occasionally fails on complex game models with unusual proportions. If it fails:

**Alternative A: Use a VRoid Klukai model instead**
- Found on VRoid Hub: https://hub.vroid.com/en/characters/6683933236386489109/models/1478219667485986360
- VRM format works with @pixiv/three-vrm library
- Mixamo animations can be retargeted to VRM using the V-Sekai reference code

**Alternative B: Use a pre-animated GLB from Sketchfab**
- The Speed Star skin model on Sketchfab may have animations
- Or download any free animated anime character GLB and swap textures

**Alternative C: Manual Blender animation**
- If Mixamo fails but we have the clean .fbx, manually pose the character in Blender
- Use the Blender GUI (not headless) to set keyframes visually
- Export from GUI to verify before scripting
