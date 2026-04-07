"""
Export Klukai GLB with separate mesh objects per material group.
Eyes, face, hair, clothes are individual objects — no z-fighting.

Usage: blender --background --python tools/export_split_mesh.py -- <obj> <output.glb> <anim1.fbx> ...
"""
import bpy
import sys
import os

argv = sys.argv
if "--" in argv:
    argv = argv[argv.index("--") + 1:]
OBJ_FILE = argv[0]
OUTPUT_GLB = argv[1]
ANIM_FBXS = argv[2:]

TEX_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'assets', 'textures')

MATERIAL_TEXTURES = {
    'Klukai_Body_(Default)': 'body_d.png',
    'Klukai_Hair': 'c_Clukay_hair_d.png',
    'Klukai_Face': 'c_Clukay_face_d.png',
    'Klukai_Cloth_1': 'c_ClukaySSR01_slg_cloth1_d.png',
    'Klukai_Cloth_2': 'c_ClukaySSR01_slg_cloth2_d.png',
    'Klukai_Eyes': 'c_Clukay_eye_d.png',
    'Klukai_Eyeblend': 'c_Clukay_eyeblend.png',
    'Eye_Shadow': 'c_Clukay_eye_d.png',
}

# Materials to completely remove (costume variants, outline)
REMOVE_MATERIALS = {
    'Astral', 'Speed', 'Luminous', 'Fishnets', 'Leggings_',
    'Body_Suit', 'Body_(Astral', 'Outline',
}

print(f"[export] OBJ: {OBJ_FILE}")
print(f"[export] Output: {OUTPUT_GLB}")
print(f"[export] Animations: {len(ANIM_FBXS)}")

bpy.ops.wm.read_factory_settings(use_empty=True)

from io_scene_fbx import import_fbx
class FakeOp:
    def report(self, *a): pass

# ── Import first FBX (armature + first animation) ────────────────────────────
first_fbx = ANIM_FBXS[0].split(':')
first_name = first_fbx[0]
first_path = first_fbx[1] if len(first_fbx) > 1 else first_fbx[0]

import_fbx.load(FakeOp(), bpy.context, filepath=first_path)

main_arm = None
fbx_mesh = None
for obj in bpy.data.objects:
    if obj.type == 'ARMATURE': main_arm = obj
    if obj.type == 'MESH': fbx_mesh = obj

if main_arm and main_arm.animation_data and main_arm.animation_data.action:
    main_arm.animation_data.action.name = first_name
    print(f"[export] First animation: {first_name}")

# ── Import additional FBX animations ─────────────────────────────────────────
for anim_spec in ANIM_FBXS[1:]:
    parts = anim_spec.split(':')
    anim_name = parts[0]
    anim_path = parts[1] if len(parts) > 1 else parts[0]
    import_fbx.load(FakeOp(), bpy.context, filepath=anim_path)
    new_arm = None
    for obj in bpy.data.objects:
        if obj.type == 'ARMATURE' and obj != main_arm:
            new_arm = obj
            break
    if new_arm and new_arm.animation_data and new_arm.animation_data.action:
        action = new_arm.animation_data.action
        action.name = anim_name
        action.use_fake_user = True
        print(f"[export] Added animation: {anim_name}")
        for obj in list(bpy.data.objects):
            if obj != main_arm and obj != fbx_mesh and obj.type in ('ARMATURE', 'MESH'):
                bpy.data.objects.remove(obj, do_unlink=True)

# ── Import OBJ for material assignments, transfer to FBX mesh, then split ────
bpy.ops.wm.obj_import(filepath=OBJ_FILE)
obj_mesh = None
for obj in bpy.data.objects:
    if obj.type == 'MESH' and obj != fbx_mesh:
        obj_mesh = obj
        break

print(f"[export] OBJ imported: {len(obj_mesh.material_slots)} materials")

# Copy material slots and face assignments from OBJ to FBX mesh (which has weights)
assert len(fbx_mesh.data.polygons) == len(obj_mesh.data.polygons), \
    f"Poly count mismatch: FBX={len(fbx_mesh.data.polygons)} OBJ={len(obj_mesh.data.polygons)}"

for slot in obj_mesh.material_slots:
    fbx_mesh.data.materials.append(slot.material)
for i, poly in enumerate(obj_mesh.data.polygons):
    fbx_mesh.data.polygons[i].material_index = poly.material_index

