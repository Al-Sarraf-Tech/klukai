"""Export one complete Garf outfit with a clean canonical-name deformation rig.

Run with Blender 4.1.1, factory startup and Python autoexec disabled:
  blender -b --factory-startup --disable-autoexec --threads 2 \
    --python tools/avatar/import-garf.py -- SOURCE_BLEND OUTFIT OUTPUT_RAW_GLB

Source: Garf, Open3DLab project 9ed8198a-df22-46db-81dd-676d0d6da019.
Klukai_V1.blend SHA256 a9f6609313c0a32269aaf5cd2002d34267610a2e6645eece6e8f512a8d091473.
Uploader-selected CC BY-NC-ND 4.0. Private noncommercial adaptation only;
no distribution of converted assets. Underlying character/model rights remain
with their holders. Source scripts and Goo Engine shaders are not executed.
Follow with retarget-variant.mjs to add the pinned avatar's animations.
"""

import sys
import re
import bpy
import bmesh
from pathlib import Path
from mathutils import Matrix

source, name, destination = sys.argv[sys.argv.index("--") + 1 :]
out_path = Path(destination).resolve()
if out_path.name == "klukai.glb" or out_path == Path(source).resolve():
    raise ValueError("Refusing to overwrite the pinned default")
bpy.ops.wm.open_mainfile(filepath=str(Path(source).resolve()), use_scripts=False)
collections = {
    "default": "Default Outfit",
    "dorm": "Dorm Outfit",
    "speed_star": "Speed Star Outfit",
    "astral_luminous": "Astral Luminous Outfit",
    "cerulean_breaker": "Cerulean Breaker Outfit",
}
rig = bpy.data.objects["RIG-Klukai" if name == "default" else "RIG-Skins"]
rig.data.pose_position = "REST"
objects = [
    o
    for o in bpy.data.collections[collections[name]].all_objects
    if o.type == "MESH" and o.parent == rig and not o.hide_render
]
if name != "default":
    objects.append(bpy.data.objects["Face"])
    if name != "cerulean_breaker":
        objects.append(bpy.data.objects["Hair"])
print("PARTS", name, [o.name for o in objects], flush=True)
# Deformation skeleton only, with original rest matrices and ancestor links.
mapping = {
    "Root_M": "Hips",
    "Spine1_M": "Spine",
    "Spine2_M": "Spine1",
    "Chest_M": "Spine2",
    "Neck_M": "Neck",
    "Head_M": "Head",
}
for side, lr in [("L", "Left"), ("R", "Right")]:
    mapping.update(
        {
            f"{game}_{side}": lr + mix
            for game, mix in [
                ("Scapula", "Shoulder"),
                ("Shoulder", "Arm"),
                ("Elbow", "ForeArm"),
                ("Wrist", "Hand"),
                ("Hip", "UpLeg"),
                ("Knee", "Leg"),
                ("Ankle", "Foot"),
                ("Toes", "ToeBase"),
            ]
        }
    )
    for digit in ["Thumb", "Index", "Middle", "Ring", "Pinky"]:
        for k in [1, 2, 3]:
            mapping[f"{digit}Finger{k}_{side}"] = lr + "Hand" + digit + str(k)
rename = {
    b.name: "mixamorig" + mapping[b.name[4:]] if b.name[4:] in mapping else b.name
    for b in rig.data.bones
    if b.use_deform
}
arm = bpy.data.armatures.new("KlukaiDeform")
clean = bpy.data.objects.new("Klukai", arm)
bpy.context.collection.objects.link(clean)
bpy.context.view_layer.objects.active = clean
clean.select_set(True)
bpy.ops.object.mode_set(mode="EDIT")
for b in rig.data.bones:
    if b.name not in rename:
        continue
    n = arm.edit_bones.new(rename[b.name])
    n.head = b.head_local
    n.tail = b.tail_local
    n.matrix = b.matrix_local
    n.use_deform = True
for b in rig.data.bones:
    if b.name not in rename:
        continue
    parent = b.parent
    while parent:
        candidate = (
            parent.name
            if parent.name in rename
            else "DEF-" + parent.name.removeprefix("ORG-")
        )
        if candidate in rename and candidate != b.name:
            arm.edit_bones[rename[b.name]].parent = arm.edit_bones[rename[candidate]]
            break
        parent = parent.parent
