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

RECORD = 15  # float32 xyz + uchar rgb, what densify_tiled.py writes
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
# So: under 5% isolated is clean, Bure at 17% is usable, and the failure sits
# far above that. The pitch ratio is the other tell — when the measured spacing
# exceeds the voxel the grid was never filled at its own pitch, whatever the
# point count says.
ISOLATED_OK = 0.05
ISOLATED_BAD = 0.25
PITCH_BAD = 1.15


def read_header(path):
    with open(path, "rb") as f:
        blob = b""
        while b"end_header\n" not in blob:
            chunk = f.read(4096)
            if not chunk:
                sys.exit(f"{path.name}: no PLY header found")
            blob += chunk
    n = int(next(l for l in blob.split(b"\n")
                 if l.startswith(b"element vertex")).split()[-1])
    return n, blob.index(b"end_header\n") + len(b"end_header\n")


def sample(path, count, n_points, offset):
    """A contiguous block from the middle: neighbours must be real neighbours."""
    count = min(count, n_points)
    with open(path, "rb") as f:
        f.seek(offset + RECORD * max(0, (n_points - count) // 2))
        buf = f.read(RECORD * count)
    dt = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
                   ("r", "u1"), ("g", "u1"), ("b", "u1")])
    a = np.frombuffer(buf, dtype=dt, count=len(buf) // RECORD)
    return np.stack([a["x"], a["y"], a["z"]], 1).astype(np.float64)


def voxel_from_name(path):
    """densify_tiled.py appends the radius: -036 means radius 0.36, voxel 0.72."""
    m = re.search(r"-(\d{3})(?:-carved)?\.ply$", path.name)
    return round(int(m.group(1)) / 100.0 * 2, 4) if m else None


def measure(path, voxel, points=4_000_000, window=None):
    from scipy.spatial import cKDTree

    path = Path(path)
    n_points, offset = read_header(path)
    xyz = sample(path, points, n_points, offset)
    centre = xyz[len(xyz) // 2]

    # A coarse cloud holds far fewer points per square metre, so a window that
    # is generous at 0.24 m is starved at 2 m. Scale it with the lattice.
    if window is None:
        window = max(25.0, 40.0 * voxel)
    w = xyz[np.all(np.abs(xyz - centre) < window, axis=1)]
    if len(w) < 1000:
        sys.exit(f"{path.name}: only {len(w)} points within {window:.0f} m; "
                 f"raise --points or --window")

    tree = cKDTree(w)
    nn = tree.query(w, k=2)[0][:, 1]
    # neighbours close enough for the spheres to meet, on a subsample: the
    # ball query is the expensive part and 1 in 10 settles it
    probe = w[::10]
    reach = voxel * SLACK
    touching = np.array([len(ids) for ids in tree.query_ball_point(probe, reach)]) - 1

    median = float(np.median(nn))
    return {
        "file": path.name,
        "points": n_points,
        "voxel": voxel,
        "radius": voxel / 2.0,
        "window_points": int(len(w)),
        "nn_median": median,
        "nn_p90": float(np.percentile(nn, 90)),
        "pitch_ratio": median / voxel,            # 1.0 when the grid is filled
        "touching_mean": float(touching.mean()),
        "fill": float(min(touching.mean() / SURFACE_SLOTS, 1.0)),
        "isolated": float((touching == 0).mean()),
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
    print(f"  points with none    {100*r['isolated']:.1f}%")
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
