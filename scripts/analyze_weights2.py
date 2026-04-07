#!/usr/bin/env python3
"""
Analyze vertex weight data for Shoulder_L and Spine1_M in ALL body-related meshes
from klukai.glb, not just the first one.
"""

import struct
import json
import sys
from collections import Counter

GLB_PATH = "/home/jalsarraf/git/companion/web-build/assets/models/klukai.glb"

# --------------------------------------------------------------------------- #
# Parse GLB
# --------------------------------------------------------------------------- #
with open(GLB_PATH, "rb") as f:
    raw = f.read()

magic, version, length = struct.unpack_from("<III", raw, 0)
json_chunk_len, json_chunk_type = struct.unpack_from("<II", raw, 12)
json_bytes = raw[20 : 20 + json_chunk_len]
gltf = json.loads(json_bytes.decode("utf-8"))

bin_offset = 20 + json_chunk_len
bin_chunk_len, bin_chunk_type = struct.unpack_from("<II", raw, bin_offset)
bin_data = raw[bin_offset + 8 : bin_offset + 8 + bin_chunk_len]

nodes = gltf["nodes"]
skins = gltf["skins"]
meshes = gltf["meshes"]

# Find Klukai skin
target_skin_idx = next(i for i, s in enumerate(skins) if s.get("name") == "Klukai")
target_skin = skins[target_skin_idx]
joints = target_skin["joints"]

def find_joint_slot(bone_name):
    for slot, nidx in enumerate(joints):
        if nodes[nidx].get("name") == bone_name:
            return slot
    return None

shoulder_l_slot = find_joint_slot("Shoulder_L")
spine1m_slot    = find_joint_slot("Spine1_M")
print(f"Shoulder_L → joint slot {shoulder_l_slot}")
print(f"Spine1_M   → joint slot {spine1m_slot}\n")

# --------------------------------------------------------------------------- #
# Accessor helper
# --------------------------------------------------------------------------- #
TYPE_COMPONENTS = {"SCALAR":1,"VEC2":2,"VEC3":3,"VEC4":4,"MAT2":4,"MAT3":9,"MAT4":16}
STRUCT_FMT = {5120:"b",5121:"B",5122:"h",5123:"H",5124:"i",5125:"I",5126:"f"}
COMP_SIZE  = {5120:1,5121:1,5122:2,5123:2,5124:4,5125:4,5126:4}

def read_accessor(acc_idx):
    acc = gltf["accessors"][acc_idx]
    bv  = gltf["bufferViews"][acc["bufferView"]]
    ct  = acc["componentType"]
    nc  = TYPE_COMPONENTS[acc["type"]]
    cnt = acc["count"]
    stride = bv.get("byteStride", COMP_SIZE[ct] * nc)
    start  = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    fmt    = f"<{nc}{STRUCT_FMT[ct]}"
    return [struct.unpack_from(fmt, bin_data, start + i*stride) for i in range(cnt)]

# --------------------------------------------------------------------------- #
# Collect every mesh node that has JOINTS_0
# --------------------------------------------------------------------------- #
skin_mesh_nodes = [
    (i, n) for i, n in enumerate(nodes)
    if n.get("skin") == target_skin_idx and "mesh" in n
]

# Keyword lists to identify body meshes
BODY_KEYWORDS = ["body", "slg_body", "slg_cloth", "slg_face", "slg_hair", "slg_skin"]

def is_body_mesh(mesh_name, node_name):
    nl = node_name.lower()
    ml = mesh_name.lower()
    return (
        "body" in nl or "suit" in nl or "skin" in nl or
        "slg_body" in ml or "slg_cloth" in ml
    )

