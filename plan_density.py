"""Decide whether to downsample at all, by counting the source over the carve.

Tiphaine's rule, and it is the right one:

    what area survived the carve?
    how many source points are in that same area?
      fewer than the budget  -> do not downsample, take every return
      more than the budget   -> downsample to the budget

The old order was backwards. It solved a voxel for the budget over the WHOLE
footprint, built that, then cropped to the carve - so the count was decided
before anyone asked how much ground was being kept, and the answer came out
below budget with holes in it. Downsampling only makes sense once you know the
source has more points than you need.

The count is a real count, not area x average density: PDAL crops each tile to
the polygon and reports how many points survive, without writing anything.

    python plan_density.py --tiles ./tiles --polygon carve.geojson \
        --target 150000000
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from densify_tiled import polygon_wkt

PDAL_EXE = r"C:\Program Files\QGIS 3.40.5\bin\pdal.exe"


def load_polygon(path):
    gj = json.loads(Path(path).read_text(encoding="utf-8"))
    geom = gj["geometry"] if gj.get("type") == "Feature" else gj
    if geom["type"] != "Polygon":
        sys.exit("--polygon needs a single Polygon")
    return polygon_wkt(geom["coordinates"][0])


def count_in_polygon(tile, wkt, workdir):
    """Points of one tile inside the polygon, counted without writing a file."""
    pipe = Path(workdir) / f"count_{tile.stem}.json"
    meta = Path(workdir) / f"count_{tile.stem}_meta.json"
    pipe.write_text(json.dumps({"pipeline": [
        {"type": "readers.copc", "filename": str(tile)},
        {"type": "filters.crop", "polygon": wkt},
        {"type": "filters.stats", "dimensions": "X"},
        {"type": "writers.null"},
    ]}), encoding="utf-8")
    r = subprocess.run([PDAL_EXE, "pipeline", str(pipe), "--metadata", str(meta)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"pdal failed counting {tile.name}: {r.stderr[:400]}")
    md = json.loads(meta.read_text(encoding="utf-8"))

    def find_count(node):
        if isinstance(node, dict):
            if "statistic" in node and isinstance(node["statistic"], list):
                for s in node["statistic"]:
                    if s.get("name") == "X":
                        return int(s.get("count", 0))
            for v in node.values():
                got = find_count(v)
                if got is not None:
                    return got
        elif isinstance(node, list):
            for v in node:
                got = find_count(v)
                if got is not None:
                    return got
        return None

    return find_count(md) or 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tiles", required=True, help="Folder of .copc.laz tiles")
    p.add_argument("--polygon", required=True, help="carve.geojson from mask_to_polygon.py")
    p.add_argument("--target", type=int, default=150_000_000,
                   help="Point budget, i.e. what the card can render (default 150M)")
    a = p.parse_args()

    tiles = sorted(Path(a.tiles).glob("*.copc.laz"))
    if not tiles:
        sys.exit(f"no .copc.laz in {a.tiles}")
    wkt = load_polygon(a.polygon)

    total = 0
    with tempfile.TemporaryDirectory() as wd:
        for i, t in enumerate(tiles, 1):
            n = count_in_polygon(t, wkt, wd)
            total += n
            print(f"  [{i}/{len(tiles)}] {t.name}: {n:,}", flush=True)

    print(f"\nsource points inside the carve: {total:,}")
    print(f"budget:                          {a.target:,}")
    if total <= a.target:
        print(f"\nUNDER BUDGET by {a.target - total:,}. Do not downsample:")
        print(f"  densify_tiled.py --voxel 0 --polygon <carve.geojson> ...")
        print(f"  Every return is kept, and the radius comes from the measured "
              f"spacing rather than from a voxel.")
        return 0
    print(f"\nOVER BUDGET by {total - a.target:,}. Downsample to the budget:")
    print(f"  solve_voxel.py --tiles <tiles> --target {a.target} "
          f"--coverage {a.target/total:.6f}")
    print(f"  then densify_tiled.py with that voxel and the same polygon.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
