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

import winrun
import sys
import tempfile
from math import log
from pathlib import Path

PDAL_EXE = r"C:\Program Files\QGIS 3.40.5\bin\pdal.exe"


def summary(path):
    r = winrun.run([PDAL_EXE, "info", "--summary", str(path)],
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
    r = winrun.run([PDAL_EXE, "pipeline", str(pipe)], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"pdal failed at voxel {voxel}: {r.stderr[:400]}")
    n = summary(out)["num_points"]
    out.unlink(missing_ok=True)
    return n


def verify_below_range(picks, target_v, v1, v2, n1, n2, scene_at_v2,
                       effective_target, multiplier, allow_saturated):
    """Measure at the extrapolated answer, and refuse it if the data is spent.

    Returns (voxel, radius), or exits when the target turns out to be
    unreachable and the caller has not passed --allow-saturated. A wrong voxel
    here costs a night of rendering; this check costs a few minutes.
    """
    fine, coarse = min(v1, v2), max(v1, v2)
    n_fine = n1 if v1 < v2 else n2
    n_coarse = n2 if v1 < v2 else n1
    mid = (target_v * fine) ** 0.5
    print(f"\nthe answer {target_v:.3f} is below the probed range "
          f"[{fine:.2f}, {coarse:.2f}], where the fit stops holding. "
          f"Measuring there instead of trusting it:", flush=True)

    with tempfile.TemporaryDirectory() as wd:
        got = {}
        for v in (target_v, mid):
            got[v] = sum(count_after_downsample(t, v, wd) for t, _ in picks)
            print(f"  voxel {v:.3f}: {got[v]:,} points on the probe tiles", flush=True)

    # scale the probe tiles up to the scene exactly as the coarse pass did
    at_target = scene_at_v2 * got[target_v] / float(n_coarse)
    print(f"\nmeasured: the scene holds {at_target:,.0f} points at voxel "
          f"{target_v:.3f}, against the {effective_target:,.0f} asked for "
          f"({100*at_target/max(effective_target,1):.0f}% of it)", flush=True)

    # Per-interval exponents. A real surface gives about 2; the number
    # collapsing towards 0 is the scene running out of data.
    counts = {target_v: got[target_v], mid: got[mid], fine: n_fine}
    steps = sorted(counts)
    print("\nhow the count actually grows as the cells shrink:", flush=True)
    usable = None
    for lo, hi in zip(steps, steps[1:]):
        e = log(counts[lo] / counts[hi]) / log(hi / lo)
        print(f"  {hi:.3f} -> {lo:.3f} m: voxel^-{e:.2f}   "
              f"{'real detail' if e >= 1.0 else 'sampled out'}", flush=True)
        if e >= 1.0:
            usable = lo
    # None of the measured intervals bought real detail: the probe range is
    # already past this scene's sampling, so the answer lies coarser than
    # anything measured here and cannot be named precisely.
    probe_too_fine = usable is None
    usable = usable or fine

    if at_target >= 0.85 * effective_target:
        return target_v, target_v / 2.0 * multiplier

    print(f"\nSATURATED: this scene cannot reach the target at any voxel. Below "
          f"about {usable:.2f} m the cells are finer than the scan itself, so "
          f"the extra points are isolated specks rather than surface.", flush=True)
    print(f"  reachable here      : about {at_target:,.0f} points before cropping",
          flush=True)
    if probe_too_fine:
        print(f"  finest useful voxel : coarser than {usable:.2f} m — even the "
              f"probe range is past this scene's sampling. Probe coarser to "
              f"find it, e.g. --probe {coarse:.2f},{2*coarse:.2f}", flush=True)
    else:
        print(f"  finest useful voxel : {usable:.2f} m", flush=True)
    if not allow_saturated:
        print("\nRefusing to answer. Lower the target, carve a larger area, or "
              "pass --allow-saturated if you want the specks anyway.", flush=True)
        sys.exit(2)
    print("\n--allow-saturated given, answering anyway", flush=True)
    return target_v, target_v / 2.0 * multiplier


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
    p.add_argument("--allow-saturated", action="store_true",
                   help="Answer anyway when the measurement shows the scene "
                        "cannot deliver the target. The cloud will render as "
                        "specks; you are saying you know that.")
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

    # ── the answer may be an extrapolation, and that is where it breaks ──
    # The exponent is fitted between the two probe sizes. Below that range a
    # LiDAR cloud stops behaving like a surface: once the cells are finer than
    # the scan's own sampling, shrinking them adds almost nothing, because the
    # points were never there. The fit keeps promising detail the data cannot
    # deliver. La Plagne asked for 460M before cropping, the fit answered voxel
    # 0.238, the scene held 208M, and the render was built out of specks. So
    # when the answer lands below the probed range, go and measure it.
    if target_v < min(v1, v2) * 0.98:
        target_v, radius = verify_below_range(
            picks, target_v, v1, v2, n1, n2, scene_at_v2, effective_target,
            a.multiplier, a.allow_saturated)

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
