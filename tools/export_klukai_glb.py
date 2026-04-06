"""
Blender headless script: Open Klukai .blend, create procedural animations, export .glb

Usage:
    blender --background --python tools/export_klukai_glb.py -- <input.blend> <output.glb> [skin_name]

Skin names (from Open3DLab pack): default, dorm, speed_star, astral_luminous, cerulean_breaker
If no skin_name given, exports default skin.

This script:
1. Opens the .blend file
2. Inspects the armature and mesh structure
3. Creates procedural animation clips (idle, blink, talking, reactions, fidgets)
4. Exports a .glb with all animations embedded
"""

import bpy
import sys
import math
import os
from mathutils import Euler, Quaternion, Vector


# ── Parse CLI args (after "--") ─────────────────────────────────────────────

argv = sys.argv
if "--" in argv:
    argv = argv[argv.index("--") + 1:]
else:
    argv = []

if len(argv) < 2:
    print("Usage: blender --background --python export_klukai_glb.py -- <input.blend> <output.glb> [skin]")
    sys.exit(1)

INPUT_BLEND = argv[0]
OUTPUT_GLB = argv[1]
TARGET_SKIN = argv[2] if len(argv) > 2 else "default"

print(f"[export] Input: {INPUT_BLEND}")
print(f"[export] Output: {OUTPUT_GLB}")
print(f"[export] Skin: {TARGET_SKIN}")


# ── Step 1: Open the .blend ─────────────────────────────────────────────────

bpy.ops.wm.open_mainfile(filepath=INPUT_BLEND)
print(f"[export] Opened blend file. Objects count: {len(bpy.data.objects)}")

# ── Clean up scene: hide non-default skins, remove extra armatures ──────────
# Make everything visible first so we can manipulate it
for col in bpy.data.collections:
    col.hide_viewport = False
    col.hide_render = False
for obj in bpy.data.objects:
    obj.hide_viewport = False
    obj.hide_render = False
    obj.hide_set(False)
def enable_collection_recursive(lc):
    lc.exclude = False
    lc.hide_viewport = False
    for child in lc.children:
        enable_collection_recursive(child)
enable_collection_recursive(bpy.context.view_layer.layer_collection)

# Skin variant keywords to HIDE (keep only default skin)
HIDE_KEYWORDS = ['Astral Luminous', 'Cerulean Breaker', 'Speed Star', 'Dorm',
                 'Cerulean_Breaker', 'Speed_Star', 'Astral_Luminous']

removed_count = 0
for obj in list(bpy.data.objects):
    name = obj.name
    # DELETE non-default skin variants (not just hide)
    if any(kw in name for kw in HIDE_KEYWORDS):
        bpy.data.objects.remove(obj, do_unlink=True)
        removed_count += 1
        continue
    # Remove junk
    if name in ('Cube', 'Camera'):
        bpy.data.objects.remove(obj, do_unlink=True)
        continue

# Remove extra armatures — keep only the main one (Klukai or RIG-Klukai)
KEEP_ARMATURES = {'Klukai', 'RIG-Klukai'}
for obj in list(bpy.data.objects):
    if obj.type == 'ARMATURE' and obj.name not in KEEP_ARMATURES:
        has_children = any(c.type == 'MESH' and not c.hide_render for c in obj.children_recursive)
        if not has_children:
            print(f"[export] Removing extra armature: {obj.name}")
            bpy.data.objects.remove(obj, do_unlink=True)

# CRITICAL FIX: Uncheck "Deform" flag on all non-DEF bones
# Rigify marks ORG/MCH/VIS/tweak bones with use_deform=True by default.
# With 673 bones competing for the 4-weights-per-vertex limit in glTF,
# the actual DEF bone weights get diluted to near-zero.
# Only DEF- prefixed bones should deform the mesh.
deform_disabled = 0
for obj in bpy.data.objects:
    if obj.type == 'ARMATURE':
        for bone in obj.data.bones:
            if bone.use_deform and not bone.name.startswith('DEF-'):
                bone.use_deform = False
                deform_disabled += 1
print(f"[export] Disabled 'Deform' on {deform_disabled} non-DEF bones")

print(f"[export] Removed {removed_count} non-default skin objects")
print(f"[export] Remaining objects: {len(bpy.data.objects)}")


