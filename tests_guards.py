"""Check the two guards that would have caught the La Plagne render.

Neither guard can be exercised for real without PDAL and a folder of tiles, so
the point counts come from a synthetic scene: a surface that scales as
voxel^-2 while the cells are coarser than the scan, and flattens out below it.
That is the shape the real data has — La Plagne measured voxel^-1.96 between
2.0 and 0.78 m, and voxel^-0.26 between 0.78 and 0.24.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solve_voxel
import check_fill

SAMPLING = 0.78      # the scan's own spacing: finer cells stop finding points
COARSE_COUNT = 9.5e6  # points per probe tile at 1 m


def synthetic(sampling):
    """A tile whose count follows voxel^-2 until the cells outrun the scan."""
    def count(tile, voxel, workdir):
        if voxel >= sampling:
            return int(COARSE_COUNT * voxel ** -2.0)
        plateau = COARSE_COUNT * sampling ** -2.0
        return int(plateau * (sampling / voxel) ** 0.26)   # sampled out
    return count


def run_verify(sampling, target_v, effective_target, allow=False):
    solve_voxel.count_after_downsample = synthetic(sampling)
    picks = [(Path("tile_a.copc.laz"), 0), (Path("tile_b.copc.laz"), 0)]
    v1, v2 = 0.60, 1.00
    n1 = sum(synthetic(sampling)(t, v1, None) for t, _ in picks)
    n2 = sum(synthetic(sampling)(t, v2, None) for t, _ in picks)
    scene_at_v2 = n2 * 8          # eight times the probe tiles
    return solve_voxel.verify_below_range(
        picks, target_v, v1, v2, n1, n2, scene_at_v2, effective_target,
        multiplier=1.0, allow_saturated=allow)


def check_pipeline_shape():
    """The PDAL stages densify_tiled builds, which decide what lands in the PLY.

    Two things went wrong here in practice and both are cheap to pin down: the
    grid anchor survived a polygon crop and stretched the cloud's bounding box
    by 6 500 km, and voxel 0 has no grid to anchor in the first place.
    """
    import densify_tiled

    print("\n--- PDAL stages ---")
    failures = 0
    poly = "POLYGON ((0 0, 1 0, 1 1, 0 0))"

    native = densify_tiled.tile_pipeline("t.copc.laz", None, 0, (1.0, 2.0, 3.0),
                                         (0, 0, 10, 10), "out.ply", polygon=poly)
    kinds = [s["type"] for s in native["pipeline"]]
    failures += check("voxel 0 adds no grid anchor", "readers.faux" not in kinds)
    failures += check("voxel 0 does not downsample",
                      "filters.voxeldownsize" not in kinds)

    for label, voxel in (("voxel 0", 0), ("voxel 0.4", 0.4)):
        p = densify_tiled.tile_pipeline("t.copc.laz", None, voxel, (1.0, 2.0, 3.0),
                                        (0, 0, 10, 10), "out.ply", polygon=poly)
        crops = [s for s in p["pipeline"] if s["type"] == "filters.crop"]
        ok = (len(crops) == 2 and "bounds" in crops[0] and "polygon" in crops[1])
        failures += check(f"{label} crops by bounds before polygon", ok)

    plain = densify_tiled.tile_pipeline("t.copc.laz", None, 0.4, (1.0, 2.0, 3.0),
                                        (0, 0, 10, 10), "out.ply")
    crops = [s for s in plain["pipeline"] if s["type"] == "filters.crop"]
    failures += check("no polygon means one bounds crop", len(crops) == 1)
    return failures


def check(label, ok):
    print(f"{'ok  ' if ok else 'FAIL'}  {label}")
    return 0 if ok else 1


def main():
    failures = 0

    # 1. the La Plagne shape: asked far below the sampling, cannot be delivered
    print("\n--- saturated scene, target unreachable ---")
    try:
        run_verify(SAMPLING, target_v=0.238, effective_target=460_000_000)
        failures += check("refuses a saturated answer", False)
    except SystemExit as exc:
        failures += check(f"refuses a saturated answer (exit {exc.code})", exc.code == 2)

    # 2. the same scene, with the escape hatch
    print("\n--- same, with --allow-saturated ---")
    voxel, radius = run_verify(SAMPLING, 0.238, 460_000_000, allow=True)
    failures += check("answers anyway when told to",
                      abs(voxel - 0.238) < 1e-9 and abs(radius - 0.119) < 1e-9)

    # 3. a scene sampled finely enough to deliver: must pass straight through
    print("\n--- densely sampled scene, target reachable ---")
    voxel, radius = run_verify(0.05, target_v=0.30, effective_target=1)
    failures += check("passes a reachable target", abs(voxel - 0.30) < 1e-9)

    # 4. the fill verdicts, which decide whether a render file gets written
    print("\n--- fill verdicts ---")
    # every row is a real cloud whose render is known, measured on disk with
    # the five-patch sampler
    cases = [
        ("mont-aiguille-035  rendered well", 0.024, 1.000, "OK"),
        ("ecrins-hd2-035     rendered well", 0.008, 1.000, "OK"),
        ("bure-016           rendered well", 0.170, 1.000, "OK"),
        ("plagne-039         rendered well, forested", 0.129, 1.005, "OK"),
        ("a thin but usable cloud", 0.300, 1.000, "THIN"),
        ("plagne-012-carved  rendered dust", 0.695, 1.400, "DUST"),
        ("tremplin-009       grid unfilled", 0.777, 1.611, "DUST"),
    ]
    for label, isolated, pitch, want in cases:
        tag, _ = check_fill.verdict({"isolated": isolated, "pitch_ratio": pitch})
        failures += check(f"{label} -> {tag}", tag == want)

    failures += check_pipeline_shape()

    print("\nall good" if not failures else f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
