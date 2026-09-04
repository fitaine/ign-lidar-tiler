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
    p.add_argument("--object", default=None,
                   help="Cloud object to replace. Omit to use the object tagged "
                        "by the Blender add-on (ign_lidar_scene custom property)")
    p.add_argument("--ply", required=True, help="Dense PLY to swap in")
    p.add_argument("--radius", type=float, required=True, help="Mesh to Points radius")
    p.add_argument("--out", required=True, help="Output .blend path")
    p.add_argument("--drop", action="append", default=[],
                   help="Delete this object before saving. Repeatable. Use it for "
                        "a superseded cloud still sitting in the scene.")
    p.add_argument("--strip-volumes", action="store_true",
                   help="Remove volume objects and unlink Volume sockets. Off by "
                        "default: volumetrics are part of the image. Use it to "
                        "measure what the point cloud alone costs.")
    return p.parse_args(argv)


def volume_sources(report_only=True):
    """Everything in the file that can make Cycles run volumetrics.

    Not just Volume objects: any node linked into a Volume socket on a material
    or world output triggers full volumetrics, even when cycles.volume_bounces
    reads 0 and no volume object exists. That is expensive and very noisy, and
    it would dominate a render-time measurement.
    """
    found = {"objects": [], "materials": [], "world": []}

    for ob in bpy.data.objects:
        if ob.type == "VOLUME":
            found["objects"].append(ob.name)

    def volume_linked(node_tree):
        if not node_tree:
            return False
        for nd in node_tree.nodes:
            if nd.bl_idname in ("ShaderNodeOutputMaterial", "ShaderNodeOutputWorld"):
                sock = nd.inputs.get("Volume")
                if sock is not None and sock.is_linked:
                    return True
        return False

    for mat in bpy.data.materials:
        if volume_linked(mat.node_tree):
            found["materials"].append(mat.name)

    for world in bpy.data.worlds:
        if volume_linked(world.node_tree):
            found["world"].append(world.name)

    return found


def strip_volumes():
    """Remove volumetrics so a render-time measurement reflects the cloud."""
    found = volume_sources()
    for name in found["objects"]:
        ob = bpy.data.objects.get(name)
        if ob:
            bpy.data.objects.remove(ob, do_unlink=True)
            print(f"[volume] removed volume object {name!r}", flush=True)

    for name in found["materials"] + found["world"]:
        tree = None
        mat = bpy.data.materials.get(name)
        if mat:
            tree = mat.node_tree
        else:
            world = bpy.data.worlds.get(name)
            tree = world.node_tree if world else None
        if not tree:
            continue
        for nd in tree.nodes:
            if nd.bl_idname in ("ShaderNodeOutputMaterial", "ShaderNodeOutputWorld"):
                sock = nd.inputs.get("Volume")
                if sock is not None and sock.is_linked:
                    for link in list(sock.links):
                        tree.links.remove(link)
                    print(f"[volume] unlinked Volume socket on {name!r}", flush=True)

    # Mesh objects that only existed to hold a volume shader are left in place:
    # removing them is a judgement call about the scene, not a mechanical fix.
    return found


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

    if a.object:
        old = bpy.data.objects.get(a.object)
        if old is None:
            names = [o.name for o in bpy.data.objects if o.type in ("MESH", "POINTCLOUD")]
            sys.exit(f"Object {a.object!r} not found. Candidates: {names}")
    else:
        tagged = [o for o in bpy.data.objects if o.get("ign_lidar_scene")]
        if len(tagged) > 1:
            sys.exit(f"several tagged objects, pass --object to choose: "
                     f"{[o.name for o in tagged]}")
        if tagged:
            old = tagged[0]
            print(f"[swap] using tagged object {old.name!r} "
                  f"(scene {old.get('ign_lidar_name')!r})", flush=True)
        else:
            # An adopted scene has no tag - it was adopted precisely because
            # nothing linked it to a manifest - and requiring one here made the
            # last step of the run fail after the dense cloud had been built.
            # extract_outline.py and export_height_grid.py already fall back to
            # the only large cloud; this is the same rule, and it still refuses
            # when there is a choice to be made, because this one REPLACES the
            # object it picks.
            clouds = [o for o in bpy.data.objects
                      if o.type in ("MESH", "POINTCLOUD")
                      and len(o.data.vertices if o.type == "MESH"
                              else o.data.points) > 100_000]
            if len(clouds) == 1:
                old = clouds[0]
                print(f"[swap] no tag here; using the only large cloud, "
                      f"{old.name!r}", flush=True)
            elif not clouds:
                sys.exit("no cloud in this file has more than 100k points; "
                         "name one with --object")
            else:
                sys.exit(f"several clouds and none tagged, pass --object to "
                         f"choose: {[o.name for o in clouds]}")

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
        scale = bpy.context.scene.unit_settings.scale_length
        print(f"[swap] radius RAW {sock.default_value:.6f} -> {a.radius:.6f} "
              f"in {group.name!r}/{node.name!r}", flush=True)
        print(f"[swap] scene scale_length={scale:g}, so the panel will show "
              f"{a.radius * scale:.6f}", flush=True)
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

    for name in a.drop:
        ob = bpy.data.objects.get(name)
        if ob is None:
            print(f"[swap] --drop {name!r}: no such object", flush=True)
            continue
        n = len(ob.data.vertices) if ob.type == "MESH" else 0
        me = ob.data if ob.type == "MESH" else None
        bpy.data.objects.remove(ob, do_unlink=True)
        if me is not None and me.users == 0:
            bpy.data.meshes.remove(me)
        print(f"[swap] dropped {name!r} ({n:,} verts)", flush=True)

    # Another heavy cloud left in the file is added VRAM at render time, and
    # nothing downstream would tell you why the render is slower than expected.
    leftovers = [(o.name, len(o.data.vertices)) for o in bpy.data.objects
                 if o.type == "MESH" and o is not new and len(o.data.vertices) > 1_000_000]
    if leftovers:
        total = sum(n for _, n in leftovers)
        print(f"[swap] WARNING {len(leftovers)} other heavy mesh(es) remain, "
              f"{total:,} verts in total:", flush=True)
        for nm, n in leftovers:
            print(f"[swap]   {nm!r}  {n:,}", flush=True)
        print(f"[swap]   they will cost VRAM in the render. Use --drop to remove.",
              flush=True)

    found = volume_sources()
    n_vol = sum(len(v) for v in found.values())
    if n_vol:
        print(f"[volume] FOUND volumetrics: objects={found['objects']} "
              f"materials={found['materials']} world={found['world']}", flush=True)
        if a.strip_volumes:
            strip_volumes()
            bpy.context.scene.cycles.volume_bounces = 0
            print("[volume] volume_bounces -> 0", flush=True)
        else:
            print("[volume] left in place, as part of the image. They do cost "
                  "render time and VRAM alongside the cloud, so if a render is "
                  "unexpectedly slow this is the first thing to check. "
                  "--strip-volumes removes them.", flush=True)
    else:
        print("[volume] none found", flush=True)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(out), compress=False)
    print(f"[swap] wrote {out}  ({out.stat().st_size/1e9:.2f} GB)", flush=True)
    print("[swap] HEADLESS ONLY - do not open this file in the GUI", flush=True)


if __name__ == "__main__":
    main()
