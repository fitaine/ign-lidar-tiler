"""Export the footprint of the carved proxy from a .blend, for PDAL to crop with.

Replaces extract_mask.py in the dense workflow. The mask was a 3D occupancy
grid, and cropping the dense cloud against it deleted points that belonged: a
dense point survived only where a sparse proxy point sat in the same 3 m cell,
so on flat ground — where the proxy has one point every couple of metres and
the Z of a dense point can fall into the neighbouring cell — it punched holes.

The footprint carries the same intent without that damage. The proxy's outline
is the shape; PDAL cuts the source tiles to it; every return inside is kept.
Islands stay separate islands, holes cut in the middle stay holes.

    blender -b "scene.blend" --python extract_outline.py -- \
        --origin 987002.47,6497720.55,1639.35 --out carve.geojson

The cell size is a rasterising step, not a filter: it decides how finely the
outline follows the carve, nothing about which points survive.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import outline as ol


def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--object", default=None,
                   help="Carved proxy. Omit to use the object tagged by the add-on")
    p.add_argument("--cell", type=float, default=4.0,
                   help="Rasterising step in metres (default 4.0). Larger follows "
                        "the carve more loosely; it never removes points")
    p.add_argument("--min-area", type=float, default=1000.0,
                   help="Drop islands smaller than this many square metres. The "
                        "default keeps anything above roughly 30x30 m, which is "
                        "far below a real island like Avoriaz's background and "
                        "far above the scraps a carve leaves behind")
    p.add_argument("--min-hole-area", type=float, default=64.0,
                   help="Ignore holes smaller than this many square metres: "
                        "below that it is the proxy's own spacing showing "
                        "through, not something you cut (default 64, an 8x8 m "
                        "hole survives)")
    p.add_argument("--origin", required=True,
                   help="X,Y,Z the proxy was recentred by, to return to Lambert 93")
    p.add_argument("--out", required=True, help="Output .geojson")
    return p.parse_args(argv)


def pick_object(name):
    if name:
        ob = bpy.data.objects.get(name)
        if ob is None:
            names = [o.name for o in bpy.data.objects
                     if o.type in ("MESH", "POINTCLOUD")]
            sys.exit(f"Object {name!r} not found. Candidates: {names}")
        return ob
    tagged = [o for o in bpy.data.objects if o.get("ign_lidar_scene")]
    if len(tagged) == 1:
        print(f"[outline] using tagged object {tagged[0].name!r}", flush=True)
        return tagged[0]
    if not tagged:
        clouds = [o for o in bpy.data.objects if o.type in ("MESH", "POINTCLOUD")
                  and len(o.data.vertices if o.type == "MESH" else o.data.points) > 100_000]
        if len(clouds) == 1:
            print(f"[outline] using the only large cloud, {clouds[0].name!r}", flush=True)
            return clouds[0]
        sys.exit("pass --object, or tag the proxy with the IGN LiDAR Tiler add-on; "
                 f"candidates: {[o.name for o in clouds]}")
    sys.exit(f"several tagged objects, pass --object: {[o.name for o in tagged]}")


def local_xy(ob):
    """Proxy points in the PLY's own coordinates - WITHOUT the object transform.

    The origin maps the PLY's coordinates to Lambert 93, so a footprint has to
    be built in those same coordinates. Applying matrix_world instead shifts it
    by however far the object was moved in the scene, and the crop then keeps
    the wrong ground: Montvernier's cloud is moved by (107, 236) m, and its
    footprint was cut 107 m east and 236 m north of the terrain it describes -
    236 m of the framing missing at one end, 240 m of empty ground at the other.

    Moving the object does not move the ground it stands for. The dense cloud
    is given the same transform when it is swapped in (make_render_blend.py),
    so ignoring it here is what keeps the two aligned.
    """
    data = ob.data
    if ob.type == "POINTCLOUD":
        n = len(data.points)
        co = np.empty(n * 3, dtype=np.float32)
        data.points.foreach_get("position", co)
    else:
        n = len(data.vertices)
        co = np.empty(n * 3, dtype=np.float32)
        data.vertices.foreach_get("co", co)
    co = co.reshape(n, 3).astype(np.float64)

    t = tuple(round(v, 3) for v in ob.matrix_world.translation)
    if any(abs(v) > 1e-6 for v in t):
        print(f"[outline] note: {ob.name!r} is moved by {t} in the scene. The "
              f"footprint follows the ground, not the placement; the dense "
              f"cloud inherits the same transform.", flush=True)
    return co[:, :2], n


def main():
    a = parse_args()
    ob = pick_object(a.object)
    xy, n = local_xy(ob)
    print(f"[outline] {ob.name!r}: {n:,} points", flush=True)

    grid, grid_origin = ol.occupancy(xy, a.cell)
    print(f"[outline] rasterised at {a.cell} m: {int(grid.sum()):,} cells", flush=True)

    cell_area = a.cell * a.cell
    min_cells = max(1.0, a.min_area / cell_area)
    loops = ol.rings(grid)
    groups = ol.group(loops, min_cells=min_cells,
                      min_hole_cells=max(1.0, a.min_hole_area / cell_area))
    if not groups:
        sys.exit("no island survived; lower --min-area")

    # Say what was thrown away and where. A fragment is usually a scrap of
    # proxy a carve left behind, but whether it was meant is the artist's call,
    # and "3 islands" for one carve plus two 200 m2 scraps is a useless report.
    scraps = ol.dropped_fragments(loops, a.cell, min_cells)
    if scraps:
        main = max(groups, key=lambda g: abs(ol.signed_area(g[0])))[0]
        cx = sum(p[0] for p in main) / len(main) * a.cell
        cy = sum(p[1] for p in main) / len(main) * a.cell
        print(f"[outline] dropped {len(scraps)} fragment(s) under "
              f"{a.min_area:,.0f} m2:", flush=True)
        for d in scraps[:5]:
            km = ((d["at"][0] - cx) ** 2 + (d["at"][1] - cy) ** 2) ** 0.5 / 1000
            print(f"    {ol.area_str(d['area'])}, {d['size'][0]:.0f} x "
                  f"{d['size'][1]:.0f} m, {km:.2f} km from the main shape",
                  flush=True)
        if len(scraps) > 5:
            print(f"    and {len(scraps) - 5} more", flush=True)

    ox, oy, _ = (float(v) for v in a.origin.split(","))
    gj = ol.to_geojson(groups, grid_origin, a.cell, (ox, oy))
    Path(a.out).write_text(json.dumps(gj), encoding="utf-8")

    total = sum(abs(ol.signed_area(g[0])) for g in groups) * a.cell * a.cell
    holes = sum(abs(ol.signed_area(h)) for g in groups for h in g[1:]) * a.cell * a.cell
    print(f"[outline] {len(groups)} island(s), {total/1e6:.2f} km2 "
          f"less {holes/1e6:.2f} km2 of holes", flush=True)
    print(ol.describe(groups, a.cell), flush=True)
    print(f"[outline] wrote {a.out}", flush=True)


if __name__ == "__main__":
    main()
