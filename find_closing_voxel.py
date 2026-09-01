"""Find the finest voxel at which a scene's spheres still touch.

radius = voxel/2 is exact geometry for a FILLED lattice. A LiDAR scene fills
its lattice only while the cells stay coarser than the scan's own sampling;
below that, cells come up empty, the real spacing is two or three cells, and a
radius sized for one cell leaves the surface as specks. That size is a
property of the scene, not of the target point count, so measure it once and
keep it as the floor for every variant.

    python find_closing_voxel.py --tiles ./tiles --sizes 0.35,0.45,0.55,0.70

Downsamples ONE tile at each size and measures the result directly, which
takes a couple of minutes per size rather than the hours a full build costs.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_fill

PDAL_EXE = r"C:\Program Files\QGIS 3.40.5\bin\pdal.exe"


def downsample(tile, voxel, out):
    """Voxel-downsample one tile to a PLY that check_fill can read."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump({"pipeline": [
            {"type": "readers.copc", "filename": str(tile)},
            {"type": "filters.voxeldownsize", "cell": voxel},
            {"type": "writers.ply", "filename": str(out),
             "storage_mode": "little endian"},
        ]}, f)
        pipe = f.name
    r = subprocess.run([PDAL_EXE, "pipeline", pipe], capture_output=True, text=True)
    Path(pipe).unlink(missing_ok=True)
    if r.returncode != 0:
        sys.exit(f"pdal failed at voxel {voxel}: {r.stderr[:400]}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tiles", required=True, help="Folder of .copc.laz tiles")
    p.add_argument("--sizes", default="0.30,0.40,0.50,0.60,0.80",
                   help="Voxel sizes to measure, in metres")
    p.add_argument("--tile", default=None,
                   help="Which tile to probe. Default: the median-sized one, "
                        "so neither the emptiest nor the busiest")
    a = p.parse_args()

    tiles = sorted(Path(a.tiles).glob("*.copc.laz"))
    if not tiles:
        sys.exit(f"no .copc.laz in {a.tiles}")
    tile = Path(a.tile) if a.tile else sorted(tiles, key=lambda t: t.stat().st_size)[len(tiles)//2]
    sizes = sorted(float(v) for v in a.sizes.split(","))

    print(f"probing {tile.name}")
    print(f"{'voxel':>7s} {'points':>12s} {'pitch':>7s} {'isolated':>9s} {'closed':>7s}  verdict")
    results = []
    with tempfile.TemporaryDirectory() as wd:
        for v in sizes:
            out = Path(wd) / f"probe_{v}.ply"
            downsample(tile, v, out)
            r = check_fill.measure(out, v)
            tag, _ = check_fill.verdict(r)
            results.append((v, tag, r))
            print(f"{v:7.3f} {r['points']:12,} {r['pitch_ratio']:7.2f} "
                  f"{100*r['isolated']:8.1f}% {100*r['fill']:6.0f}%  {tag}")
            out.unlink(missing_ok=True)

    closing = next((v for v, tag, _ in results if tag == "OK"), None)
    print()
    if closing is None:
        print("none of these sizes closes the surface; probe coarser")
    else:
        print(f"finest voxel that closes the surface: {closing:.2f} m "
              f"-> radius {closing/2:.3f}")
        print("Use that as the floor. A finer voxel buys specks, not detail.")


if __name__ == "__main__":
    raise SystemExit(main())
