"""Export an occupancy mask from a carved point cloud in a .blend.

Stage 3a of the IGN LiDAR Tiler (see PLAN.md). Run it through Blender:

    blender -b "scene.blend" --python extract_mask.py -- \
        --object mont-aiguille-035 --cell 3.0 --out mask.npz

Tiphaine carves the sparse cloud in the GUI for framing and performance, often
keeping only a few percent of the downloaded footprint. The dense variant has
to reproduce that carving, and the shape can be arbitrary: interior holes,
vertical cuts, ragged edges. Rather than ask anyone to describe it, we
voxelize the surviving vertices into a coarse grid and keep the dense points
whose cell is occupied.

The mask is built from the mesh's LOCAL vertex coordinates, which are exactly
the PLY's coordinates. So the dense PLY must be produced with the SAME
--origin as the sparse one, and then no transform is needed at all. The origin
is recorded here so crop_to_mask.py can refuse a mismatch.
"""

import argparse
import sys

import numpy as np
import bpy


def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--object", required=True, help="Carved cloud object")
    p.add_argument("--cell", type=float, default=3.0,
                   help="Mask cell size in metres (default 3.0)")
    p.add_argument("--origin", default=None,
                   help="X,Y,Z the source PLY was recentred by, recorded for checking")
    p.add_argument("--out", required=True, help="Output .npz")
    return p.parse_args(argv)


def main():
    a = parse_args()
    ob = bpy.data.objects.get(a.object)
    if ob is None:
        names = [o.name for o in bpy.data.objects if o.type in ("MESH", "POINTCLOUD")]
        sys.exit(f"Object {a.object!r} not found. Candidates: {names}")

    if ob.type == "MESH":
        n = len(ob.data.vertices)
        co = np.empty(n * 3, dtype=np.float32)
        ob.data.vertices.foreach_get("co", co)
    else:
        n = len(ob.data.points)
        co = np.empty(n * 3, dtype=np.float32)
        ob.data.points.foreach_get("position", co)
    co = co.reshape(n, 3).astype(np.float64)

    mn = co.min(0)
    mx = co.max(0)
    cell = a.cell
    dims = np.floor((mx - mn) / cell).astype(np.int64) + 1
    idx = np.floor((co - mn) / cell).astype(np.int64)
    keys = (idx[:, 0] * dims[1] + idx[:, 1]) * dims[2] + idx[:, 2]
    keys = np.unique(keys)

    # How much of the bounding box actually survives: the tell for how much
    # work the crop is going to save.
    filled = keys.size / float(dims.prod())
    planim = np.unique(idx[:, 0] * dims[1] + idx[:, 1]).size
    planim_frac = planim / float(dims[0] * dims[1])

    # The cell must be comfortably coarser than the sparse cloud's own point
    # spacing. If it is not, cells inside the kept region come up empty by
    # chance and the mask punches holes in itself, quietly dropping dense
    # points that should have survived.
    spacing = float(np.sqrt((dims[0] * dims[1] * cell * cell) * planim_frac / max(n, 1)))
    ratio = cell / spacing if spacing > 0 else float("inf")
    print(f"[mask] sparse spacing ~{spacing:.3f} m, cell/spacing = {ratio:.1f}x", flush=True)
    if ratio < 4.0:
        print(f"[mask] WARNING cell {cell} m is only {ratio:.1f}x the point spacing. "
              f"The mask will be pitted and will drop points it should keep. "
              f"Use --cell {max(4.0*spacing, cell):.1f} or larger.", flush=True)

    origin = None
    if a.origin:
        origin = np.array([float(v) for v in a.origin.split(",")], dtype=np.float64)

    np.savez_compressed(
        a.out,
        keys=keys, mn=mn, dims=dims, cell=np.float64(cell),
        origin=origin if origin is not None else np.array([np.nan] * 3),
        object_name=np.array(a.object),
        matrix_world=np.array(ob.matrix_world),
        source_points=np.int64(n),
    )

    print(f"[mask] object      : {ob.name!r}  {n:,} verts", flush=True)
    print(f"[mask] local bbox  : {mn} .. {mx}", flush=True)
    print(f"[mask] cell        : {cell} m   grid {tuple(dims)}", flush=True)
    print(f"[mask] occupied    : {keys.size:,} cells, {100*filled:.2f}% of the volume", flush=True)
    print(f"[mask] planimetric : {planim:,} cells, {100*planim_frac:.1f}% of the footprint", flush=True)
    print(f"[mask] object translation: {tuple(round(v,4) for v in ob.matrix_world.translation)}", flush=True)
    print(f"[mask] wrote {a.out}", flush=True)


if __name__ == "__main__":
    main()
