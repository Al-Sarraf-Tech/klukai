"""
Blender script: Clean Klukai model and export FBX for Mixamo upload.
Usage: blender --background --python tools/prepare_for_mixamo.py -- <input.blend> <output.fbx>
"""
import bpy
import sys

argv = sys.argv
if "--" in argv:
    argv = argv[argv.index("--") + 1:]
INPUT = argv[0]
OUTPUT = argv[1]

print(f"[prep] Input: {INPUT}")
print(f"[prep] Output: {OUTPUT}")

bpy.ops.wm.open_mainfile(filepath=INPUT)

# Make everything visible
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

# Delete everything except essential default-skin meshes and Klukai armature
KEEP_ARMATURE = 'Klukai'
KEEP_MESH_KEYWORDS = ['Body', 'Face', 'Hair', 'Jacket', 'Legs', 'Feet',
                      'Gloves', 'Shoes', 'Leggings', 'Hat', 'Earpieces',
                      'Chest Cover', 'Chest Belt', 'Chest Acc']
REMOVE_KEYWORDS = ['Dorm', 'Speed Star', 'Astral', 'Cerulean', 'Fight',
                   'Pistol', 'HK416', 'Axe', 'Baton', 'Magazine', 'Holster',
                   'Flashbang', 'Radio', 'Bag', 'Ring', 'Bracelet', 'Watch',
                   'Cube', 'Camera', 'Silencer', 'Grenade', 'Rig',
                   'Swimsuit', 'Swim', 'Slippers', 'Flip Flop', 'Flowers',
                   'Straps', 'Neck Watch', 'Glass', 'Glasses', 'Headphones',
                   'Hip Bag', 'Cape', 'Belt (', 'Decals', 'Ear Rings',
                   'Body Suit', 'Magazines', 'Pendant', 'Stocking',
                   'WGT-', 'Upperarm', 'Underwear', 'Thigh Band',
                   'Hat Acc', 'Hat Back', 'Hair Acc', 'Chest Cover Body',
                   'Face (Default)', 'Hair (Default)']

removed = 0
for obj in list(bpy.data.objects):
    name = obj.name

    # Keep the Klukai armature
    if obj.type == 'ARMATURE' and name == KEEP_ARMATURE:
        continue

    # Remove all other armatures
    if obj.type == 'ARMATURE':
        bpy.data.objects.remove(obj, do_unlink=True)
        removed += 1
        continue

    # Remove meshes matching remove keywords
    if obj.type == 'MESH' and any(kw in name for kw in REMOVE_KEYWORDS):
        bpy.data.objects.remove(obj, do_unlink=True)
        removed += 1
        continue

    # Keep meshes matching keep keywords
    if obj.type == 'MESH' and any(kw in name for kw in KEEP_MESH_KEYWORDS):
        continue

    # Remove non-mesh, non-armature objects (lights, cameras, empties)
    if obj.type not in ('MESH', 'ARMATURE'):
        bpy.data.objects.remove(obj, do_unlink=True)
        removed += 1
        continue

print(f"[prep] Removed {removed} objects")

remaining = [(obj.name, obj.type) for obj in bpy.data.objects]
print(f"[prep] Remaining ({len(remaining)}):")
for name, typ in remaining:
    print(f"[prep]   {typ}: {name}")

# Join all remaining meshes into one object for Mixamo
meshes = [obj for obj in bpy.data.objects if obj.type == 'MESH']
if len(meshes) > 1:
    bpy.ops.object.select_all(action='DESELECT')
    for m in meshes:
        m.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.object.join()
    bpy.context.active_object.name = 'Klukai_Body'
    print(f"[prep] Joined {len(meshes)} meshes into Klukai_Body")
elif len(meshes) == 1:
    meshes[0].name = 'Klukai_Body'

# Final count
final = [(obj.name, obj.type) for obj in bpy.data.objects]
print(f"[prep] Final objects ({len(final)}):")
for name, typ in final:
    print(f"[prep]   {typ}: {name}")

# Export as FBX
# Blender 5.0 FBX exporter is broken (use_space_transform error)
# Export as OBJ instead — Mixamo accepts OBJ files
if OUTPUT.endswith('.fbx'):
    OUTPUT = OUTPUT.replace('.fbx', '.obj')
    print(f"[prep] FBX export broken in Blender 5.0, using OBJ: {OUTPUT}")

bpy.ops.wm.obj_export(filepath=OUTPUT)

import os
size = os.path.getsize(OUTPUT)
print(f"[prep] Exported: {OUTPUT} ({size / 1024 / 1024:.1f} MB)")
