"""
Combine Mixamo animation with original Klukai textures.
Opens the Mixamo FBX, applies original materials from the .blend, exports .glb.

Usage: blender --background <original.blend> --python tools/export_mixamo_textured.py -- <mixamo.fbx> <output.glb>
"""
import bpy
import sys
import os
from io_scene_fbx import import_fbx

argv = sys.argv
if "--" in argv:
    argv = argv[argv.index("--") + 1:]
MIXAMO_FBX = argv[0]
OUTPUT_GLB = argv[1]

print(f"[export] Mixamo FBX: {MIXAMO_FBX}")
print(f"[export] Output: {OUTPUT_GLB}")

# The .blend is already loaded (passed as --background argument)
# It has all the original textures and materials

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
    lc.exclude = False; lc.hide_viewport = False
    for c in lc.children: enable_lc(c)
enable_lc(bpy.context.view_layer.layer_collection)

# Save the original materials by mesh name before cleanup
original_materials = {}
for obj in bpy.data.objects:
    if obj.type == 'MESH' and obj.data.materials:
        original_materials[obj.name] = [m.name for m in obj.data.materials if m]

print(f"[export] Original materials saved from {len(original_materials)} meshes")

# Now import the Mixamo FBX (this adds the rigged+animated mesh)
class FakeOp:
    def report(self, *args): pass

result = import_fbx.load(FakeOp(), bpy.context, filepath=MIXAMO_FBX)
print(f"[export] Mixamo FBX import: {result}")

# Find the Mixamo mesh and armature
mixamo_mesh = None
mixamo_armature = None
for obj in bpy.data.objects:
    if obj.type == 'MESH' and 'Klukai_Body' in obj.name:
        # Prefer the one that was just imported (has .001 suffix or is newest)
        if mixamo_mesh is None or obj.name > mixamo_mesh.name:
            mixamo_mesh = obj
    if obj.type == 'ARMATURE' and obj.animation_data and obj.animation_data.action:
        if 'mixamo' in obj.animation_data.action.name.lower():
            mixamo_armature = obj

if mixamo_mesh:
    print(f"[export] Mixamo mesh: {mixamo_mesh.name} ({len(mixamo_mesh.data.materials)} materials)")
else:
    print("[export] ERROR: Mixamo mesh not found!")

if mixamo_armature:
    print(f"[export] Mixamo armature: {mixamo_armature.name} ({len(mixamo_armature.data.bones)} bones)")
    print(f"[export] Action: {mixamo_armature.animation_data.action.name}")
else:
    print("[export] ERROR: Mixamo armature not found!")

# Apply original materials to the Mixamo mesh
# The Mixamo mesh was joined from the same source meshes, so material slots should align
# But they may have been merged. Try to find matching materials by name.
if mixamo_mesh:
    # The joined mesh lost individual material assignments.
    # Apply the most common body material as a starting point.
    # Look for materials with 'cloth' or 'body' in the name from the original
    body_materials = []
    for mat_name in ['GooEngine_ToonShader', 'Body', 'Cloth 1']:
        mat = bpy.data.materials.get(mat_name)
        if mat:
            body_materials.append(mat)

    # If the mesh has no materials, assign the first available original material
    if len(mixamo_mesh.data.materials) == 0:
        for mat in bpy.data.materials:
            if mat.name and 'GooEngine' not in mat.name:
                mixamo_mesh.data.materials.append(mat)
                break
    else:
        # Replace placeholder materials with originals
        for i, slot in enumerate(mixamo_mesh.material_slots):
            # Try to find a matching original material
            if slot.material and slot.material.name in bpy.data.materials:
                continue  # Already has a valid material
            # Assign from body materials
            if i < len(body_materials):
                slot.material = body_materials[i]

    print(f"[export] Materials on Mixamo mesh: {[m.name for m in mixamo_mesh.data.materials if m]}")

# Delete all objects EXCEPT the Mixamo mesh and armature
keep = {mixamo_mesh, mixamo_armature} if mixamo_mesh and mixamo_armature else set()
for obj in list(bpy.data.objects):
    if obj not in keep:
        bpy.data.objects.remove(obj, do_unlink=True)

print(f"[export] Final objects: {[(o.name, o.type) for o in bpy.data.objects]}")

# Export
os.makedirs(os.path.dirname(os.path.abspath(OUTPUT_GLB)) or '.', exist_ok=True)
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
    export_image_format='AUTO',
)

size = os.path.getsize(OUTPUT_GLB)
print(f"[export] Done! {OUTPUT_GLB} ({size / 1024 / 1024:.1f} MB)")
