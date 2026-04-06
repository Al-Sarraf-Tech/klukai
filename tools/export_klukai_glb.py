"""
Blender headless export: Klukai .blend → .glb with hands-on-hips idle animation

Usage:
    blender --background --python tools/export_klukai_glb.py -- <input.blend> <output.glb>
"""

import bpy
import sys
import os
import math
from mathutils import Quaternion

# ── Parse args ──────────────────────────────────────────────────────────────
argv = sys.argv
if "--" in argv:
    argv = argv[argv.index("--") + 1:]
else:
    argv = []

INPUT_BLEND = argv[0] if len(argv) > 0 else ""
OUTPUT_GLB = argv[1] if len(argv) > 1 else ""

if not INPUT_BLEND or not OUTPUT_GLB:
    print("Usage: blender --background --python export_klukai_glb.py -- <input.blend> <output.glb>")
    sys.exit(1)

print(f"[export] Input: {INPUT_BLEND}")
print(f"[export] Output: {OUTPUT_GLB}")

# ── Open file ───────────────────────────────────────────────────────────────
bpy.ops.wm.open_mainfile(filepath=INPUT_BLEND)

# ── Make everything visible ─────────────────────────────────────────────────
for col in bpy.data.collections:
    col.hide_viewport = False
    col.hide_render = False
for obj in bpy.data.objects:
    obj.hide_viewport = False
    obj.hide_render = False
    try:
        obj.hide_set(False)
    except:
        pass

def enable_lc(lc):
    lc.exclude = False
    lc.hide_viewport = False
    for c in lc.children:
        enable_lc(c)

enable_lc(bpy.context.view_layer.layer_collection)

# ── Remove non-default skin variants ────────────────────────────────────────
REMOVE_KEYWORDS = ['Astral Luminous', 'Cerulean Breaker', 'Speed Star', 'Dorm',
                   'Cerulean_Breaker', 'Speed_Star', 'Astral_Luminous']

removed = 0
for obj in list(bpy.data.objects):
    if any(kw in obj.name for kw in REMOVE_KEYWORDS):
        bpy.data.objects.remove(obj, do_unlink=True)
        removed += 1
    elif obj.name in ('Cube', 'Camera'):
        bpy.data.objects.remove(obj, do_unlink=True)

print(f"[export] Removed {removed} non-default skin objects")

# ── Rename vertex groups: strip DEF- prefix ─────────────────────────────────
renamed = 0
for obj in bpy.data.objects:
    if obj.type == 'MESH':
        for vg in obj.vertex_groups:
            if vg.name.startswith('DEF-'):
                new_name = vg.name[4:]
                existing = obj.vertex_groups.get(new_name)
                if existing and existing != vg:
                    existing.name = existing.name + '_OLD'
                vg.name = new_name
                renamed += 1

print(f"[export] Renamed {renamed} vertex groups")

# ── Re-parent all meshes to Klukai armature ─────────────────────────────────
klukai = bpy.data.objects.get('Klukai')
if not klukai:
    print("[export] ERROR: Klukai armature not found!")
    sys.exit(1)

print(f"[export] Klukai armature: {len(klukai.data.bones)} bones")

reparented = 0
for obj in list(bpy.data.objects):
    if obj.type != 'MESH' or len(obj.vertex_groups) == 0:
        continue
    if obj.parent != klukai:
        obj.parent = klukai
        obj.parent_type = 'OBJECT'
        reparented += 1
    # Fix armature modifier
    has_mod = False
    for mod in obj.modifiers:
        if mod.type == 'ARMATURE':
            mod.object = klukai
            has_mod = True
            break
    if not has_mod:
        mod = obj.modifiers.new(name='Armature', type='ARMATURE')
        mod.object = klukai

print(f"[export] Re-parented {reparented} meshes")

# ── Delete Rigify armatures ─────────────────────────────────────────────────
for arm_name in ['RIG-Klukai', 'RIG-Skins', 'Skins']:
    arm = bpy.data.objects.get(arm_name)
    if arm:
        bpy.data.objects.remove(arm, do_unlink=True)
        print(f"[export] Deleted {arm_name}")

# ── Disable Deform on non-DEF bones ────────────────────────────────────────
disabled = 0
for bone in klukai.data.bones:
    if bone.use_deform and not bone.name.startswith('DEF-'):
        # Keep the game bones as deform bones (they're the real deform bones)
        # Only disable bones that start with known non-deform prefixes
        pass
disabled = 0  # Don't disable anything — game bones ARE the deform bones

# ── Create hands-on-hips idle animation ─────────────────────────────────────
bpy.context.view_layer.objects.active = klukai
bpy.ops.object.mode_set(mode='POSE')

action = bpy.data.actions.new(name='idle')
action.use_fake_user = True
klukai.animation_data_create()
klukai.animation_data.action = action

FPS = 30
DURATION = 120  # 4 seconds
bpy.context.scene.render.fps = FPS
bpy.context.scene.frame_start = 0
bpy.context.scene.frame_end = DURATION


def key_quat(bone_name, frame, quat):
    """Insert quaternion keyframes using the Blender 5.0 API."""
    dp = f'pose.bones["{bone_name}"].rotation_quaternion'
    for i in range(4):
        fc = action.fcurve_ensure_for_datablock(klukai, dp, index=i)
        kf = fc.keyframe_points.insert(frame, quat[i])
        kf.interpolation = 'BEZIER'


# Hands-on-hips pose (verified: wrist 0.108 from hip)
# Shoulder: -60° on X axis
# Elbow: -80° on Y axis
shoulder_L = Quaternion((1, 0, 0), math.radians(-60))
shoulder_R = Quaternion((1, 0, 0), math.radians(60))
elbow_L = Quaternion((0, 1, 0), math.radians(-80))
elbow_R = Quaternion((0, 1, 0), math.radians(80))

# Key the pose at start and end (loop)
for frame in [0, DURATION]:
    key_quat('Shoulder_L', frame, shoulder_L)
    key_quat('Shoulder_R', frame, shoulder_R)
    key_quat('Elbow_L', frame, elbow_L)
    key_quat('Elbow_R', frame, elbow_R)

# Subtle breathing on Spine1_M
for frame in range(0, DURATION + 1, 10):
    t = frame / DURATION * 2 * math.pi
    breath = Quaternion((1, 0, 0), math.sin(t) * 0.015)
    key_quat('Spine1_M', frame, breath)

# Subtle body sway on Root_M
for frame in range(0, DURATION + 1, 15):
    t = frame / DURATION * 2 * math.pi
    sway = Quaternion((0, 0, 1), math.sin(t) * 0.01)
    key_quat('Root_M', frame, sway)

# Subtle head movement
for frame in range(0, DURATION + 1, 15):
    t = frame / DURATION * math.pi
    head = Quaternion((0, 1, 0), math.sin(t) * 0.025)
    key_quat('Head_M', frame, head)

bpy.ops.object.mode_set(mode='OBJECT')
print("[export] Created idle animation (hands on hips)")

# ── Export ───────────────────────────────────────────────────────────────────
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
    export_morph_normal=True,
    export_lights=False,
    export_cameras=False,
    export_def_bones=True,
)

file_size = os.path.getsize(OUTPUT_GLB)
print(f"[export] Done! {OUTPUT_GLB} ({file_size / 1024 / 1024:.1f} MB)")