def analyze_bone(slot, name, jd, wd):
    refs = [(vi, s, wd[vi][s]) for vi in range(len(jd)) for s in range(4) if jd[vi][s] == slot]
    if not refs:
        return f"  {name}: 0 verts *** NOT REFERENCED ***"
    ws = [r[2] for r in refs]
    lines = [
        f"  {name} (slot {slot}): {len(refs)} verts",
        f"    weight min={min(ws):.6f}  max={max(ws):.6f}  avg={sum(ws)/len(ws):.6f}",
        f"    w>0.1={sum(1 for w in ws if w>0.1)}  w>0.01={sum(1 for w in ws if w>0.01)}  w≈0={sum(1 for w in ws if w<1e-6)}",
        f"    first 5: {[(r[0], f'{r[2]:.4f}') for r in refs[:5]]}",
    ]
    return "\n".join(lines)

# --------------------------------------------------------------------------- #
# Main loop — check every skin mesh node, report body-related ones
# --------------------------------------------------------------------------- #
print(f"Scanning {len(skin_mesh_nodes)} mesh nodes...\n")
print("="*70)

body_meshes_found = 0
shoulder_total_refs = 0

for ni, nd in skin_mesh_nodes:
    mesh_idx  = nd["mesh"]
    mesh      = meshes[mesh_idx]
    mesh_name = mesh.get("name", f"mesh_{mesh_idx}")
    node_name = nd.get("name", f"node_{ni}")

    for pidx, prim in enumerate(mesh.get("primitives", [])):
        attrs = prim.get("attributes", {})
        if "JOINTS_0" not in attrs or "WEIGHTS_0" not in attrs:
            continue

        jd = read_accessor(attrs["JOINTS_0"])
        wd = read_accessor(attrs["WEIGHTS_0"])
        n_verts = len(jd)

        # Count Shoulder_L refs
        shl_refs = sum(1 for jvec in jd for s in range(4) if jvec[s] == shoulder_l_slot)
        shl_meaningful = 0
        for vi, jvec in enumerate(jd):
            for s in range(4):
                if jvec[s] == shoulder_l_slot and wd[vi][s] > 0.1:
                    shl_meaningful += 1

        shoulder_total_refs += shl_refs

        # Only print detail for body/suit meshes, or any mesh that references Shoulder_L
        if is_body_mesh(mesh_name, node_name) or shl_refs > 0:
            body_meshes_found += 1
            print(f"NODE '{node_name}' | MESH '{mesh_name}' | prim {pidx} | {n_verts} verts")
            print(analyze_bone(shoulder_l_slot, "Shoulder_L", jd, wd))
            print(analyze_bone(spine1m_slot,    "Spine1_M",   jd, wd))

            # Top joints
            cnt = Counter(jvec[s] for jvec in jd for s in range(4))
            top = cnt.most_common(10)
            top_str = "  Top joints: " + "  ".join(
                f"slot{j}({nodes[joints[j]].get('name','?')})={c}" for j, c in top
            )
            print(top_str)
            print("-"*70)

print("="*70)
print(f"\nSummary:")
print(f"  Body-related or Shoulder_L-referencing meshes examined: {body_meshes_found}")
print(f"  Total vertex-slot references to Shoulder_L across ALL skin meshes: {shoulder_total_refs}")

# --------------------------------------------------------------------------- #
# Also: scan ALL meshes and accumulate Shoulder_L references
# --------------------------------------------------------------------------- #
print("\n--- Full scan: Shoulder_L references per mesh node (non-zero only) ---")
for ni, nd in skin_mesh_nodes:
    mesh_idx  = nd["mesh"]
    mesh      = meshes[mesh_idx]
    mesh_name = mesh.get("name", f"mesh_{mesh_idx}")
    node_name = nd.get("name", f"node_{ni}")
    for pidx, prim in enumerate(mesh.get("primitives", [])):
        attrs = prim.get("attributes", {})
        if "JOINTS_0" not in attrs or "WEIGHTS_0" not in attrs:
            continue
        jd = read_accessor(attrs["JOINTS_0"])
        wd = read_accessor(attrs["WEIGHTS_0"])
        refs = [(vi, s, wd[vi][s]) for vi in range(len(jd)) for s in range(4) if jd[vi][s] == shoulder_l_slot]
        if refs:
            ws = [r[2] for r in refs]
            print(f"  '{node_name}': {len(refs)} refs  max_w={max(ws):.4f}  min_w={min(ws):.4f}")