# ── Skeleton fix: re-parent meshes from 673-bone RIG-Klukai to 152-bone Klukai ──
# The Body mesh is bound to Skin 0 "RIG-Klukai" (673 bones — full Rigify control
# rig with DEF/MCH/ORG/VIS bones).  With 4 weights-per-vertex the DEF bone weights
# get diluted to near-zero and Three.js cannot deform the mesh.
# Fix: rename vertex groups (strip DEF- prefix), re-parent to the clean "Klukai"
# armature, then delete the Rigify control rigs entirely.

# Step 1: Rename vertex groups — strip DEF- prefix so they match Klukai bone names
renamed_count = 0
for obj in bpy.data.objects:
    if obj.type == 'MESH':
        for vg in obj.vertex_groups:
            if vg.name.startswith('DEF-'):
                new_name = vg.name[4:]  # Strip "DEF-"
                # If a group with the target name already exists, rename it out of the way
                existing = obj.vertex_groups.get(new_name)
                if existing and existing != vg:
                    existing.name = existing.name + '_OLD'
                vg.name = new_name
                renamed_count += 1
print(f"[export] Renamed {renamed_count} vertex groups (stripped DEF- prefix)")

# Step 2: Find the clean 152-bone game armature
klukai_arm = bpy.data.objects.get('Klukai')
if not klukai_arm:
    print("[export] ERROR: 'Klukai' armature not found — skeleton re-parent skipped!")
else:
    print(f"[export] Found Klukai armature: {len(klukai_arm.data.bones)} bones")

    # Step 3: Re-parent ALL meshes with vertex groups to Klukai armature
    # and ensure they have a working armature modifier pointing to Klukai
    rig_arm = bpy.data.objects.get('RIG-Klukai')
    reparented = 0
    modifier_fixed = 0
    for obj in list(bpy.data.objects):
        if obj.type != 'MESH':
            continue
        # Check if this mesh has vertex groups (meaning it should be skinned)
        if len(obj.vertex_groups) == 0:
            continue

        # Re-parent to Klukai if currently under RIG-Klukai or any other armature
        if obj.parent != klukai_arm:
            obj.parent = klukai_arm
            obj.parent_type = 'OBJECT'
            reparented += 1

        # Fix armature modifier: update or create one pointing to Klukai
        has_armature_mod = False
        for mod in obj.modifiers:
            if mod.type == 'ARMATURE':
                if mod.object != klukai_arm:
                    mod.object = klukai_arm
                    modifier_fixed += 1
                has_armature_mod = True
                break

        # If no armature modifier exists, add one
        if not has_armature_mod and len(obj.vertex_groups) > 0:
            mod = obj.modifiers.new(name='Armature', type='ARMATURE')
            mod.object = klukai_arm
            modifier_fixed += 1

    print(f"[export] Re-parented {reparented} meshes to Klukai armature")
    print(f"[export] Fixed/added {modifier_fixed} armature modifiers")

    # Steps 4-6: Delete the Rigify control rigs — they must not appear in the export
    for arm_name in ['RIG-Klukai', 'RIG-Skins', 'Skins']:
        arm = bpy.data.objects.get(arm_name)
        if arm:
            bpy.data.objects.remove(arm, do_unlink=True)
            print(f"[export] Deleted armature: {arm_name}")

print(f"[export] Post-skeleton-fix objects: {len(bpy.data.objects)}")


# ── Pose arms down and bake as rest pose ────────────────────────────────────
# This makes the .glb load with arms already at the character's sides.
# No JS bone rotation needed for rest pose — just small animation deltas.
klukai_arm2 = bpy.data.objects.get('Klukai')
if klukai_arm2:
    bpy.context.view_layer.objects.active = klukai_arm2
    bpy.ops.object.mode_set(mode='POSE')

    for bone_name, angle in [('Shoulder_L', -60), ('Shoulder_R', 60),
                              ('Elbow_L', -15), ('Elbow_R', 15)]:
        pb = klukai_arm2.pose.bones.get(bone_name)
        if pb:
            pb.rotation_mode = 'QUATERNION'
            pb.rotation_quaternion = __import__('mathutils').Quaternion((1, 0, 0), __import__('math').radians(angle))

    bpy.context.view_layer.update()
    bpy.ops.pose.armature_apply(selected=False)
    bpy.ops.object.mode_set(mode='OBJECT')
    print("[export] Applied arms-down pose as rest pose")
