"""Measure whether a point cloud's spheres actually touch.

The failure this catches: a voxel finer than the LiDAR's own sampling. The
downsample then stops downsampling — every return keeps its own cell, the grid
is mostly empty, and the derived radius (voxel/2) is sized for a lattice that
was never filled. On screen the surface breaks into isolated specks.

Point count does not reveal it: La Plagne's 208M-point cloud at voxel 0.238 m
looked like a win and rendered as dust, because 36% of its points had no
touching neighbour at all.

    python check_fill.py cloud.ply                 # voxel from the -NNN suffix
    python check_fill.py cloud.ply --voxel 0.238

Reads a contiguous block from the middle of the file rather than the whole
cloud: at a few million points the local geometry is already decided.
"""
import argparse
import re
import sys
from pathlib import Path

import numpy as np

SLACK = 1.05  # the -NNN suffix rounds the radius to whole centimetres, so a
              # lattice can sit a fraction of a percent beyond 2R and still be
              # closed. Without this slack a perfectly good cloud reads as empty.

# A point cloud of a SURFACE can only occupy 4 of a 3D lattice's 6 face slots:
# the two along the surface normal are empty by definition. Counting out of 6
# made every good scene look half broken.
SURFACE_SLOTS = 4

# Thresholds calibrated on clouds whose renders are known, not invented:
#
#   scene                     voxel   medNN  p90/med  no neighbour
#   mont-aiguille-035         0.700   0.700     1.00     2.4%   rendered well
#   barre-des-ecrins-hd2-035  0.700   0.700     1.00     0.8%   rendered well
#   plagne-extended-dense-039 0.780   0.784     1.00     4.4%   rendered well
#   interferometre-de-bure-016 0.320  0.320     1.41    17.0%   rendered well, thin
#   plagne-dense-012-carved   0.240   0.238     2.24    45.8%   rendered as dust
#   tremplin-du-dauphine-009  0.180   0.290     1.90    77.7%   grid never filled
#
# PITCH is the reliable gate. When the measured spacing exceeds the voxel, the
# grid was never filled at its own pitch and no point count redeems it.
#
# ISOLATION is informative but noisy, and cannot be read as a quality score on
# its own: VEGETATION NEVER FILLS A LATTICE AT ANY VOXEL. Canopy returns are
# irregular by nature, so on a forested scene the isolated fraction stays high
# however coarse the cells get — measured across five patches of La Plagne's
# 0.784 m cloud it ranged from 3% on open snow to 55% in the trees, on a cloud
# whose render is fine. So the isolation thresholds are set where the evidence
# puts them: Bure at 17% and La Plagne's good 0.784 m cloud at 13% both render
# well, while the failed cloud sits at 70%.
ISOLATED_OK = 0.20
ISOLATED_BAD = 0.45
PITCH_BAD = 1.15


PLY_TYPES = {"float": "<f4", "float32": "<f4", "double": "<f8", "float64": "<f8",
             "uchar": "u1", "uint8": "u1", "char": "i1", "int8": "i1",
             "ushort": "<u2", "uint16": "<u2", "short": "<i2", "int16": "<i2",
             "uint": "<u4", "uint32": "<u4", "int": "<i4", "int32": "<i4"}


def read_header(path):
    """Point count, data offset and record layout.

    The layout is read rather than assumed: densify_tiled.py writes xyz+rgb,
    but a probe cloud straight out of PDAL has no colour, and guessing the
    record size on that silently reads garbage coordinates.
    """
    with open(path, "rb") as f:
        blob = b""
        while b"end_header\n" not in blob:
            chunk = f.read(4096)
            if not chunk:
                sys.exit(f"{path.name}: no PLY header found")
            blob += chunk
    head = blob[:blob.index(b"end_header\n")].decode("ascii", "replace")
    if "binary_little_endian" not in head:
        sys.exit(f"{path.name}: only binary little-endian PLY is supported")

    n, fields, in_vertex = None, [], False
    for line in head.splitlines():
        w = line.split()
        if not w:
            continue
        if w[0] == "element":
            in_vertex = w[1] == "vertex"
            if in_vertex:
                n = int(w[2])
        elif w[0] == "property" and in_vertex:
            if w[1] == "list":
                sys.exit(f"{path.name}: list properties in the vertex element")
            if w[1] not in PLY_TYPES:
                sys.exit(f"{path.name}: unsupported property type {w[1]!r}")
            fields.append((w[2], PLY_TYPES[w[1]]))
    if n is None or not {"x", "y", "z"} <= {f for f, _ in fields}:
        sys.exit(f"{path.name}: no vertex element with x/y/z")

    dtype = np.dtype([(f, t) for f, t in fields])
    return n, blob.index(b"end_header\n") + len(b"end_header\n"), dtype


