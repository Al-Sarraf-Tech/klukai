"""Convert Player Legend's HK416 source to a private companion GLB.

Run with checksum-verified Blender 4.1.1 (Python autoexec disabled):
  blender -b --factory-startup --disable-autoexec --threads 2 \
    --python tools/avatar/import-gfl64.py -- SOURCE_RIG_BLEND OUTPUT_GLB

Source: PlayerLegend/char-select-girls-frontline, commit
148c3b7bcf2b7dac751e267b428b4afa389208a8, blend/hk416/rig.blend.
Model by Player Legend, CC BY-SA 4.0. Character rights MICA/Sunborn.
Changes: F3D materials converted to glTF, wing-cap parts omitted, rig rebound
and renamed for this viewer, creator idle baked, metre-scale parent added.
This converter never changes the approved default and requires explicit output.
"""

import sys
from pathlib import Path
import bpy
import bmesh

source, destination = sys.argv[sys.argv.index("--") + 1 :]
out = Path(destination).resolve()
if out.name == "klukai.glb" or out == Path(source).resolve():
    raise ValueError("Refusing to overwrite the pinned default; use a variant output")
out.parent.mkdir(parents=True, exist_ok=True)
bpy.ops.wm.open_mainfile(filepath=str(Path(source).resolve()), use_scripts=False)
a = bpy.data.objects["Mario"]
m = bpy.data.objects["Mario Mesh"]
for o in list(bpy.data.objects):
    if o not in (a, m):
        bpy.data.objects.remove(o, do_unlink=True)
# Remove the Nintendo wing-cap geometry, unrelated to HK416.
bm = bmesh.new()
bm.from_mesh(m.data)
wing = {i for i, x in enumerate(m.data.materials) if x.name.startswith("Wing")}
bmesh.ops.delete(
    bm, geom=[f for f in bm.faces if f.material_index in wing], context="FACES"
)
bm.to_mesh(m.data)
bm.free()
for mat in m.data.materials:
    if not mat:
        continue
    cfg = mat.get("f3d_mat", {})
    tex = cfg.get("tex0", {}).get("tex")
    color = cfg.get("default_light_color", [1, 1, 1, 1])
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    nodes.clear()
    output_node = nodes.new("ShaderNodeOutputMaterial")
    bs = nodes.new("ShaderNodeBsdfPrincipled")
    mat.node_tree.links.new(bs.outputs["BSDF"], output_node.inputs["Surface"])
    bs.inputs["Base Color"].default_value = color
    bs.inputs["Roughness"].default_value = 0.9
    if isinstance(tex, bpy.types.Image):
        n = nodes.new("ShaderNodeTexImage")
        n.image = tex
        n.interpolation = "Closest"
        mat.node_tree.links.new(n.outputs["Color"], bs.inputs["Base Color"])
        if mat.name.startswith("alpha"):
            mat.node_tree.links.new(n.outputs["Alpha"], bs.inputs["Alpha"])
            mat.blend_method = "CLIP"
            mat.alpha_threshold = 0.5
    mat.use_backface_culling = False
# Keep the creator's idle animation, excluding game-specific actions.
idle = bpy.data.actions["Idle Bake"].copy()
idle.name = "idle"
a.animation_data_clear()
a.animation_data_create()
a.animation_data.action = idle
for ac in list(bpy.data.actions):
    if ac != idle:
        bpy.data.actions.remove(ac)
bpy.context.scene.frame_set(1)
bpy.context.view_layer.update()
bpy.context.scene.frame_start = 1
bpy.context.scene.frame_end = 29

# Capture the creator's idle in armature space, then bind the mesh at idle start.
frames = {}
for f in range(1, 30):
    bpy.context.scene.frame_set(f)
    bpy.context.view_layer.update()
    frames[f] = {b.name: b.matrix.copy() for b in a.pose.bones}
bpy.context.scene.frame_set(1)
bpy.context.view_layer.update()
bpy.context.view_layer.objects.active = m
m.select_set(True)
bpy.ops.object.modifier_apply(modifier=m.modifiers[0].name)
a.select_set(True)
m.select_set(False)
bpy.context.view_layer.objects.active = a
bpy.ops.object.mode_set(mode="POSE")
bpy.ops.pose.armature_apply(selected=False)
bpy.ops.object.mode_set(mode="OBJECT")
a.animation_data_clear()
a.animation_data_create()
act = bpy.data.actions.new("idle-rebased")
a.animation_data.action = act
mod = m.modifiers.new("Avatar rig", "ARMATURE")
mod.object = a
for f, poses in frames.items():
    bpy.context.scene.frame_set(f)
    for b in a.pose.bones:
        b.rotation_mode = "QUATERNION"
        b.matrix = poses[b.name]
        bpy.context.view_layer.update()
        b.keyframe_insert("location", frame=f)
        b.keyframe_insert("rotation_quaternion", frame=f)
        b.keyframe_insert("scale", frame=f)
for ac in list(bpy.data.actions):
    if ac != act:
        bpy.data.actions.remove(ac)
act.name = "idle"
names = {
    "Butt": "Hips",
    "Torso": "Spine2",
    "Head": "Neck",
    "Head DL": "Head",
    "Left Arm": "LeftArm",
    "Left Forearm": "LeftForeArm",
    "Left Hand DL": "LeftHand",
    "Right Arm": "RightArm",
    "Right Forearm": "RightForeArm",
    "Right Hand DL": "RightHand",
    "Left Thigh": "LeftUpLeg",
    "Left Leg": "LeftLeg",
    "Left Shoe": "LeftFoot",
    "Right Thigh": "RightUpLeg",
    "Right Leg": "RightLeg",
    "Right Shoe DL": "RightFoot",
}
for n, t in names.items():
    a.data.bones[n].name = "mixamorig" + t
# Parent all geometry and armature to the same metre-scale node.
p = bpy.data.objects.new("HK416_GFL64", None)
bpy.context.collection.objects.link(p)
a.parent = p
p.scale = (0.57,) * 3
bpy.context.scene.frame_set(1)
bpy.context.view_layer.update()

# Do not export source-editor paths or unrelated add-on properties.
for block in [
    *bpy.data.scenes,
    *bpy.data.objects,
    *bpy.data.meshes,
    *bpy.data.materials,
    *a.data.bones,
]:
    for key in list(block.keys()):
        del block[key]
bpy.context.scene["preferredFraming"] = "full"
bpy.context.scene["framingHeadY"] = (
    1.44  # chibi crown height needs a taller camera frame
)
bpy.ops.export_scene.gltf(
    filepath=str(out),
    export_format="GLB",
    export_animations=True,
    export_nla_strips=False,
    export_skins=True,
    export_extras=True,
    export_yup=True,
)
print("EXPORTED", out)