else:
    print("[export] WARNING: Could not find Klukai armature for pose")


# ── Step 2: Inspect scene ───────────────────────────────────────────────────

# Find the main character armature (the one named "Klukai" with ~152 bones)
armature = None
for obj in bpy.data.objects:
    if obj.type == 'ARMATURE' and obj.name == 'Klukai':
        armature = obj
        break
# Fallback: pick the armature with the most bones (but not RIG- or Skins rigs)
if not armature:
    best = None
    best_count = 0
    for obj in bpy.data.objects:
        if obj.type == 'ARMATURE' and not obj.name.startswith('RIG-') and obj.name != 'Skins':
            count = len(obj.data.bones)
            if count > best_count:
                best = obj
                best_count = count
    armature = best

if not armature:
    print("[export] ERROR: No armature found in scene!")
    # Try to export anyway without animations
    armature = None

if armature:
    print(f"[export] Armature: {armature.name}")
    bone_names = [b.name for b in armature.data.bones]
    print(f"[export] Bones ({len(bone_names)}): {bone_names[:20]}...")

# List all meshes
meshes = [o for o in bpy.data.objects if o.type == 'MESH']
print(f"[export] Meshes: {[m.name for m in meshes]}")

# Check for shape keys (blend shapes / morph targets)
for mesh in meshes:
    if mesh.data.shape_keys:
        keys = [k.name for k in mesh.data.shape_keys.key_blocks]
        print(f"[export] Shape keys on {mesh.name}: {keys}")


# ── Step 3: Find common bone names ──────────────────────────────────────────

def find_bone(patterns):
    """Find a bone name matching any of the given patterns (case-insensitive)."""
    if not armature:
        return None
    for bone in armature.data.bones:
        name_lower = bone.name.lower()
        for p in patterns:
            if p.lower() in name_lower:
                return bone.name
    return None

# GFL2 Klukai rig uses: Head_M, Spine1_M, Spine2_M, Chest_M, Root_M, etc.
HEAD_BONE = find_bone(['Head_M', 'head'])
SPINE_BONE = find_bone(['Chest_M', 'Spine2_M', 'chest', 'spine'])
HIPS_BONE = find_bone(['Root_M', 'Hips', 'hips'])
L_EYE_BONE = find_bone(['Face_Eye_L', 'eye.l'])
R_EYE_BONE = find_bone(['Face_Eye_R', 'eye.r'])
L_HAND_BONE = find_bone(['Wrist_L', 'Hand_L', 'hand.l'])
R_HAND_BONE = find_bone(['Wrist_R', 'Hand_R', 'hand.r'])
L_ARM_BONE = find_bone(['Shoulder_L', 'upperarm.l'])
R_ARM_BONE = find_bone(['Shoulder_R', 'upperarm.r'])

print(f"[export] Key bones - Head: {HEAD_BONE}, Spine: {SPINE_BONE}, Hips: {HIPS_BONE}")
print(f"[export] Hands - L: {L_HAND_BONE}, R: {R_HAND_BONE}")


# ── Step 4: Animation helper functions ──────────────────────────────────────

def ensure_action(name, frame_end=60):
    """Create a new action and assign it to the armature (Blender 5.0 API)."""
    action = bpy.data.actions.new(name=name)
    action.use_fake_user = True
    if armature:
        armature.animation_data_create()
        armature.animation_data.action = action
    return action


def keyframe_bone_rotation(action, bone_name, frame, rotation_euler, data_path_prefix=None):
    """Insert rotation keyframes for a bone (Blender 5.0 API)."""
    if not bone_name or not armature:
        return
    prefix = data_path_prefix or f'pose.bones["{bone_name}"].rotation_euler'
    for i, val in enumerate(rotation_euler):
        fc = action.fcurve_ensure_for_datablock(armature, prefix, index=i)
        kf = fc.keyframe_points.insert(frame, val)
        kf.interpolation = 'BEZIER'