def sample(path, count, n_points, offset, dtype, at=0.5):
    """A contiguous block from one place in the file.

    Contiguous because neighbours must be real neighbours: a random scatter of
    points from across the scene has no local geometry to measure.
    """
    count = min(count, n_points)
    start = int((n_points - count) * min(max(at, 0.0), 1.0))
    with open(path, "rb") as f:
        f.seek(offset + dtype.itemsize * start)
        buf = f.read(dtype.itemsize * count)
    a = np.frombuffer(buf, dtype=dtype, count=len(buf) // dtype.itemsize)
    return np.stack([a["x"], a["y"], a["z"]], 1).astype(np.float64)


def voxel_from_name(path):
    """densify_tiled.py appends the radius: -036 means radius 0.36, voxel 0.72."""
    m = re.search(r"-(\d{3})(?:-carved)?\.ply$", path.name)
    return round(int(m.group(1)) / 100.0 * 2, 4) if m else None


def measure_window(xyz, voxel, window):
    """Stats for one patch of ground, or None if the patch is too thin to judge."""
    from scipy.spatial import cKDTree

    centre = xyz[len(xyz) // 2]
    w = xyz[np.all(np.abs(xyz - centre) < window, axis=1)]
    if len(w) < 1000:
        return None

    tree = cKDTree(w)
    nn = tree.query(w, k=2)[0][:, 1]
    # neighbours close enough for the spheres to meet, on a subsample: the
    # ball query is the expensive part and 1 in 10 settles it
    touching = np.array([len(ids) for ids
                         in tree.query_ball_point(w[::10], voxel * SLACK)]) - 1
    return {
        "window_points": int(len(w)),
        "nn_median": float(np.median(nn)),
        "nn_p90": float(np.percentile(nn, 90)),
        "touching_mean": float(touching.mean()),
        "isolated": float((touching == 0).mean()),
    }


def measure(path, voxel, points=4_000_000, window=None, windows=5):
    """Measure several patches spread through the file and take the median.

    One patch is not enough. A single window lands wherever the file happens to
    be halfway through, and forest, snowfield and rooftop have wildly different
    local densities: measuring one size in the trees and the next on a piste
    produced a ladder where 0.40 m scored better than 0.50 m, which no physical
    process would do. Several patches, median across them.
    """
    path = Path(path)
    n_points, offset, dtype = read_header(path)

    # A coarse cloud holds far fewer points per square metre, so a window that
    # is generous at 0.24 m is starved at 2 m. Scale it with the lattice.
    if window is None:
        window = max(25.0, 40.0 * voxel)

    per_window = max(200_000, points // max(windows, 1))
    seen = []
    for i in range(max(windows, 1)):
        at = (i + 0.5) / max(windows, 1)
        got = measure_window(sample(path, per_window, n_points, offset, dtype, at),
                             voxel, window)
        if got:
            seen.append(got)
    if not seen:
        sys.exit(f"{path.name}: no sampled patch held enough points to judge; "
                 f"raise --points or --window")

    def med(key):
        return float(np.median([s[key] for s in seen]))

    median_nn = med("nn_median")
    return {
        "file": path.name,
        "points": n_points,
        "voxel": voxel,
        "radius": voxel / 2.0,
        "windows": len(seen),
        "window_points": int(np.median([s["window_points"] for s in seen])),
        "nn_median": median_nn,
        "nn_p90": med("nn_p90"),
        "pitch_ratio": median_nn / voxel,          # 1.0 when the grid is filled
        "touching_mean": med("touching_mean"),
        "fill": float(min(med("touching_mean") / SURFACE_SLOTS, 1.0)),
        "isolated": med("isolated"),
        "isolated_spread": (float(min(s["isolated"] for s in seen)),
                            float(max(s["isolated"] for s in seen))),
    }


def verdict(r):
    """Judge on isolation and pitch, the two things that decide the look."""
    if r.get("pitch_ratio", 1.0) > PITCH_BAD:
        return "DUST", (f"the points sit {r['pitch_ratio']:.2f}x further apart than "
                        f"the voxel: the grid was never filled at its own pitch, so "
                        f"the voxel is finer than the scan itself")
    if r["isolated"] > ISOLATED_BAD:
        return "DUST", (f"{100*r['isolated']:.0f}% of points have no neighbour close "
                        f"enough to touch: this renders as specks, not a surface")
    if r["isolated"] > ISOLATED_OK:
        return "THIN", ("some gaps will show in lit areas; usable, but a coarser "
                        "voxel would close them and render faster")
    return "OK", "the surface closes"


def report(r):
    tag, note = verdict(r)
    print(f"{r['file']}")
    print(f"  points            {r['points']:,}")
    print(f"  voxel / radius    {r['voxel']:.3f} m / {r['radius']:.4f}")
    print(f"  nearest neighbour median {r['nn_median']:.3f} m, p90 {r['nn_p90']:.3f} m"
          f"   (pitch {r['pitch_ratio']:.2f}x the voxel)")
    print(f"  touching neighbours {r['touching_mean']:.2f} of {SURFACE_SLOTS} "
          f"-> surface {100*r['fill']:.0f}% closed")
    lo, hi = r.get("isolated_spread", (r["isolated"], r["isolated"]))
    print(f"  points with none    {100*r['isolated']:.1f}%  "
          f"(median of {r.get('windows', 1)} patches, {100*lo:.1f}-{100*hi:.1f}%)")
    print(f"  {tag}: {note}")
    return tag


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("ply", nargs="+")
    p.add_argument("--voxel", type=float, default=None,
                   help="Voxel in metres. Default: read the -NNN radius suffix")
    p.add_argument("--points", type=int, default=4_000_000)
    p.add_argument("--window", type=float, default=None,
                   help="Sample window in metres (default: scaled to the voxel)")
    a = p.parse_args()

    worst = "OK"
    for name in a.ply:
        path = Path(name)
        voxel = a.voxel or voxel_from_name(path)
        if voxel is None:
            sys.exit(f"{path.name}: no -NNN suffix to read the voxel from, pass --voxel")
        tag = report(measure(path, voxel, a.points, a.window))
        worst = tag if (tag == "DUST" or (tag == "THIN" and worst == "OK")) else worst
        print()
    return {"OK": 0, "THIN": 0, "DUST": 1}[worst]


if __name__ == "__main__":
    raise SystemExit(main())
