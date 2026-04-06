"""
Import Mixamo FBX (bypassing broken operator) and export as .glb for Three.js.
Usage: blender --background --python tools/export_mixamo_glb.py -- <input.fbx> <output.glb>
"""
import bpy
import sys
import os

argv = sys.argv
if "--" in argv:
    argv = argv[argv.index("--") + 1:]
INPUT_FBX = argv[0]
OUTPUT_GLB = argv[1]

print(f"[export] Input: {INPUT_FBX}")
print(f"[export] Output: {OUTPUT_GLB}")

# Start clean
bpy.ops.wm.read_factory_settings(use_empty=True)

# Import FBX via direct module call (Blender 5.0 operator is broken)
from io_scene_fbx import import_fbx

class FakeOp:
    def report(self, *args): pass

result = import_fbx.load(FakeOp(), bpy.context, filepath=INPUT_FBX)
print(f"[export] FBX import: {result}")

# Report what we have
for obj in bpy.data.objects:
    info = f"[export] {obj.type}: {obj.name}"
    if obj.type == 'ARMATURE':
        info += f" ({len(obj.data.bones)} bones)"
        if obj.animation_data and obj.animation_data.action:
            info += f" action='{obj.animation_data.action.name}'"
    print(info)

print(f"[export] Actions: {[a.name for a in bpy.data.actions]}")

# Export as GLB
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
)

size = os.path.getsize(OUTPUT_GLB)
print(f"[export] Done! {OUTPUT_GLB} ({size / 1024 / 1024:.1f} MB)")
