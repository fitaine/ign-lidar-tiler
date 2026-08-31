"""Find the voxel size that yields a target point count, by measurement.

Stage 1a of the IGN LiDAR Tiler (see PLAN.md):

    python solve_voxel.py --tiles ./tiles --target 150000000

The terrain-aware formula in lidar_pipeline.py is calibrated on low and
moderate relief and under-performs badly on alpine terrain, which is why every
alpine scene has needed a --voxel override. So rather than trust a formula,
this measures: it voxel-downsamples a few tiles at two sizes, fits the local
exponent of `points = k * voxel ** -a`, and scales to the scene by AREA.

Scaling by area rather than by raw point count matters. The downsample output
counts occupied voxels, which follow the surface, while raw counts follow
flight overlap: across Mont Aiguille's six tiles the raw counts span 2.4x
(62M to 152M) while their occupied-voxel counts span only 1.8x. Scaling by raw
counts missed the target by 27%; scaling by area lands within 1%.

Verified 2026-08-31 on Mont Aiguille: asked for 150M, answered voxel 0.493,
against a measured 0.49 that produced 153.0M points.
"""

import argparse
import json
import subprocess
import sys
import tempfile
from math import log
from pathlib import Path

PDAL_EXE = r"C:\Program Files\QGIS 3.40.5\bin\pdal.exe"


def summary(path):
    r = subprocess.run([PDAL_EXE, "info", "--summary", str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"pdal info failed on {path}: {r.stderr[:400]}")
    return json.loads(r.stdout)["summary"]


def count_after_downsample(tile, voxel, workdir):
    out = Path(workdir) / f"probe_{voxel}.laz"
    pipe = Path(workdir) / f"probe_{voxel}.json"
    pipe.write_text(json.dumps({"pipeline": [
        {"type": "readers.copc", "filename": str(tile)},
        {"type": "filters.voxeldownsize", "cell": voxel},
        {"type": "writers.las", "filename": str(out), "compression": "laszip"},
    ]}), encoding="utf-8")
    r = subprocess.run([PDAL_EXE, "pipeline", str(pipe)], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"pdal failed at voxel {voxel}: {r.stderr[:400]}")
    n = summary(out)["num_points"]
    out.unlink(missing_ok=True)
    return n


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tiles", required=True, help="Folder of .copc.laz tiles")
    p.add_argument("--target", type=int, required=True, help="Target point count")
    p.add_argument("--probe", default="0.60,1.00",
                   help="Two voxel sizes to measure with (default 0.60,1.00)")
    p.add_argument("--probe-tiles", type=int, default=3,
                   help="How many tiles to probe, spread across the density range (default 3)")
    p.add_argument("--multiplier", type=float, default=1.0,
                   help="Ball radius multiplier, 1.0 = spheres touch")
    a = p.parse_args()

    tiles = sorted(Path(a.tiles).glob("*.copc.laz"))
    if not tiles:
        sys.exit(f"no .copc.laz in {a.tiles}")

    counts = [(t, summary(t)["num_points"]) for t in tiles]
    raw_total = sum(n for _, n in counts)
    counts.sort(key=lambda kv: kv[1])

    # Scale by AREA, not by raw point count. The downsample output is a count
    # of occupied voxels, which follows the surface, while raw counts follow
    # flight overlap: a tile flown twice has double the returns and nearly the
    # same voxels. Scaling by raw counts inherits the probe tile's overlap and
    # was measured to miss by 27% on Mont Aiguille.
    k = max(1, min(a.probe_tiles, len(counts)))
    if k == 1:
        picks = [counts[len(counts) // 2]]
    else:
        step = (len(counts) - 1) / (k - 1)
        picks = [counts[round(i * step)] for i in range(k)]
    print(f"{len(tiles)} tiles, {raw_total:,} raw points", flush=True)
    print(f"probing {len(picks)} tile(s), scaling by area:", flush=True)

    v1, v2 = (float(v) for v in a.probe.split(","))
    n1 = n2 = 0
    with tempfile.TemporaryDirectory() as wd:
        for t, raw in picks:
            c1 = count_after_downsample(t, v1, wd)
            c2 = count_after_downsample(t, v2, wd)
            n1 += c1
            n2 += c2
            print(f"  {t.name}  raw {raw:,}  v{v1}: {c1:,}  v{v2}: {c2:,}", flush=True)

    if n1 == n2:
        sys.exit("the two probes gave the same count; pick probe sizes further apart")
    exponent = log(n1 / n2) / log(v2 / v1)
    scale = len(tiles) / float(len(picks))
    # points(scene, v) = n1 * scale * (v1 / v) ** exponent
    target_v = v1 * (n1 * scale / a.target) ** (1.0 / exponent)
    radius = target_v / 2.0 * a.multiplier

    print(f"\nfitted points proportional to voxel^-{exponent:.3f}", flush=True)
    print(f"scene is {scale:.2f}x the probed area", flush=True)
    print(f"\nfor {a.target:,} points:")
    print(f"  --voxel {target_v:.3f}")
    print(f"  radius {radius:.4f}   (suffix -{round(radius*100):03d})")
    est = n1 * scale * (v1 / target_v) ** exponent
    print(f"  predicted {est:,.0f} points, about {est*15/1e9:.2f} GB as a PLY")
    if not (0.05 <= target_v <= 5.0):
        print("\nWARNING that voxel is outside the sane range; check the target")


if __name__ == "__main__":
    main()
