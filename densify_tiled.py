"""Build a dense PLY tile by tile, so memory never scales with the scene.

`densify.py` merges every tile before downsampling, so its peak memory is the
whole raw cloud: 40 GB for Mont Aiguille's 565M points, and it held 51 GB for
twenty minutes afterwards. Aravis (1.18 billion) and Nantua (1.33 billion)
would not run at all.

This processes one tile at a time and concatenates the results, so peak memory
is one tile's worth. A binary PLY is a header followed by fixed-size records,
so concatenating bodies and rewriting the count is exact, not an approximation.

    python densify_tiled.py --tiles ./tiles --raster ortho.tif \
        --voxel 0.49 --origin 900000,6418000,1052.3 --name scene --out ./out

## The grid alignment problem, and the fix

PDAL's `filters.voxeldownsize` anchors its voxel grid on the FIRST point it
sees. Measured 2026-08-31: downsampling two adjacent Mont Aiguille tiles
separately put one tile's output entirely at X = 0.12 mod 1.0 and the other's
at 0.39 — two different lattices, which would leave a mismatched seam at every
tile boundary.

The fix exploits the same behaviour: a synthetic point at a known aligned
coordinate is fed in first, via `readers.faux`, which pins the grid. Both
tiles then land on offset 0.0. The anchor sits far outside the data and is
removed by a crop before writing.

## The one artefact that remains

A voxel straddling a tile boundary receives a point from each side, since each
tile is processed independently. That doubles the points in a one-voxel-wide
strip along each seam: on a 6-tile scene at voxel 0.65 that is on the order of
10 000 points out of 150 million, about 0.007%, and two points inside a single
voxel rather than one. Not worth a dedup pass, but worth knowing it is there.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from densify import radius_for, suffix_for

PDAL_EXE = r"C:\Program Files\QGIS 3.40.5\bin\pdal.exe"

# Far outside any Lambert 93 data, and on a whole-metre coordinate so the grid
# lands on round offsets for any voxel that divides evenly into a metre.
ANCHOR = (800000.0, 6300000.0, 0.0)


def tile_pipeline(tile, raster, voxel, origin, bounds, out_ply):
    ox, oy, oz = origin
    (minx, miny, maxx, maxy) = bounds
    ax, ay, az = ANCHOR
    stages = [
        # First reader wins: this pins the voxel grid identically for every tile.
        {"type": "readers.faux", "mode": "constant", "count": 1,
         "bounds": f"([{ax},{ax}],[{ay},{ay}],[{az},{az}])"},
        {"type": "readers.copc", "filename": str(tile)},
        {"type": "filters.merge"},
        {"type": "filters.voxeldownsize", "cell": voxel},
        # Drops the anchor along with anything outside the scene.
        {"type": "filters.crop",
         "bounds": f"([{minx},{maxx}],[{miny},{maxy}])"},
    ]
    if raster:
        stages.append({"type": "filters.colorization", "raster": str(raster)})
    stages.append({"type": "filters.transformation",
                   "matrix": f"1 0 0 {-ox}  0 1 0 {-oy}  0 0 1 {-oz}  0 0 0 1"})
    stages.append({"type": "writers.ply", "filename": str(out_ply),
                   "storage_mode": "little endian",
                   "dims": "X=float,Y=float,Z=float,Red=uint8,Green=uint8,Blue=uint8"})
    return {"pipeline": stages}


def ply_count_and_offset(path):
    with open(path, "rb") as f:
        raw = f.read(4096)
    end = raw.find(b"end_header\n")
    if end < 0:
        sys.exit(f"{path}: no end_header")
    n = None
    for line in raw[:end].decode("ascii", errors="replace").splitlines():
        if line.startswith("element vertex"):
            n = int(line.split()[-1])
    return n, end + len(b"end_header\n")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tiles", required=True)
    p.add_argument("--raster", default=None)
    p.add_argument("--voxel", type=float, required=True)
    p.add_argument("--origin", required=True)
    p.add_argument("--multiplier", type=float, default=1.0)
    p.add_argument("--name", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--keep-parts", action="store_true",
                   help="Do not delete the per-tile PLYs")
    a = p.parse_args()

    tiles_dir = Path(a.tiles)
    tiles = sorted(tiles_dir.glob("*.copc.laz"))
    if not tiles:
        sys.exit(f"no .copc.laz in {tiles_dir}")
    if a.raster and not Path(a.raster).is_file():
        sys.exit(f"--raster not found: {a.raster}")

    origin = [float(v) for v in a.origin.split(",")]
    if len(origin) != 3:
        sys.exit("--origin needs three values")

    radius = radius_for(a.voxel, a.multiplier)
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    parts_dir = out_dir / f"{a.name}_parts"
    parts_dir.mkdir(exist_ok=True)

    # Scene bounds, so the crop drops the anchor without clipping real data.
    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    for t in tiles:
        r = subprocess.run([PDAL_EXE, "info", "--summary", str(t)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"pdal info failed on {t.name}")
        b = json.loads(r.stdout)["summary"]["bounds"]
        minx = min(minx, b["minx"]); maxx = max(maxx, b["maxx"])
        miny = min(miny, b["miny"]); maxy = max(maxy, b["maxy"])
    bounds = (minx, miny, maxx, maxy)

    print(f"{len(tiles)} tiles, voxel {a.voxel}, radius {radius:.4f}", flush=True)
    print(f"scene bounds {minx:.0f},{miny:.0f} .. {maxx:.0f},{maxy:.0f}", flush=True)

    parts = []
    for i, t in enumerate(tiles, 1):
        part = parts_dir / f"{t.stem}.ply"
        pipe = parts_dir / f"{t.stem}.json"
        if part.is_file():
            n, _ = ply_count_and_offset(part)
            print(f"[{i}/{len(tiles)}] {t.name}: {n:,} pts (already done)", flush=True)
            parts.append(part)
            continue
        pipe.write_text(json.dumps(
            tile_pipeline(t, a.raster, a.voxel, origin, bounds, part), indent=2),
            encoding="utf-8")
        r = subprocess.run([PDAL_EXE, "pipeline", str(pipe)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"pdal failed on {t.name}: {r.stderr[-600:]}")
        n, _ = ply_count_and_offset(part)
        print(f"[{i}/{len(tiles)}] {t.name}: {n:,} pts", flush=True)
        parts.append(part)

    total = 0
    for part in parts:
        n, _ = ply_count_and_offset(part)
        total += n

    out_ply = out_dir / f"{a.name}-{suffix_for(radius)}.ply"
    print(f"\nconcatenating {len(parts)} parts, {total:,} points ...", flush=True)
    with open(out_ply, "wb") as fout:
        fout.write(b"ply\nformat binary_little_endian 1.0\n")
        fout.write(b"comment built per-tile by densify_tiled.py\n")
        fout.write(f"element vertex {total}\n".encode())
        fout.write(b"property float x\nproperty float y\nproperty float z\n")
        fout.write(b"property uchar red\nproperty uchar green\nproperty uchar blue\n")
        fout.write(b"end_header\n")
        for part in parts:
            _, off = ply_count_and_offset(part)
            with open(part, "rb") as fin:
                fin.seek(off)
                while True:
                    block = fin.read(64 << 20)
                    if not block:
                        break
                    fout.write(block)

    if not a.keep_parts:
        for part in parts:
            part.unlink(missing_ok=True)
        for j in parts_dir.glob("*.json"):
            j.unlink(missing_ok=True)
        parts_dir.rmdir()

    print(f"wrote {out_ply.name}  ({out_ply.stat().st_size/1e9:.2f} GB)")
    print(f"set Mesh to Points -> Radius = {radius:.4f}")


if __name__ == "__main__":
    main()
