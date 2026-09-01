"""Build a dense PLY variant of an existing scene, in the scene's own frame.

Stage 3 of the IGN LiDAR Tiler, command line first (see PLAN.md).

Reads the raw COPC tiles, downsamples to the requested voxel, colorizes from
the ortho, recentres on the scene origin and writes a PLY. The recentring is
not optional: writing a PLY in absolute Lambert 93 quantises it to a 0.5 m
lattice in Y, because writers.ply stores float32.

The origin must be the *centroid* of the existing cloud, not its bbox corner,
so the new variant lands exactly where the old one sits and nothing in the
.blend has to move.
"""

import argparse
import json
import subprocess

import winrun
import sys
from pathlib import Path

PDAL_EXE = r"C:\Program Files\QGIS 3.40.5\bin\pdal.exe"


def radius_for(voxel, multiplier=1.0):
    """Ball radius at which neighbouring spheres touch without overlapping."""
    return voxel / 2.0 * multiplier


def suffix_for(radius):
    return f"{round(radius * 100):03d}"


def build_pipeline(tiles, raster, voxel, origin, out_ply):
    ox, oy, oz = origin
    stages = [{"type": "readers.copc", "filename": str(t)} for t in tiles]
    stages.append({"type": "filters.merge"})
    if voxel:
        stages.append({"type": "filters.voxeldownsize", "cell": voxel})
    if raster:
        stages.append({"type": "filters.colorization", "raster": str(raster)})
    # Recentre AFTER colorization: colorization samples the raster in map coords.
    stages.append({
        "type": "filters.transformation",
        "matrix": f"1 0 0 {-ox}  0 1 0 {-oy}  0 0 1 {-oz}  0 0 0 1",
    })
    stages.append({
        "type": "writers.ply",
        "filename": str(out_ply),
        "storage_mode": "little endian",
        "dims": "X=float,Y=float,Z=float,Red=uint8,Green=uint8,Blue=uint8",
    })
    return {"pipeline": stages}


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tiles", required=True,
                   help="Folder of source .copc.laz tiles")
    p.add_argument("--raster", default=None,
                   help="Ortho GeoTIFF to colorize from (prefer the -020 native one)")
    p.add_argument("--voxel", type=float, required=True,
                   help="Target voxel size in metres; radius is derived from it")
    p.add_argument("--origin", required=True,
                   help="X,Y,Z to subtract. Must be the existing cloud's CENTROID")
    p.add_argument("--multiplier", type=float, default=1.0,
                   help="Ball radius multiplier, 1.0 = spheres touch without overlapping")
    p.add_argument("--name", required=True, help="Base name; radius suffix is appended")
    p.add_argument("--out", required=True, help="Output folder")
    p.add_argument("--dry-run", action="store_true", help="Write the pipeline JSON only")
    a = p.parse_args()

    # Validate everything up front. PDAL opens the raster only at the
    # colorization stage, which is after all the reading and downsampling, so a
    # bad path otherwise fails an hour into the run.
    tiles_dir = Path(a.tiles)
    if not tiles_dir.is_dir():
        sys.exit(f"--tiles is not a folder: {tiles_dir}")
    tiles = sorted(tiles_dir.glob("*.copc.laz"))
    if not tiles:
        sys.exit(f"No .copc.laz found in {tiles_dir}")

    if a.raster:
        raster = Path(a.raster)
        if not raster.is_file():
            sys.exit(f"--raster not found: {raster}")
        probe = winrun.run([PDAL_EXE, "info", "--summary", str(tiles[0])],
                               capture_output=True)
        if probe.returncode != 0:
            sys.exit(f"cannot read {tiles[0].name}: {probe.stderr.decode(errors='replace')}")

    origin = [float(v) for v in a.origin.split(",")]
    if len(origin) != 3:
        sys.exit("--origin needs three comma-separated values")

    radius = radius_for(a.voxel, a.multiplier)
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_ply = out_dir / f"{a.name}-{suffix_for(radius)}.ply"
    pipe_json = out_dir / f"{a.name}-{suffix_for(radius)}_pipeline.json"

    pipeline = build_pipeline(tiles, a.raster, a.voxel, origin, out_ply)
    pipe_json.write_text(json.dumps(pipeline, indent=2), encoding="utf-8")

    print(f"tiles    : {len(tiles)}")
    for t in tiles:
        print(f"           {t.name}")
    print(f"voxel    : {a.voxel} m")
    print(f"radius   : {radius:.4f} m  (multiplier {a.multiplier})")
    print(f"origin   : {origin}")
    print(f"raster   : {a.raster}")
    print(f"output   : {out_ply}")
    print(f"pipeline : {pipe_json}")
    if a.dry_run:
        return

    meta_json = out_dir / f"{a.name}-{suffix_for(radius)}_metadata.json"
    cmd = [PDAL_EXE, "pipeline", str(pipe_json), "--metadata", str(meta_json)]
    print("\nrunning pdal ...", flush=True)
    r = winrun.stream(cmd)
    if r.returncode != 0:
        sys.exit(f"pdal failed with {r.returncode}")

    size_gb = out_ply.stat().st_size / 1e9
    print(f"\nwrote {out_ply.name}  ({size_gb:.2f} GB)")
    print(f"set Mesh to Points -> Radius = {radius:.3f}")


if __name__ == "__main__":
    main()
