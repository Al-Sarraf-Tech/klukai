"""
Combine Mixamo FBX (skeleton+animation+vertex groups) with OBJ (material groups) + textures.
Both meshes have identical geometry (105824 verts, 154788 faces).
Copy material assignments from OBJ → Mixamo mesh, add textures, export .glb.

Usage: blender --background --python tools/export_textured_mixamo.py -- <obj> <mixamo.fbx> <output.glb>
"""
import bpy
import sys
import os

argv = sys.argv
if "--" in argv:
    argv = argv[argv.index("--") + 1:]
OBJ_FILE = argv[0]
MIXAMO_FBX = argv[1]
OUTPUT_GLB = argv[2]
TEX_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'assets', 'textures')

print(f"[export] OBJ: {OBJ_FILE}")
print(f"[export] FBX: {MIXAMO_FBX}")
print(f"[export] Output: {OUTPUT_GLB}")
print(f"[export] Textures: {TEX_DIR}")

bpy.ops.wm.read_factory_settings(use_empty=True)

# ── Import Mixamo FBX (skeleton + animation + vertex groups) ────────────────
from io_scene_fbx import import_fbx
class FakeOp:
    def report(self, *a): pass
import_fbx.load(FakeOp(), bpy.context, filepath=MIXAMO_FBX)

mixamo_mesh = None
mixamo_arm = None
for obj in bpy.data.objects:
    if obj.type == 'MESH':
        mixamo_mesh = obj
    if obj.type == 'ARMATURE':
        mixamo_arm = obj

print(f"[export] Mixamo: {mixamo_mesh.name} ({len(mixamo_mesh.vertex_groups)} vgroups, {len(mixamo_mesh.data.polygons)} faces)")

# ── Import OBJ (material groups) ───────────────────────────────────────────
bpy.ops.wm.obj_import(filepath=OBJ_FILE)

obj_mesh = None
for obj in bpy.data.objects:
    if obj.type == 'MESH' and obj != mixamo_mesh:
        obj_mesh = obj
        break

print(f"[export] OBJ: {obj_mesh.name} ({len(obj_mesh.material_slots)} materials, {len(obj_mesh.data.polygons)} faces)")

# ── Copy material slots from OBJ to Mixamo mesh ────────────────────────────
# Both meshes have identical face count and order
assert len(mixamo_mesh.data.polygons) == len(obj_mesh.data.polygons), \
    f"Face count mismatch: {len(mixamo_mesh.data.polygons)} vs {len(obj_mesh.data.polygons)}"

# Copy material slots
for slot in obj_mesh.material_slots:
    mat = slot.material
    if mat:
        mixamo_mesh.data.materials.append(mat)
    else:
        mixamo_mesh.data.materials.append(None)

# Copy per-face material indices
for i, poly in enumerate(obj_mesh.data.polygons):
    mixamo_mesh.data.polygons[i].material_index = poly.material_index

print(f"[export] Copied {len(obj_mesh.material_slots)} material slots + face assignments")

# Delete OBJ mesh (no longer needed)
bpy.data.objects.remove(obj_mesh, do_unlink=True)

# ── Texture mappings ────────────────────────────────────────────────────────
MATERIAL_TEXTURES = {
    'Klukai_Body_(Default)': 'c_ClukaySSR01_slg_cloth2_d.png',
    'Klukai_Hair': 'c_Clukay_hair_d.png',
    'Klukai_Face': 'c_Clukay_face_d.png',
    'Klukai_Eyeblend': 'c_Clukay_eyeblend.png',
    'Klukai_Eyes': 'c_Clukay_eye_d.png',
    'Eye_Shadow': 'c_Clukay_face_d.png',
    'Klukai_Cloth_1': 'c_ClukaySSR01_slg_cloth1_d.png',
    'Klukai_Cloth_2': 'c_ClukaySSR01_slg_cloth2_d.png',
    'Outline': 'c_ClukaySSR01_slg_cloth2_d.png',  # Keep outline as dark body
}

# Delete ONLY non-default SKIN faces (Astral Luminous, Speed Star, etc.)
# Keep Outline faces — they provide neck/gap geometry
bad_indices = set()
for i, slot in enumerate(mixamo_mesh.material_slots):
    mat = slot.material
    if not mat:
        bad_indices.add(i)
        continue
    name = mat.name
    # Remove alt-skin faces
    if 'Astral' in name or 'Speed' in name or 'Luminous' in name or \
       'Fishnets' in name or 'Leggings_' in name or 'Body_Suit' in name or \
       'Body_(Astral' in name:
        bad_indices.add(i)
        print(f"[export]   Removing alt-skin faces: {name}")
    # Remove 3D eye meshes — the face texture already has painted anime eyes
    elif 'Eyes' in name or 'Eyeblend' in name or 'Eye_Shadow' in name:
        bad_indices.add(i)
        print(f"[export]   Removing 3D eye overlay: {name}")

if bad_indices:
    bpy.context.view_layer.objects.active = mixamo_mesh
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.object.mode_set(mode='OBJECT')
    for poly in mixamo_mesh.data.polygons:
        if poly.material_index in bad_indices:
            poly.select = True
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.delete(type='FACE')
    bpy.ops.object.mode_set(mode='OBJECT')
    print(f"[export] Deleted faces from {len(bad_indices)} alt-skin material slots")

# Assign textures to ALL materials — anime-friendly PBR settings
for slot in mixamo_mesh.material_slots:
    mat = slot.material
    if not mat:
        continue

    # Find texture — exact match first, then partial
    tex_file = MATERIAL_TEXTURES.get(mat.name)
    if not tex_file:
        for key, val in MATERIAL_TEXTURES.items():
            if key in mat.name:
                tex_file = val
                break
    # Fallback: use body texture for unknown materials
    if not tex_file:
        tex_file = 'c_ClukaySSR01_slg_cloth2_d.png'

    tex_path = os.path.join(TEX_DIR, tex_file)
    if os.path.exists(tex_path):
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        nodes.clear()
        bsdf = nodes.new('ShaderNodeBsdfPrincipled')
        bsdf.location = (0, 0)
        # Anime-friendly: fully rough, non-metallic
        bsdf.inputs['Roughness'].default_value = 1.0
        bsdf.inputs['Metallic'].default_value = 0.0
        bsdf.inputs['Specular IOR Level'].default_value = 0.0
        output = nodes.new('ShaderNodeOutputMaterial')
        output.location = (300, 0)
        links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
        tex = nodes.new('ShaderNodeTexImage')
        tex.location = (-300, 0)
        tex.image = bpy.data.images.load(tex_path)
        links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
        print(f"[export]   {mat.name} → {tex_file}")
    else:
        print(f"[export]   {mat.name} → MISSING {tex_path}")

# ── Export ───────────────────────────────────────────────────────────────────
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