bpy.ops.object.mode_set(mode="OBJECT")
for o in objects:
    o.hide_set(False)
    o.hide_viewport = False
    o.hide_render = False
    # This source material is a fully transparent shader helper, not eyelid
    # geometry. Exporting it as Principled creates a white bar over each iris.
    hidden_slots = {i for i, m in enumerate(o.data.materials) if m and m.name == "Eye Shadow"}
    if hidden_slots:
        mesh = bmesh.new()
        mesh.from_mesh(o.data)
        bmesh.ops.delete(
            mesh,
            geom=[f for f in mesh.faces if f.material_index in hidden_slots],
            context="FACES_ONLY",
        )
        mesh.to_mesh(o.data)
        mesh.free()
    for mod in list(o.modifiers):
        o.modifiers.remove(mod)
    if o.animation_data:
        o.animation_data_clear()
    o.parent = clean
    o.matrix_parent_inverse = Matrix.Identity(4)
    # The shared source rig lacks these Astral accessory bones. Bind them to
    # their actual attachment joints instead of exporting stationary vertices.
    attachments = {
        "Pistol Magazines": {
            "DEF-clip_hipR1": "DEF-HipPart1_R",
            "DEF-clip_hipR": "DEF-HipPart1_R",
        },
        "Clothes (Astral Luminous)": {"DEF-zipper": "DEF-Spine2_M"},
        "Belt (Astral Luminous)": {"DEF-stick4": "DEF-Spine2_M"},
    }
    for old_name, joint in attachments.get(o.name, {}).items():
        old = o.vertex_groups.get(old_name)
        if old is None or old_name in rename:
            continue
        weights = [
            (v.index, g.weight)
            for v in o.data.vertices
            for g in v.groups
            if g.group == old.index
        ]
        dst = o.vertex_groups.get(joint) or o.vertex_groups.new(name=joint)
        for vertex, weight in weights:
            dst.add([vertex], weight, "ADD")
        o.vertex_groups.remove(old)
    for vg in o.vertex_groups:
        if vg.name in rename:
            vg.name = rename[vg.name]
    deform_groups = {g.index for g in o.vertex_groups if g.name in arm.bones}
    unweighted = [
        v.index
        for v in o.data.vertices
        if sum(g.weight for g in v.groups if g.group in deform_groups) < 0.001
    ]
    if unweighted:
        raise ValueError(f"{o.name}: {len(unweighted)} vertices have no exported joint")
    mod = o.modifiers.new("Avatar rig", "ARMATURE")
    mod.object = clean
    if o.data.shape_keys:
        o.data.shape_keys.animation_data_clear()
        keys = o.data.shape_keys.key_blocks
        for k in keys:
            k.value = 0
        for n in [
            "Eyes_Close_Up_Right",
            "Eyes_Close_Up_Left",
            "Eyes_Close_Down_Right",
            "Eyes_Close_Down_Left",
        ]:
            if n in keys:
                keys[n].value = 1
        blink = o.shape_key_add(name="blink", from_mix=True)
        for k in keys:
            k.value = 0
        for k in list(keys):
            if k.name not in ["Basis", "blink"]:
                o.shape_key_remove(k)
# Rebuild albedo materials using packed images. Goo-specific shaders stay offline.
materials = {m for o in objects for m in o.data.materials if m}
for mat in materials:
    images = (
        [n.image for n in mat.node_tree.nodes if n.type == "TEX_IMAGE" and n.image]
        if mat.node_tree
        else []
    )
    image = next((i for i in images if re.search(r"_(?:d|da)\.tga", i.name)), None)
    role = (
        "overlay"
        if mat.name == "Klukai Eyeblend"
        else "hair"
        if "Hair" in mat.name and "Acc" not in mat.name
        else "face"
        if mat.name in {"Klukai Face", "Klukai Eyes"}
        else "cloth"
    )
    if mat.name == "Klukai Eyeblend":
        image = images[0] if images else None
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    nodes.clear()
    out = nodes.new("ShaderNodeOutputMaterial")
    bs = nodes.new("ShaderNodeBsdfPrincipled")
    mat.node_tree.links.new(bs.outputs["BSDF"], out.inputs["Surface"])
    bs.inputs["Roughness"].default_value = 0.85
    if image:
        if max(image.size) > 1024:
            ratio = 1024 / max(image.size)
            image.scale(int(image.size[0] * ratio), int(image.size[1] * ratio))
        tex = nodes.new("ShaderNodeTexImage")
        tex.image = image
        mat.node_tree.links.new(tex.outputs["Color"], bs.inputs["Base Color"])
        if "_da." in image.name or "Eyeblend" in mat.name:
            mat.node_tree.links.new(tex.outputs["Alpha"], bs.inputs["Alpha"])
            mat.blend_method = "CLIP"
            mat.alpha_threshold = 0.5
    mat.use_backface_culling = False
    for k in list(mat.keys()):
        del mat[k]
    mat["role"] = role
for o in list(bpy.data.objects):
    if o not in objects and o != clean:
        bpy.data.objects.remove(o, do_unlink=True)
for ac in list(bpy.data.actions):
    bpy.data.actions.remove(ac)
for o in [*objects, clean, *bpy.data.scenes, *arm.bones]:
    for k in list(o.keys()):
        del o[k]
# Put only intended objects in an active collection regardless of source UI exclusions.
for o in [clean, *objects]:
    for c in list(o.users_collection):
        c.objects.unlink(o)
    bpy.context.scene.collection.objects.link(o)
bpy.context.scene.frame_set(0)
bpy.context.view_layer.update()
p = out_path
p.parent.mkdir(exist_ok=True, parents=True)
bpy.ops.export_scene.gltf(
    filepath=str(p),
    export_format="GLB",
    export_animations=False,
    export_skins=True,
    export_extras=True,
    export_yup=True,
    export_morph=True,
)
print("EXPORTED", name, p.stat().st_size, flush=True)
