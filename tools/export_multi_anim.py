"""
Create a .glb with textured mesh + multiple Mixamo animations.
Imports each FBX, renames the action, combines into one armature.

Usage: blender --background --python tools/export_multi_anim.py -- <obj> <output.glb> <idle.fbx> <talking.fbx> ...
"""
import bpy
import sys
import os

argv = sys.argv
if "--" in argv:
    argv = argv[argv.index("--") + 1:]
OBJ_FILE = argv[0]
OUTPUT_GLB = argv[1]
ANIM_FBXS = argv[2:]  # Remaining args are FBX files with format: name:path

TEX_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'assets', 'textures')

print(f"[export] OBJ: {OBJ_FILE}")
print(f"[export] Output: {OUTPUT_GLB}")
print(f"[export] Animations: {ANIM_FBXS}")

bpy.ops.wm.read_factory_settings(use_empty=True)

from io_scene_fbx import import_fbx
class FakeOp:
    def report(self, *a): pass

# ── Import first FBX (provides the armature + mesh + first animation) ───────
first_fbx = ANIM_FBXS[0].split(':')
first_name = first_fbx[0]
first_path = first_fbx[1] if len(first_fbx) > 1 else first_fbx[0]

import_fbx.load(FakeOp(), bpy.context, filepath=first_path)

# Find armature and mesh
main_arm = None
main_mesh = None
for obj in bpy.data.objects:
    if obj.type == 'ARMATURE': main_arm = obj
    if obj.type == 'MESH': main_mesh = obj

# Rename the first action
if main_arm and main_arm.animation_data and main_arm.animation_data.action:
    main_arm.animation_data.action.name = first_name
    print(f"[export] First animation: {first_name}")

# ── Import additional FBX files (animation only) ───────────────────────────
for anim_spec in ANIM_FBXS[1:]:
    parts = anim_spec.split(':')
    anim_name = parts[0]
    anim_path = parts[1] if len(parts) > 1 else parts[0]

    # Import
    import_fbx.load(FakeOp(), bpy.context, filepath=anim_path)

    # Find the newly imported armature (has a different name)
    new_arm = None
    for obj in bpy.data.objects:
        if obj.type == 'ARMATURE' and obj != main_arm:
            new_arm = obj
            break

    if new_arm and new_arm.animation_data and new_arm.animation_data.action:
        # Rename action
        action = new_arm.animation_data.action
        action.name = anim_name
        action.use_fake_user = True
        print(f"[export] Added animation: {anim_name} ({action.frame_range})")

        # Delete the extra armature and mesh
        for obj in list(bpy.data.objects):
            if obj != main_arm and obj != main_mesh and obj.type in ('ARMATURE', 'MESH'):
                bpy.data.objects.remove(obj, do_unlink=True)

# ── Import OBJ for material groups ─────────────────────────────────────────
bpy.ops.wm.obj_import(filepath=OBJ_FILE)

obj_mesh = None
for obj in bpy.data.objects:
    if obj.type == 'MESH' and obj != main_mesh:
        obj_mesh = obj
        break

if obj_mesh and main_mesh:
    print(f"[export] OBJ: {obj_mesh.name} ({len(obj_mesh.material_slots)} materials)")

    # Copy material slots and face assignments
    assert len(main_mesh.data.polygons) == len(obj_mesh.data.polygons)

    for slot in obj_mesh.material_slots:
        mat = slot.material
        main_mesh.data.materials.append(mat)

    for i, poly in enumerate(obj_mesh.data.polygons):
        main_mesh.data.polygons[i].material_index = poly.material_index

    # Delete OBJ mesh
    bpy.data.objects.remove(obj_mesh, do_unlink=True)

# ── Assign textures ────────────────────────────────────────────────────────
MATERIAL_TEXTURES = {
    'Klukai_Body_(Default)': 'body_d.png',
    'Klukai_Hair': 'c_Clukay_hair_d.png',
    'Klukai_Face': 'c_Clukay_face_d.png',
    'Klukai_Cloth_1': 'c_ClukaySSR01_slg_cloth1_d.png',
    'Klukai_Cloth_2': 'c_ClukaySSR01_slg_cloth2_d.png',
    'Klukai_Eyes': 'c_Clukay_eye_d.png',
    'Klukai_Eyeblend': 'c_Clukay_eyeblend.png',
    'Eye_Shadow': 'c_Clukay_eye_d.png',
    # Outline removed - causes dark patches
}