def keyframe_bone_location(action, bone_name, frame, location):
    """Insert location keyframes for a bone (Blender 5.0 API)."""
    if not bone_name or not armature:
        return
    prefix = f'pose.bones["{bone_name}"].location'
    for i, val in enumerate(location):
        fc = action.fcurve_ensure_for_datablock(armature, prefix, index=i)
        kf = fc.keyframe_points.insert(frame, val)
        kf.interpolation = 'BEZIER'


def create_breathing_cycle(action, name_suffix="", amplitude=0.003, period=90):
    """Add subtle breathing motion on the spine/chest bone."""
    bone = SPINE_BONE or HIPS_BONE
    if not bone:
        return
    for frame in range(0, period + 1, 5):
        t = frame / period * 2 * math.pi
        y_offset = math.sin(t) * amplitude
        keyframe_bone_location(action, bone, frame, (0, 0, y_offset))


def create_sway_cycle(action, amplitude=0.01, period=120):
    """Add subtle body sway on hips."""
    bone = HIPS_BONE
    if not bone:
        return
    for frame in range(0, period + 1, 10):
        t = frame / period * 2 * math.pi
        x_rot = math.sin(t) * amplitude
        keyframe_bone_rotation(action, bone, frame, (x_rot, 0, 0))


# ── Step 5: Create all animation clips ──────────────────────────────────────

print("[export] Creating procedural animations...")

FPS = 30
bpy.context.scene.render.fps = FPS

# Ensure all collections/objects are visible and in view layer
for col in bpy.data.collections:
    col.hide_viewport = False
    col.hide_render = False
for obj in bpy.data.objects:
    obj.hide_viewport = False
    obj.hide_render = False
    obj.hide_set(False)
# Ensure all collections are linked to the view layer
def enable_collection_recursive(layer_collection):
    layer_collection.exclude = False
    layer_collection.hide_viewport = False
    for child in layer_collection.children:
        enable_collection_recursive(child)
enable_collection_recursive(bpy.context.view_layer.layer_collection)

