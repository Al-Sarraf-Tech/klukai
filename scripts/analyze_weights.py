#!/usr/bin/env python3
"""
Analyze vertex weight data for Shoulder_L and Spine1_M bones in klukai.glb.
"""

import struct
import json
import sys

GLB_PATH = "/home/jalsarraf/git/companion/web-build/assets/models/klukai.glb"

# --------------------------------------------------------------------------- #
# 1. Parse GLB header + JSON chunk
# --------------------------------------------------------------------------- #
with open(GLB_PATH, "rb") as f:
    raw = f.read()

magic, version, length = struct.unpack_from("<III", raw, 0)
assert magic == 0x46546C67, "Not a GLB file"
print(f"GLB version={version}, total_bytes={length}")

# JSON chunk
json_chunk_len, json_chunk_type = struct.unpack_from("<II", raw, 12)
assert json_chunk_type == 0x4E4F534A, "First chunk is not JSON"
json_bytes = raw[20 : 20 + json_chunk_len]
gltf = json.loads(json_bytes.decode("utf-8"))

# Binary chunk
bin_offset = 20 + json_chunk_len
bin_chunk_len, bin_chunk_type = struct.unpack_from("<II", raw, bin_offset)
assert bin_chunk_type == 0x004E4942, "Second chunk is not BIN"
bin_data = raw[bin_offset + 8 : bin_offset + 8 + bin_chunk_len]
print(f"JSON chunk: {json_chunk_len} bytes | BIN chunk: {bin_chunk_len} bytes\n")

# --------------------------------------------------------------------------- #
# 2. Find skin named "Klukai"
# --------------------------------------------------------------------------- #
skins = gltf.get("skins", [])
target_skin_idx = None
target_skin = None
for i, skin in enumerate(skins):
    if skin.get("name", "") == "Klukai":
        target_skin_idx = i
        target_skin = skin
        break

if target_skin is None:
    print("ERROR: No skin named 'Klukai' found.")
    print("Available skins:", [s.get("name") for s in skins])
    sys.exit(1)

joints = target_skin["joints"]  # list of node indices
print(f"Skin 'Klukai' found at index {target_skin_idx}, {len(joints)} joints")

# --------------------------------------------------------------------------- #
# 3. Find joint indices for target bones
# --------------------------------------------------------------------------- #
nodes = gltf.get("nodes", [])

def find_joint_index(bone_name):
    for joint_slot, node_idx in enumerate(joints):
        if nodes[node_idx].get("name", "") == bone_name:
            return joint_slot, node_idx
    return None, None

shoulder_l_slot, shoulder_l_node = find_joint_index("Shoulder_L")
spine1m_slot,    spine1m_node    = find_joint_index("Spine1_M")

if shoulder_l_slot is None:
    print("ERROR: 'Shoulder_L' not found in skin joints.")
    # print all joint names
    print("All joint names:", [nodes[j].get("name") for j in joints])
    sys.exit(1)
if spine1m_slot is None:
    print("ERROR: 'Spine1_M' not found in skin joints.")
    sys.exit(1)

print(f"Shoulder_L → joint slot {shoulder_l_slot} (node index {shoulder_l_node})")
print(f"Spine1_M   → joint slot {spine1m_slot} (node index {spine1m_node})\n")

# --------------------------------------------------------------------------- #
# 4. Find nodes that use this skin and have a mesh
# --------------------------------------------------------------------------- #
skin_mesh_nodes = []
for i, node in enumerate(nodes):
    if node.get("skin") == target_skin_idx and "mesh" in node:
        skin_mesh_nodes.append((i, node))

print(f"Nodes using skin {target_skin_idx} with a mesh: {len(skin_mesh_nodes)}")
for ni, nd in skin_mesh_nodes:
    mesh_idx = nd["mesh"]
    mesh_name = gltf["meshes"][mesh_idx].get("name", f"mesh_{mesh_idx}")
    print(f"  node {ni} '{nd.get('name','')}' → mesh {mesh_idx} '{mesh_name}'")
print()

# --------------------------------------------------------------------------- #
# 5. Helper: read accessor data from binary chunk
# --------------------------------------------------------------------------- #
COMPONENT_SIZES = {
    5120: 1,  # BYTE
    5121: 1,  # UNSIGNED_BYTE
    5122: 2,  # SHORT
    5123: 2,  # UNSIGNED_SHORT
    5124: 4,  # INT
    5125: 4,  # UNSIGNED_INT
    5126: 4,  # FLOAT
}
TYPE_COMPONENTS = {
    "SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4,
    "MAT2": 4, "MAT3": 9, "MAT4": 16,
}
STRUCT_FMT = {
    5120: "b", 5121: "B", 5122: "h", 5123: "H",
    5124: "i", 5125: "I", 5126: "f",
}