# Delete OBJ mesh (FBX mesh now has materials + weights)
bpy.data.objects.remove(obj_mesh, do_unlink=True)

# Remove bad material faces
bad_indices = set()
for i, slot in enumerate(fbx_mesh.material_slots):
    mat = slot.material
    if not mat: continue
    if any(kw in mat.name for kw in REMOVE_MATERIALS):
        bad_indices.add(i)

if bad_indices:
    bpy.context.view_layer.objects.active = fbx_mesh
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.object.mode_set(mode='OBJECT')
    for poly in fbx_mesh.data.polygons:
        if poly.material_index in bad_indices:
            poly.select = True
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.delete(type='FACE')
    bpy.ops.object.mode_set(mode='OBJECT')
    print(f"[export] Removed {len(bad_indices)} costume material groups")

# Split FBX mesh by material → separate objects (keeps vertex weights!)
bpy.context.view_layer.objects.active = fbx_mesh
fbx_mesh.select_set(True)
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_all(action='SELECT')
bpy.ops.mesh.separate(type='MATERIAL')
bpy.ops.object.mode_set(mode='OBJECT')

# ── Scale eye meshes to fit inside face sockets ──────────────────────────────
import mathutils
EYE_SCALE = 0.55
for obj in bpy.data.objects:
    if obj.type != 'MESH':
        continue
    mat = obj.data.materials[0] if obj.data.materials else None
    if not mat or 'Eye' not in mat.name:
        continue
    # Split into left/right by X, scale each independently
    verts = list(range(len(obj.data.vertices)))
    left = [vi for vi in verts if obj.data.vertices[vi].co.x > 0]
    right = [vi for vi in verts if obj.data.vertices[vi].co.x <= 0]
    for group_name, group in [("L", left), ("R", right)]:
        if not group:
            continue
        center = mathutils.Vector((0,0,0))
        for vi in group:
            center += obj.data.vertices[vi].co
        center /= len(group)
        for vi in group:
            v = obj.data.vertices[vi]
            v.co = center + (v.co - center) * EYE_SCALE
            # Right eye needs extra forward push (tighter socket)
            v.co.y -= 0.001 if group_name == "R" else 0.0005
    print(f"[export] Scaled eyes: {mat.name} L={len(left)} R={len(right)} at {EYE_SCALE}x")

# ── Set up each split mesh: texture, parent to armature, armature modifier ───
for obj in list(bpy.data.objects):
    if obj.type != 'MESH':
        continue

    # Get the material name for this object
    mat = obj.data.materials[0] if obj.data.materials else None
    if not mat:
        bpy.data.objects.remove(obj, do_unlink=True)
        continue

    mat_name = mat.name
    label = mat_name.replace('Klukai_', '')

    # Assign texture
    tex_file = MATERIAL_TEXTURES.get(mat_name)
    if not tex_file:
        for key, val in MATERIAL_TEXTURES.items():
            if key in mat_name:
                tex_file = val
                break
    if not tex_file:
        tex_file = 'body_d.png'

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
        # Eyes get some specular
        if 'Eye' in mat_name:
            bsdf.inputs['Roughness'].default_value = 0.2
            bsdf.inputs['Specular IOR Level'].default_value = 0.8
        output = nodes.new('ShaderNodeOutputMaterial')
        links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
        tex = nodes.new('ShaderNodeTexImage')
        tex.image = bpy.data.images.load(tex_path)
        links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])

    # Parent to armature
    obj.parent = main_arm
    obj.parent_type = 'OBJECT'

    # Add armature modifier
    has_mod = False
    for mod in obj.modifiers:
        if mod.type == 'ARMATURE':
            mod.object = main_arm
            has_mod = True
    if not has_mod:
        mod = obj.modifiers.new(name='Armature', type='ARMATURE')
        mod.object = main_arm

    # Rename for clarity
    obj.name = f"Klukai_{label}"
    print(f"[export] Part: {obj.name} -> {tex_file} ({len(obj.data.polygons)} faces)")

# ── Make sure all actions are ready ──────────────────────────────────────────
for action in bpy.data.actions:
    action.use_fake_user = True

if main_arm and bpy.data.actions:
    main_arm.animation_data.action = bpy.data.actions[0]

# ── Export ────────────────────────────────────────────────────────────────────
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
