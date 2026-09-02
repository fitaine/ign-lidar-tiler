"""Export a ground-height raster from a point cloud in a .blend.

Used by locate_scene.py to georeference a scene whose origin was never
recorded. Run it through Blender:

    blender -b scene.blend --python export_height_grid.py -- \
        --object "cloud" --cell 5.0 --out grid.npz

Takes the MINIMUM Z per cell, not the maximum: the lowest return in a cell is
the ground, which is what IGN's MNT holds. Using the maximum would compare
treetops against bare earth and bias every cell by the canopy height.
"""

import argparse
import sys

import numpy as np
import bpy


def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--object", default=None,
                   help="Cloud object; omit to use the only large one")
    p.add_argument("--cell", type=float, default=5.0, help="Cell size in metres")
    p.add_argument("--out", required=True)
    return p.parse_args(argv)


def main():
    a = parse_args()
    if a.object:
        ob = bpy.data.objects.get(a.object)
        if ob is None:
            names = [o.name for o in bpy.data.objects
                     if o.type in ("MESH", "POINTCLOUD")]
            sys.exit(f"Object {a.object!r} not found. Candidates: {names}")
    else:
        # A scene that predates the app has no tag - that is the whole reason
        # it is being located - so falling back to the tag alone made "leave it
        # empty" impossible for exactly the files this is for. Fall back to the
        # only large cloud, the way extract_outline.py does, and only ask when
        # the file genuinely holds more than one.
        tagged = [o for o in bpy.data.objects if o.get("ign_lidar_scene")]
        if len(tagged) == 1:
            ob = tagged[0]
        else:
            clouds = [o for o in bpy.data.objects
                      if o.type in ("MESH", "POINTCLOUD")
                      and len(o.data.vertices if o.type == "MESH"
                              else o.data.points) > 100_000]
            if len(clouds) == 1:
                ob = clouds[0]
                print(f"[grid] using the only large cloud, {ob.name!r}", flush=True)
            elif not clouds:
                sys.exit("no cloud in this file has more than 100k points; "
                         "name one with --object")
            else:
                sys.exit(f"several clouds here, name one with --object: "
                         f"{[o.name for o in clouds]}")

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
    cell = a.cell
    idx = np.floor((co[:, :2] - mn[:2]) / cell).astype(np.int64)
    w = int(idx[:, 0].max()) + 1
    h = int(idx[:, 1].max()) + 1
    flat = idx[:, 1] * w + idx[:, 0]

    # minimum Z per cell = ground
    grid = np.full(w * h, np.inf)
    np.minimum.at(grid, flat, co[:, 2])
    grid = grid.reshape(h, w)
    mask = np.isfinite(grid)
    grid[~mask] = np.nan

    np.savez_compressed(a.out, grid=grid, mask=mask, cell=np.float64(cell),
                        local_min=mn, points=np.int64(n),
                        object_name=np.array(ob.name),
                        matrix_world=np.array(ob.matrix_world))
    print(f"[grid] object {ob.name!r}, {n:,} points", flush=True)
    print(f"[grid] local bbox min {mn}", flush=True)
    print(f"[grid] raster {w} x {h} at {cell} m, "
          f"{100*mask.mean():.1f}% of cells occupied", flush=True)
    zs = grid[mask]
    print(f"[grid] ground Z {zs.min():.2f} .. {zs.max():.2f} "
          f"(mean {zs.mean():.2f})", flush=True)
    print(f"[grid] wrote {a.out}", flush=True)


if __name__ == "__main__":
    main()