def read_accessor(accessor_idx):
    acc = gltf["accessors"][accessor_idx]
    bv_idx = acc["bufferView"]
    bv = gltf["bufferViews"][bv_idx]

    comp_type = acc["componentType"]
    type_str  = acc["type"]
    count     = acc["count"]
    n_comp    = TYPE_COMPONENTS[type_str]
    comp_size = COMPONENT_SIZES[comp_type]
    fmt_char  = STRUCT_FMT[comp_type]

    bv_offset    = bv.get("byteOffset", 0)
    bv_byte_len  = bv["byteLength"]
    acc_offset   = acc.get("byteOffset", 0)
    byte_stride  = bv.get("byteStride", comp_size * n_comp)

    start = bv_offset + acc_offset
    results = []
    for i in range(count):
        off = start + i * byte_stride
        vals = struct.unpack_from(f"<{n_comp}{fmt_char}", bin_data, off)
        results.append(vals)
    return results, comp_type, type_str

# --------------------------------------------------------------------------- #
# 6. Analyse the FIRST body mesh primitive that has JOINTS_0 + WEIGHTS_0
# --------------------------------------------------------------------------- #
def analyze_bone(joint_slot, bone_name, joints_data, weights_data):
    total_verts = len(joints_data)
    refs = []          # (vertex_idx, slot_in_vec4, weight)
    for vi, (jvec, wvec) in enumerate(zip(joints_data, weights_data)):
        for slot in range(4):
            if jvec[slot] == joint_slot:
                refs.append((vi, slot, wvec[slot]))

    print(f"--- {bone_name} (joint slot {joint_slot}) ---")
    print(f"  Total vertices in mesh: {total_verts}")
    print(f"  Vertices referencing this bone: {len(refs)}")

    if not refs:
        print("  *** NO VERTICES REFERENCE THIS BONE ***")
        return

    weights = [r[2] for r in refs]
    above_01  = sum(1 for w in weights if w > 0.1)
    above_001 = sum(1 for w in weights if w > 0.01)
    near_zero = sum(1 for w in weights if w < 1e-6)
    w_min = min(weights)
    w_max = max(weights)
    w_avg = sum(weights) / len(weights)

    print(f"  Weight range: min={w_min:.6f}  max={w_max:.6f}  avg={w_avg:.6f}")
    print(f"  w > 0.1  (meaningful):  {above_01}")
    print(f"  w > 0.01 (weak):        {above_001}")
    print(f"  w ≈ 0    (near-zero):   {near_zero}")

    # Show first 10 samples
    print(f"  First 10 samples (vertex, slot, weight):")
    for r in refs[:10]:
        print(f"    vert {r[0]:6d}  slot {r[1]}  weight={r[2]:.6f}")


found = False
for ni, nd in skin_mesh_nodes:
    mesh_idx = nd["mesh"]
    mesh = gltf["meshes"][mesh_idx]
    mesh_name = mesh.get("name", f"mesh_{mesh_idx}")

    for prim_idx, prim in enumerate(mesh.get("primitives", [])):
        attrs = prim.get("attributes", {})
        if "JOINTS_0" not in attrs or "WEIGHTS_0" not in attrs:
            continue

        print(f"Analysing mesh '{mesh_name}' primitive {prim_idx} (node {ni})\n")

        joints_data, jct, _ = read_accessor(attrs["JOINTS_0"])
        weights_data, wct, _ = read_accessor(attrs["WEIGHTS_0"])

        print(f"JOINTS_0  componentType={jct} ({'UNSIGNED_BYTE' if jct==5121 else 'UNSIGNED_SHORT' if jct==5123 else str(jct)})")
        print(f"WEIGHTS_0 componentType={wct} ({'FLOAT' if wct==5126 else str(wct)})")
        print(f"Vertex count: {len(joints_data)}\n")

        analyze_bone(shoulder_l_slot, "Shoulder_L", joints_data, weights_data)
        print()
        analyze_bone(spine1m_slot, "Spine1_M", joints_data, weights_data)
        print()

        # Extra: distribution of all bones touched by this mesh
        from collections import Counter
        bone_counts = Counter()
        for jvec in joints_data:
            for jidx in jvec:
                bone_counts[jidx] += 1
        print("Top 20 most-referenced joints in this mesh:")
        for jidx, cnt in bone_counts.most_common(20):
            name = nodes[joints[jidx]].get("name", f"node_{joints[jidx]}")
            print(f"  joint slot {jidx:3d}  '{name}'  referenced {cnt} times")

        found = True
        break
    if found:
        break

if not found:
    print("ERROR: No primitive with JOINTS_0 + WEIGHTS_0 found in any skin mesh node.")
