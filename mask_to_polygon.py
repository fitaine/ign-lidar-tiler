"""Turn a Blender carve mask into a GeoJSON polygon, so PDAL can crop the tiles.

Stage 3a-bis. extract_mask.py gives an occupancy grid; densify_tiled.py wants a
polygon it can hand to filters.crop. With one, the dense build reads only the
ground the camera sees instead of the whole footprint, which is the difference
between writing 300M points and throwing most away, and writing 95M and keeping
them.

The polygon is deliberately a little generous: it traces the outline of the
carve and fills its interior holes, because a crop is an I/O saving, not the
final shape. crop_to_mask.py still applies the exact mask afterwards, holes and
all. A ragged 30 000-vertex boundary would slow PDAL down for no gain, so the
trace is simplified to whole grid steps and then to straight runs.

    python mask_to_polygon.py --mask scene-carve-mask.npz \
        --origin 987002.47,6497720.55,1639.35 --out carve.geojson
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np


def occupancy(mask_path):
    """Planimetric occupancy grid, its cell size and its local-space corner."""
    z = np.load(mask_path, allow_pickle=True)
    keys, dims, cell, mn = z["keys"], z["dims"], float(z["cell"]), z["mn"]
    kx = keys // (dims[1] * dims[2])
    ky = (keys // dims[2]) % dims[1]
    grid = np.zeros((int(dims[0]), int(dims[1])), dtype=bool)
    grid[kx, ky] = True
    return grid, cell, mn


def largest_blob(grid):
    """Keep the biggest connected piece, and close its interior holes.

    A carve can leave specks behind — a stray vertex island far from the
    subject. Cropping to a polygon that detours around those would drag the
    boundary across the whole scene for a handful of points.
    """
    from scipy import ndimage

    labels, n = ndimage.label(grid)
    if n == 0:
        sys.exit("the mask is empty")
    if n > 1:
        sizes = ndimage.sum(grid, labels, range(1, n + 1))
        keep = int(np.argmax(sizes)) + 1
        dropped = int(grid.sum() - sizes[keep - 1])
        print(f"{n} separate pieces; keeping the largest and dropping "
              f"{dropped:,} cells in the others", flush=True)
        grid = labels == keep
    return ndimage.binary_fill_holes(grid)


def trace_outline(grid):
    """Boundary of a filled region, as grid-corner vertices.

    Every filled cell contributes its four edges, wound the same way. An edge
    between two filled cells therefore appears twice, once in each direction,
    and the two cancel; what survives is exactly the boundary. Chaining those
    survivors head to tail gives the ring.

    This replaces a turtle-style square trace, which walked into a corner and
    failed to close on a mask with pinch points — carves have plenty, since a
    valley traced by hand narrows to a cell or two in places.
    """
    rows, cols = np.nonzero(grid)
    W1 = grid.shape[1] + 1

    def vid(r, c):
        return r * W1 + c

    edges = set()
    for r, c in zip(rows.tolist(), cols.tolist()):
        corners = [vid(r, c), vid(r, c + 1), vid(r + 1, c + 1), vid(r + 1, c)]
        for a, b in zip(corners, corners[1:] + corners[:1]):
            if (b, a) in edges:
                edges.discard((b, a))     # shared with a neighbour: interior
            else:
                edges.add((a, b))
    if not edges:
        sys.exit("the mask has no boundary")

    succ = {}
    for a, b in edges:
        succ.setdefault(a, []).append(b)

    loops = []
    while succ:
        start = next(iter(succ))
        loop, node = [start], start
        while True:
            nxt = succ.get(node)
            if not nxt:
                break
            following = nxt.pop()
            if not nxt:
                del succ[node]
            loop.append(following)
            node = following
            if node == start:
                break
        loops.append(loop)

    ring = max(loops, key=len)
    if len(loops) > 1:
        print(f"{len(loops)} boundary loops; keeping the longest "
              f"({len(ring):,} of {sum(len(l) for l in loops):,} vertices)", flush=True)
    return [(v // W1, v % W1) for v in ring]


def drop_collinear(ring):
    """Collapse straight runs: a 3 m staircase does not need a vertex per step."""
    out = []
    for p in ring:
        if len(out) >= 2:
            (ar, ac), (br, bc) = out[-2], out[-1]
            if (br - ar, bc - ac) == (p[0] - br, p[1] - bc):
                out[-1] = p
                continue
        out.append(p)
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mask", required=True, help="carve-mask.npz from extract_mask.py")
    p.add_argument("--origin", required=True,
                   help="X,Y,Z the PLY was recentred by, to get back to Lambert 93")
    p.add_argument("--pad", type=float, default=3.0,
                   help="Metres of slack around the outline (default 3.0)")
    p.add_argument("--out", required=True, help="Output .geojson")
    a = p.parse_args()

    grid, cell, mn = occupancy(Path(a.mask))
    print(f"mask: {grid.sum():,} occupied cells of {grid.size:,} at {cell} m", flush=True)
    filled = largest_blob(grid)
    ring = drop_collinear(trace_outline(filled))
    print(f"outline: {len(ring):,} vertices after dropping collinear steps", flush=True)

    ox, oy, _ = (float(v) for v in a.origin.split(","))
    centre = np.array([filled.shape[0], filled.shape[1]]) / 2.0
    coords = []
    for r, c in ring:
        # grid corner -> local metres -> Lambert 93, pushed out by the pad
        gx, gy = float(r), float(c)
        px = a.pad * (1 if gx >= centre[0] else -1) if a.pad else 0.0
        py = a.pad * (1 if gy >= centre[1] else -1) if a.pad else 0.0
        coords.append([ox + float(mn[0]) + gx * cell + px,
                       oy + float(mn[1]) + gy * cell + py])
    if coords[0] != coords[-1]:
        coords.append(coords[0])

    Path(a.out).write_text(json.dumps(
        {"type": "Polygon", "coordinates": [coords]}), encoding="utf-8")

    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    # shoelace, to say how much ground the crop will actually read
    area = 0.5 * abs(sum(xs[i] * ys[i + 1] - xs[i + 1] * ys[i]
                         for i in range(len(coords) - 1)))
    print(f"bounds {min(xs):.0f},{min(ys):.0f} .. {max(xs):.0f},{max(ys):.0f}")
    print(f"encloses {area/1e6:.2f} km2 against {grid.sum()*cell*cell/1e6:.2f} km2 carved")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
