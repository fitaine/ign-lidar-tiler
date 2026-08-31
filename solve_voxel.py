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
    p.add_argument("--coverage", type=float, default=1.0,
                   help="Fraction of the footprint that survives a carve. The "
                        "target is then reached AFTER cropping, not before.")
    p.add_argument("--probe-tiles", type=int, default=3,
                   help="How many tiles to fit the exponent on (default 3)")
    p.add_argument("--scale-tiles", default="all",
                   help="Tiles to measure the scene total on: 'all' (default, "
                        "exact) or a number. Guessing this was the dominant error "
                        "on scenes with varied terrain.")
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
    with tempfile.TemporaryDirectory() as wd:
        # The EXPONENT only needs a local fit, so a couple of tiles will do.
        n1 = n2 = 0
        for t, raw in picks:
            c1 = count_after_downsample(t, v1, wd)
            c2 = count_after_downsample(t, v2, wd)
            n1 += c1
            n2 += c2
            print(f"  {t.name}  raw {raw:,}  v{v1}: {c1:,}  v{v2}: {c2:,}", flush=True)
        if n1 == n2:
            sys.exit("the two probes gave the same count; use sizes further apart")
        exponent = log(n1 / n2) / log(v2 / v1)

        # The SCALE is different: multiplying a few tiles up to the whole scene
        # assumes they represent it. On Mont Aiguille's six similar tiles that
        # landed within 0.7%; on La Plagne's sixteen, across 1400 m of relief,
        # it missed by 18% one way and 14% the other on the same scene.
        # Measuring every tile once at the coarse size costs minutes and
        # removes the assumption.
        if str(a.scale_tiles).lower() == "all":
            scale_set = [t for t, _ in counts]
        else:
            k2 = max(1, min(int(a.scale_tiles), len(counts)))
            step2 = (len(counts) - 1) / max(k2 - 1, 1)
            scale_set = [counts[round(i * step2)][0] for i in range(k2)]
        print(f"\nmeasuring the scene total on {len(scale_set)} tile(s) "
              f"at voxel {v2}:", flush=True)
        measured = 0
        for i, t in enumerate(scale_set, 1):
            c = count_after_downsample(t, v2, wd)
            measured += c
            print(f"  [{i}/{len(scale_set)}] {t.name}: {c:,}", flush=True)

    exact = len(scale_set) == len(tiles)
    scene_at_v2 = measured * (len(tiles) / float(len(scale_set)))

    effective_target = a.target / max(a.coverage, 1e-6)
    if a.coverage < 1.0:
        print(f"carve keeps {100*a.coverage:.1f}% of the footprint, so solving "
              f"for {effective_target:,.0f} points before cropping", flush=True)
    target_v = v2 * (scene_at_v2 / effective_target) ** (1.0 / exponent)
    radius = target_v / 2.0 * a.multiplier

    print(f"\nfitted points proportional to voxel^-{exponent:.3f}", flush=True)
    print(f"scene at voxel {v2}: {scene_at_v2:,.0f} points "
          f"({'measured on every tile' if exact else 'estimated from a sample'})",
          flush=True)
    print(f"\nfor {a.target:,} points:")
    print(f"  --voxel {target_v:.3f}")
    print(f"  radius {radius:.4f}   (suffix -{round(radius*100):03d})")
    est = scene_at_v2 * (v2 / target_v) ** exponent
    print(f"  predicted {est:,.0f} points before cropping, "
          f"{est*a.coverage:,.0f} after, about {est*a.coverage*15/1e9:.2f} GB as a PLY")
    if not exact:
        print("  NOTE the scale came from a sample; on varied terrain that has "
              "missed by 15% or more. Use --scale-tiles all to measure it.")
    if not (0.05 <= target_v <= 5.0):
        print("\nWARNING that voxel is outside the sane range; check the target")


if __name__ == "__main__":
    main()