# Set armature to pose mode if it exists
if armature:
    bpy.context.view_layer.objects.active = armature
    bpy.ops.object.mode_set(mode='POSE')

    # ─── Idle animations (one per mood group) ───

    idle_params = {
        'idle_relaxed':    {'sway': 0.008, 'breathe': 0.003, 'period': 120},
        'idle_happy':      {'sway': 0.015, 'breathe': 0.004, 'period': 90},
        'idle_serious':    {'sway': 0.004, 'breathe': 0.002, 'period': 150},
        'idle_shy':        {'sway': 0.012, 'breathe': 0.003, 'period': 100},
        'idle_combat':     {'sway': 0.006, 'breathe': 0.003, 'period': 80},
        'idle_tender':     {'sway': 0.010, 'breathe': 0.004, 'period': 110},
        'idle_drowsy':     {'sway': 0.020, 'breathe': 0.005, 'period': 180},
        'idle_melancholy': {'sway': 0.007, 'breathe': 0.003, 'period': 140},
    }

    for name, params in idle_params.items():
        print(f"[export]   Creating {name}...")
        action = ensure_action(name, frame_end=params['period'])
        create_breathing_cycle(action, amplitude=params['breathe'], period=params['period'])
        create_sway_cycle(action, amplitude=params['sway'], period=params['period'])

        # Add slight head movement for personality
        if HEAD_BONE:
            period = params['period']
            for frame in range(0, period + 1, 15):
                t = frame / period * 2 * math.pi
                head_y = math.sin(t * 0.7) * params['sway'] * 0.5
                head_x = math.cos(t * 0.5) * params['sway'] * 0.3
                keyframe_bone_rotation(action, HEAD_BONE, frame, (head_x, head_y, 0))

    # ─── Blink animation (short, loops independently) ───

    print("[export]   Creating blink...")
    action = ensure_action('blink', frame_end=15)
    # Blink is done via shape keys if available, otherwise head bone dip
    blink_mesh = None
    blink_key_idx = None
    for mesh in meshes:
        if mesh.data.shape_keys:
            for i, kb in enumerate(mesh.data.shape_keys.key_blocks):
                name_l = kb.name.lower()
                if 'eyes_close' in name_l or 'blink' in name_l or 'eye_close' in name_l or 'まばたき' in name_l:
                    blink_mesh = mesh
                    blink_key_idx = i
                    break
        if blink_mesh:
            break

    if blink_mesh and blink_key_idx is not None:
        kb = blink_mesh.data.shape_keys.key_blocks[blink_key_idx]
        dp = f'key_blocks["{kb.name}"].value'
        kb.value = 0.0
        kb.keyframe_insert(data_path="value", frame=0)
        kb.value = 1.0
        kb.keyframe_insert(data_path="value", frame=4)
        kb.value = 0.0
        kb.keyframe_insert(data_path="value", frame=8)
        print(f"[export]     Using shape key: {kb.name}")
    elif HEAD_BONE:
        # Fallback: slight head dip for "blink"
        keyframe_bone_rotation(action, HEAD_BONE, 0, (0, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 3, (0.03, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 6, (0, 0, 0))

    # ─── Talking animation (mouth movement loop) ───

    print("[export]   Creating talking...")
    action = ensure_action('talking', frame_end=20)
    # Try to find mouth shape keys
    mouth_mesh = None
    mouth_key_name = None
    for mesh in meshes:
        if mesh.data.shape_keys:
            for kb in mesh.data.shape_keys.key_blocks:
                name_l = kb.name.lower()
                if any(k in name_l for k in ['mouth_happy', 'mouth_open', 'mouth_smile', 'あ', 'mouth_a', 'jaw_open']):
                    mouth_mesh = mesh
                    mouth_key_name = kb.name
                    break
        if mouth_mesh:
            break

    if mouth_mesh and mouth_key_name:
        kb = mouth_mesh.data.shape_keys.key_blocks[mouth_key_name]
        dp = f'key_blocks["{mouth_key_name}"].value'
        # Open-close-open cycle
        for frame, val in [(0, 0.0), (5, 0.6), (10, 0.1), (15, 0.8), (20, 0.0)]:
            kb.value = val
            kb.keyframe_insert(data_path="value", frame=frame)
        print(f"[export]     Using shape key: {mouth_key_name}")
    elif HEAD_BONE:
        # Fallback: subtle head bob for "talking"
        for frame, rot in [(0, 0), (5, 0.02), (10, -0.01), (15, 0.015), (20, 0)]:
            keyframe_bone_rotation(action, HEAD_BONE, frame, (rot, 0, 0))

    # ─── Reaction animations ───

    print("[export]   Creating reactions...")

    # reaction_tap: quick head tilt + return
    action = ensure_action('reaction_tap', frame_end=30)
    if HEAD_BONE:
        keyframe_bone_rotation(action, HEAD_BONE, 0, (0, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 8, (0.05, 0.1, 0.03))
        keyframe_bone_rotation(action, HEAD_BONE, 20, (-0.02, 0.05, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 30, (0, 0, 0))

    # reaction_surprise: quick lean back
    action = ensure_action('reaction_surprise', frame_end=30)
    if SPINE_BONE:
        keyframe_bone_rotation(action, SPINE_BONE, 0, (0, 0, 0))
        keyframe_bone_rotation(action, SPINE_BONE, 6, (-0.06, 0, 0))
        keyframe_bone_rotation(action, SPINE_BONE, 18, (-0.02, 0, 0))
        keyframe_bone_rotation(action, SPINE_BONE, 30, (0, 0, 0))

    # reaction_milestone: happy bounce
    action = ensure_action('reaction_milestone', frame_end=40)
    if HIPS_BONE:
        keyframe_bone_location(action, HIPS_BONE, 0, (0, 0, 0))
        keyframe_bone_location(action, HIPS_BONE, 8, (0, 0, 0.02))
        keyframe_bone_location(action, HIPS_BONE, 16, (0, 0, 0))
        keyframe_bone_location(action, HIPS_BONE, 22, (0, 0, 0.01))
        keyframe_bone_location(action, HIPS_BONE, 30, (0, 0, 0))

    # ─── Fidget animations ───

    print("[export]   Creating fidgets...")

    # fidget_look_around
    action = ensure_action('fidget_look_around', frame_end=60)
    if HEAD_BONE:
        keyframe_bone_rotation(action, HEAD_BONE, 0, (0, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 15, (0, 0.12, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 35, (0.03, -0.08, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 50, (0, 0.02, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 60, (0, 0, 0))

    # fidget_weight_shift
    action = ensure_action('fidget_weight_shift', frame_end=50)
    if HIPS_BONE:
        keyframe_bone_location(action, HIPS_BONE, 0, (0, 0, 0))
        keyframe_bone_location(action, HIPS_BONE, 15, (0.01, 0, 0))
        keyframe_bone_location(action, HIPS_BONE, 35, (-0.01, 0, 0))
        keyframe_bone_location(action, HIPS_BONE, 50, (0, 0, 0))

    # fidget_blink_hard (double blink)
    action = ensure_action('fidget_blink_hard', frame_end=20)
    if HEAD_BONE:
        keyframe_bone_rotation(action, HEAD_BONE, 0, (0, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 3, (0.04, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 6, (0, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 10, (0.04, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 13, (0, 0, 0))

    # fidget_hair (touch right side of head with right hand)
    action = ensure_action('fidget_hair', frame_end=50)
    if R_HAND_BONE:
        keyframe_bone_rotation(action, R_HAND_BONE, 0, (0, 0, 0))
        keyframe_bone_rotation(action, R_HAND_BONE, 15, (0.3, 0.2, 0))
        keyframe_bone_rotation(action, R_HAND_BONE, 35, (0.2, 0.15, 0.1))
        keyframe_bone_rotation(action, R_HAND_BONE, 50, (0, 0, 0))
    if HEAD_BONE:
        keyframe_bone_rotation(action, HEAD_BONE, 0, (0, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 15, (0, -0.05, -0.03))
        keyframe_bone_rotation(action, HEAD_BONE, 50, (0, 0, 0))

    # fidget_stretch
    action = ensure_action('fidget_stretch', frame_end=60)
    if SPINE_BONE:
        keyframe_bone_rotation(action, SPINE_BONE, 0, (0, 0, 0))
        keyframe_bone_rotation(action, SPINE_BONE, 20, (-0.05, 0, 0))
        keyframe_bone_rotation(action, SPINE_BONE, 40, (-0.02, 0, 0))
        keyframe_bone_rotation(action, SPINE_BONE, 60, (0, 0, 0))

    # fidget_smile (head tilt + slight nod)
    action = ensure_action('fidget_smile', frame_end=40)
    if HEAD_BONE:
        keyframe_bone_rotation(action, HEAD_BONE, 0, (0, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 10, (0.03, 0.04, 0.02))
        keyframe_bone_rotation(action, HEAD_BONE, 30, (0.02, 0.02, 0.01))
        keyframe_bone_rotation(action, HEAD_BONE, 40, (0, 0, 0))

    # fidget_weapon (glance down at weapon hand)
    action = ensure_action('fidget_weapon', frame_end=45)
    if HEAD_BONE:
        keyframe_bone_rotation(action, HEAD_BONE, 0, (0, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 12, (0.08, -0.06, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 30, (0.04, -0.03, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 45, (0, 0, 0))

    # fidget_scan (look left-right deliberately)
    action = ensure_action('fidget_scan', frame_end=60)
    if HEAD_BONE:
        keyframe_bone_rotation(action, HEAD_BONE, 0, (0, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 12, (0, -0.15, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 24, (0, -0.15, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 36, (0, 0.12, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 48, (0, 0.12, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 60, (0, 0, 0))

    # fidget_tuck_hair (shy: touch left side of face)
    action = ensure_action('fidget_tuck_hair', frame_end=50)
    if L_HAND_BONE:
        keyframe_bone_rotation(action, L_HAND_BONE, 0, (0, 0, 0))
        keyframe_bone_rotation(action, L_HAND_BONE, 15, (0.25, -0.15, 0))
        keyframe_bone_rotation(action, L_HAND_BONE, 35, (0.15, -0.1, 0.05))
        keyframe_bone_rotation(action, L_HAND_BONE, 50, (0, 0, 0))
    if HEAD_BONE:
        keyframe_bone_rotation(action, HEAD_BONE, 0, (0, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 15, (0.03, 0.06, 0.02))
        keyframe_bone_rotation(action, HEAD_BONE, 50, (0, 0, 0))

    # fidget_look_away (melancholy/shy)
    action = ensure_action('fidget_look_away', frame_end=50)
    if HEAD_BONE:
        keyframe_bone_rotation(action, HEAD_BONE, 0, (0, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 15, (0.04, 0.15, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 40, (0.02, 0.1, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 50, (0, 0, 0))

    # fidget_yawn
    action = ensure_action('fidget_yawn', frame_end=60)
    if HEAD_BONE:
        keyframe_bone_rotation(action, HEAD_BONE, 0, (0, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 15, (-0.06, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 30, (-0.08, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 45, (-0.04, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 60, (0, 0, 0))

    # fidget_head_nod (drowsy nod-off)
    action = ensure_action('fidget_head_nod', frame_end=50)
    if HEAD_BONE:
        keyframe_bone_rotation(action, HEAD_BONE, 0, (0, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 20, (0.1, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 25, (0.12, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 30, (0.02, 0, 0))
        keyframe_bone_rotation(action, HEAD_BONE, 50, (0, 0, 0))

    # fidget_rub_eyes
    action = ensure_action('fidget_rub_eyes', frame_end=50)
    if R_HAND_BONE:
        keyframe_bone_rotation(action, R_HAND_BONE, 0, (0, 0, 0))
        keyframe_bone_rotation(action, R_HAND_BONE, 12, (0.5, 0.3, 0))
        keyframe_bone_rotation(action, R_HAND_BONE, 25, (0.45, 0.25, 0.1))
        keyframe_bone_rotation(action, R_HAND_BONE, 38, (0.5, 0.3, -0.1))
        keyframe_bone_rotation(action, R_HAND_BONE, 50, (0, 0, 0))

    # Back to object mode
    bpy.ops.object.mode_set(mode='OBJECT')

print(f"[export] Created {len(bpy.data.actions)} animation actions")
for a in bpy.data.actions:
    try:
        fc_count = len(a.fcurves) if hasattr(a, 'fcurves') else '?'
    except Exception:
        fc_count = '?'
    print(f"[export]   - {a.name} ({fc_count} fcurves)")


# ── Step 6: Select objects to export ────────────────────────────────────────

# Deselect all first
bpy.ops.object.select_all(action='DESELECT')

# Select armature and all meshes
if armature:
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature

for mesh in meshes:
    mesh.select_set(True)

print(f"[export] Selected {len(bpy.context.selected_objects)} objects for export")


# ── Step 7: Export as .glb ──────────────────────────────────────────────────

print(f"[export] Exporting to {OUTPUT_GLB}...")

os.makedirs(os.path.dirname(OUTPUT_GLB) or '.', exist_ok=True)

# Blender 5.0 uses the new glTF exporter API
try:
    bpy.ops.export_scene.gltf(
        filepath=OUTPUT_GLB,
        export_format='GLB',
        use_selection=False,       # Export all visible objects
        export_animations=True,
        export_animation_mode='ACTIONS',  # Export all actions as separate clips
        export_anim_slide_to_zero=True,
        export_apply=False,        # Don't apply modifiers (keep armature)
        export_texcoords=True,
        export_normals=True,
        export_colors=True,
        export_image_format='AUTO',
        export_materials='EXPORT',
        export_skins=True,
        export_def_bones=True,     # ONLY export deformation bones — strips MCH/ORG/VIS
        export_morph=True,         # Export shape keys / morph targets
        export_morph_normal=True,
        export_lights=False,
        export_cameras=False,
    )
except TypeError as e:
    # Fallback for different Blender versions with different API params
    print(f"[export] Retrying with simplified params: {e}")
    bpy.ops.export_scene.gltf(
        filepath=OUTPUT_GLB,
        export_format='GLB',
        export_animations=True,
        export_texcoords=True,
        export_normals=True,
        export_materials='EXPORT',
        export_skins=True,
        export_morph=True,
    )

file_size = os.path.getsize(OUTPUT_GLB)
print(f"[export] Done! Output: {OUTPUT_GLB} ({file_size / 1024 / 1024:.1f} MB)")
print(f"[export] Animation clips: {[a.name for a in bpy.data.actions]}")
