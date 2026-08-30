"""Build a headless-only render .blend by swapping in a denser point cloud.

Stage 3b of the IGN LiDAR Tiler (see PLAN.md). Run it through Blender:

    blender -b "scene.blend" --python make_render_blend.py -- \
        --object mont-aiguille-035 \
        --ply  ".../mont-aiguille-dense-032.ply" \
        --radius 0.65 \
        --out  ".../mont-aiguille-dense-HEADLESS.blend"

The lighting, cameras, world and render settings are untouched. Only the cloud
object's geometry is replaced, and the ball radius is set on the Mesh to Points
node inside the object's geometry node group.

The result is NOT meant to be opened in the GUI: the whole point is that the
viewport cannot cope with the point counts that Cycles renders happily.
"""

import argparse
import sys
from pathlib import Path

import bpy


def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--object", required=True, help="Name of the cloud object to replace")
    p.add_argument("--ply", required=True, help="Dense PLY to swap in")
    p.add_argument("--radius", type=float, required=True, help="Mesh to Points radius")
    p.add_argument("--out", required=True, help="Output .blend path")
    return p.parse_args(argv)


def find_mesh_to_points(node_group):
    """Find the Mesh to Points node, following nested groups."""
    for nd in node_group.nodes:
        if nd.bl_idname == "GeometryNodeMeshToPoints":
            return node_group, nd
        if nd.bl_idname == "GeometryNodeGroup" and nd.node_group:
            found = find_mesh_to_points(nd.node_group)
            if found[1]:
                return found
    return node_group, None


def main():
    a = parse_args()
    ply = Path(a.ply)
    if not ply.is_file():
        sys.exit(f"PLY not found: {ply}")

    old = bpy.data.objects.get(a.object)
    if old is None:
        names = [o.name for o in bpy.data.objects if o.type in ("MESH", "POINTCLOUD")]
        sys.exit(f"Object {a.object!r} not found. Candidates: {names}")

    old_matrix = old.matrix_world.copy()
    old_materials = [m for m in old.data.materials]
    old_mods = [(m.name, m.type, getattr(m, "node_group", None)) for m in old.modifiers]
    old_collections = list(old.users_collection)
    old_props = {k: old[k] for k in old.keys() if not k.startswith("_")}
    old_count = len(old.data.vertices)
    print(f"[swap] replacing {old.name!r} ({old_count:,} verts)", flush=True)
    print(f"[swap] matrix_world translation = {tuple(round(v,4) for v in old_matrix.translation)}", flush=True)

    before = set(bpy.data.objects.keys())
    bpy.ops.wm.ply_import(filepath=str(ply))
    new_names = set(bpy.data.objects.keys()) - before
    if len(new_names) != 1:
        sys.exit(f"expected one imported object, got {new_names}")
    new = bpy.data.objects[new_names.pop()]
    print(f"[swap] imported {new.name!r} ({len(new.data.vertices):,} verts)", flush=True)

    # Colour attribute must stay FLOAT_COLOR: the material samples 'Col'.
    attrs = {at.name: at.data_type for at in new.data.attributes}
    print(f"[swap] imported attributes: {attrs}", flush=True)

    # Put the new object where the old one lived, in every sense.
    new.matrix_world = old_matrix
    new.data.materials.clear()
    for m in old_materials:
        new.data.materials.append(m)
    for k, v in old_props.items():
        try:
            new[k] = v
        except Exception:
            pass

    for name, mtype, ng in old_mods:
        mod = new.modifiers.new(name=name, type=mtype)
        if ng is not None:
            mod.node_group = ng

    for coll in old_collections:
        if new.name not in coll.objects:
            coll.objects.link(new)
    for coll in list(new.users_collection):
        if coll not in old_collections:
            coll.objects.unlink(new)

    # Set the radius. The node group is shared data, but the only other user is
    # the object we are about to delete.
    radius_set = False
    for mod in new.modifiers:
        ng = getattr(mod, "node_group", None)
        if not ng:
            continue
        group, node = find_mesh_to_points(ng)
        if node is None:
            continue
        sock = node.inputs["Radius"]
        if sock.is_linked:
            print(f"[swap] WARNING Radius on {group.name!r} is linked, not setting it", flush=True)
            continue
        print(f"[swap] radius {sock.default_value:.4f} -> {a.radius:.4f} "
              f"in {group.name!r}/{node.name!r}", flush=True)
        sock.default_value = a.radius
        radius_set = True
    if not radius_set:
        sys.exit("could not set the radius: no unlinked Mesh to Points node found")

    # Drop the old cloud so the file does not carry both.
    old_mesh = old.data
    old_name = old.name
    bpy.data.objects.remove(old, do_unlink=True)
    bpy.data.meshes.remove(old_mesh)
    new.name = old_name
    new.data.name = old_name
    print(f"[swap] removed the old cloud, renamed the new one to {old_name!r}", flush=True)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(out), compress=False)
    print(f"[swap] wrote {out}  ({out.stat().st_size/1e9:.2f} GB)", flush=True)
    print("[swap] HEADLESS ONLY - do not open this file in the GUI", flush=True)


if __name__ == "__main__":
    main()