# Assign textures FIRST (before deleting faces, so material slots are intact)
for slot in main_mesh.material_slots:
    mat = slot.material
    if not mat: continue
    tex_file = MATERIAL_TEXTURES.get(mat.name)
    if not tex_file:
        for key, val in MATERIAL_TEXTURES.items():
            if key in mat.name:
                tex_file = val
                break
    if not tex_file:
        tex_file = 'c_ClukaySSR01_slg_cloth2_d.png'

    tex_path = os.path.join(TEX_DIR, tex_file)
    if os.path.exists(tex_path):
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        nodes.clear()
        bsdf = nodes.new('ShaderNodeBsdfPrincipled')
        bsdf.inputs['Roughness'].default_value = 1.0
        bsdf.inputs['Metallic'].default_value = 0.0
        bsdf.inputs['Specular IOR Level'].default_value = 0.0
        output = nodes.new('ShaderNodeOutputMaterial')
        links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
        tex = nodes.new('ShaderNodeTexImage')
        tex.image = bpy.data.images.load(tex_path)
        links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])

# ── Remove bad costume faces (keep eye geometry) ─────────────────────────────
bad_indices = set()
for i, slot in enumerate(main_mesh.material_slots):
    mat = slot.material
    if not mat: continue
    name = mat.name
    if 'Astral' in name or 'Speed' in name or 'Luminous' in name or \
       'Fishnets' in name or 'Leggings_' in name or 'Body_Suit' in name or \
       'Body_(Astral' in name or 'Outline' in name:
        bad_indices.add(i)

if bad_indices:
    bpy.context.view_layer.objects.active = main_mesh
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.object.mode_set(mode='OBJECT')
    for poly in main_mesh.data.polygons:
        if poly.material_index in bad_indices:
            poly.select = True
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.delete(type='FACE')
    bpy.ops.object.mode_set(mode='OBJECT')
    print(f"[export] Removed {len(bad_indices)} costume material groups")
    print("[export] Eye geometry kept with iris textures")

    # Scale eye vertices to fit sockets and push forward
    import mathutils
    EYE_SCALE = 0.6  # shrink to 60% of original size
    for i, slot in enumerate(main_mesh.material_slots):
        mat = slot.material
        if not mat or ('Eye' not in mat.name and 'Eyeblend' not in mat.name):
            continue
        # Collect eye vertex indices and find center
        eye_verts = set()
        for poly in main_mesh.data.polygons:
            if poly.material_index == i:
                for vi in poly.vertices:
                    eye_verts.add(vi)
        if not eye_verts:
            continue
        # Compute center of eye vertices
        center = mathutils.Vector((0, 0, 0))
        for vi in eye_verts:
            center += main_mesh.data.vertices[vi].co
        center /= len(eye_verts)
        # Split into left/right eye groups by X position
        left_verts = [vi for vi in eye_verts if main_mesh.data.vertices[vi].co.x > 0]
        right_verts = [vi for vi in eye_verts if main_mesh.data.vertices[vi].co.x <= 0]

        for group_name, group in [("left", left_verts), ("right", right_verts)]:
            if not group:
                continue
            gcenter = mathutils.Vector((0, 0, 0))
            for vi in group:
                gcenter += main_mesh.data.vertices[vi].co
            gcenter /= len(group)

            # Right eye socket is tighter — push further forward
            forward = 0.0005 if group_name == "left" else 0.001

            for vi in group:
                v = main_mesh.data.vertices[vi]
                v.co = gcenter + (v.co - gcenter) * EYE_SCALE
                v.co.y -= forward

        print(f"[export] Scaled {mat.name}: L={len(left_verts)} R={len(right_verts)} at {EYE_SCALE}x")

# ── Make sure all actions are linked to the armature ─────────────────────────
# The exporter needs all actions to be assigned or use_fake_user=True
for action in bpy.data.actions:
    action.use_fake_user = True
    print(f"[export] Action ready: {action.name}")

# Set the first action as active
if main_arm and bpy.data.actions:
    main_arm.animation_data.action = bpy.data.actions[0]

# ── Export ──────────────────────────────────────────────────────────────────
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
