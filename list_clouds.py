"""List the point clouds inside a .blend, as one line of JSON.

So the app can offer them in a menu instead of asking anyone to type an object
name they would have to open Blender to read. Nothing is modified: the file is
opened read-only and the process exits.

    blender -b scene.blend --python list_clouds.py -- --min-points 100000

Prints a single line starting with CLOUDS= so the caller can find it among
Blender's startup chatter, which no amount of quiet flags fully silences.
"""
import argparse
import json
import sys

import bpy

MARKER = "CLOUDS="


def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--min-points", type=int, default=100_000,
                   help="Ignore objects smaller than this (default 100k)")
    return p.parse_args(argv)


def main():
    a = parse_args()
    out = []
    for ob in bpy.data.objects:
        if ob.type not in ("MESH", "POINTCLOUD"):
            continue
        data = ob.data
        n = len(data.vertices) if ob.type == "MESH" else len(data.points)
        if n < a.min_points:
            continue
        entry = {"name": ob.name, "points": n, "type": ob.type,
                 "tagged": bool(ob.get("ign_lidar_scene"))}
        try:
            import numpy as np
            co = np.empty(n * 3, dtype=np.float32)
            (data.vertices if ob.type == "MESH" else data.points).foreach_get(
                "co" if ob.type == "MESH" else "position", co)
            co = co.reshape(n, 3)
            size = (co.max(0) - co.min(0)).tolist()
            entry["size"] = [round(v, 1) for v in size]
        except Exception:
            pass
        out.append(entry)
    out.sort(key=lambda e: -e["points"])
    print(MARKER + json.dumps(out), flush=True)


if __name__ == "__main__":
    main()
