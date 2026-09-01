"""Footprint of a carved point cloud, as polygons PDAL can crop with.

The proxy is where the shape is decided: Tiphaine frames and carves a light
cloud in Blender, and what survives — outer edge, islands, holes she cut in the
middle — is the shape the dense build has to follow.

This turns that into a MultiPolygon. Three properties matter and each has been
got wrong at least once:

  * islands. Avoriaz is a foreground, a middle ground and a background, three
    separate pieces. Keeping only the largest throws two thirds of the scene
    away.
  * holes. A hole cut in the middle of the proxy is a deliberate deletion and
    must survive into the dense cloud.
  * closed rings. A hand carve pinches to one cell wide in places, which is
    where a turtle-style boundary walk gets stuck. Chaining cell edges does
    not care: every edge between a kept cell and an empty one is on the
    boundary, and the rest cancel.

Nothing here needs Blender, so it can be tested on a grid drawn by hand.
"""
import numpy as np

# Cell edges are wound the same way for every kept cell, so an edge shared by
# two kept cells appears once in each direction and the pair cancels. What
# survives bounds the shape. In (row, col) space this winding traces an outer
# boundary clockwise, so its shoelace area is NEGATIVE and a hole's is
# positive. Measured, not assumed: a solid 2x2 block gives -4.
CORNER_ORDER = ((0, 0), (0, 1), (1, 1), (1, 0))


def occupancy(xy, cell, pad=1):
    """Rasterise points to a boolean grid. Returns (grid, origin_of_grid)."""
    mn = xy.min(0) - cell * pad
    idx = np.floor((xy - mn) / cell).astype(np.int64)
    dims = idx.max(0) + 1 + pad
    grid = np.zeros(tuple(dims), dtype=bool)
    grid[idx[:, 0], idx[:, 1]] = True
    return grid, mn


def _shift_reduce(grid, radius, how):
    """Dilate (how=max) or erode (how=min) with a square of side 2r+1.

    Written with shifts rather than scipy because this runs inside Blender,
    whose Python has numpy but no scipy.
    """
    pad = radius
    fill = False if how is np.logical_or else True
    padded = np.pad(grid, pad, constant_values=fill)
    out = None
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            view = padded[pad + dx:pad + dx + grid.shape[0],
                          pad + dy:pad + dy + grid.shape[1]]
            out = view.copy() if out is None else how(out, view)
    return out


def close_gaps(grid, radius=1):
    """Bridge the odd empty cell inside the shape.

    A sparse proxy leaves pinholes that are an artefact of its own spacing, not
    holes the artist cut. Closing by a cell removes those while leaving a real
    deletion — which is many cells across — untouched.
    """
    if radius <= 0:
        return grid
    dilated = _shift_reduce(grid, radius, np.logical_or)
    return _shift_reduce(dilated, radius, np.logical_and)


def rings(grid):
    """Every closed boundary ring of the grid, in grid-corner coordinates."""
    rows, cols = np.nonzero(grid)
    stride = grid.shape[1] + 1
    edges = set()
    for r, c in zip(rows.tolist(), cols.tolist()):
        corners = [(r + dr) * stride + (c + dc) for dr, dc in CORNER_ORDER]
        for a, b in zip(corners, corners[1:] + corners[:1]):
            if (b, a) in edges:
                edges.discard((b, a))
            else:
                edges.add((a, b))

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
        loops.append([(v // stride, v % stride) for v in loop])
    return loops


def signed_area(ring):
    return 0.5 * sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:]))


def contains(ring, point):
    """Ray cast, so a hole can be attached to the ring that encloses it."""
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xin:
                inside = not inside
    return inside


def group(loops, min_cells=1, min_hole_cells=1):
    """Sort rings into [outer, hole, hole, ...] groups.

    Two size rules, and they are not the same thing. `min_cells` drops specks:
    a stray vertex left behind by a carve should not drag the crop boundary
    across the scene for a handful of points. `min_hole_cells` ignores
    pinholes: a one-cell gap is the proxy's own spacing showing through, while
    a hole the artist cut is metres across.

    This replaced a morphological closing, which could not tell those apart -
    closing with a 3x3 filled a 2x2 deletion, quietly undoing a real edit.
    """
    outers, holes = [], []
    for ring in loops:
        area = signed_area(ring)
        if area < 0:
            if abs(area) >= min_cells:
                outers.append(ring)
        elif area >= min_hole_cells:
            holes.append(ring)

    groups = [[o] for o in outers]
    for h in holes:
        probe = h[0]
        # smallest enclosing outer ring, so a hole inside an island lands there
        best, best_area = None, None
        for g in groups:
            if contains(g[0], probe):
                a = abs(signed_area(g[0]))
                if best_area is None or a < best_area:
                    best, best_area = g, a
        if best is not None:
            best.append(h)
    return groups


def to_geojson(groups, grid_origin, cell, origin_xy):
    """Grid rings -> a Lambert 93 MultiPolygon.

    Grid axis 0 is X and axis 1 is Y: occupancy() was fed (x, y) pairs, so no
    transposition happens anywhere in here.
    """
    ox, oy = origin_xy

    def ring_coords(ring):
        pts = [[ox + grid_origin[0] + r * cell, oy + grid_origin[1] + c * cell]
               for r, c in ring]
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        return pts

    return {"type": "MultiPolygon",
            "coordinates": [[ring_coords(r) for r in g] for g in groups]}


def multipolygon_wkt(geom):
    """GeoJSON Polygon or MultiPolygon -> the WKT PDAL's filters.crop wants."""
    if geom["type"] == "Polygon":
        polys = [geom["coordinates"]]
    elif geom["type"] == "MultiPolygon":
        polys = geom["coordinates"]
    else:
        raise ValueError(f"expected Polygon or MultiPolygon, got {geom['type']!r}")

    def ring(r):
        return "(" + ", ".join(f"{x} {y}" for x, y in r) + ")"

    body = ", ".join("(" + ", ".join(ring(r) for r in poly) + ")" for poly in polys)
    return f"MULTIPOLYGON ({body})"


def area_str(m2):
    """Square metres until they are worth calling square kilometres.

    A 240 m2 scrap printed as "0.000 km2" is a number that hides itself, which
    is how two strays got reported as islands next to a 2.4 km2 carve.
    """
    return f"{m2:,.0f} m2" if m2 < 100_000 else f"{m2/1e6:.3f} km2"


def describe(groups, cell):
    """One line per island, for a log that says what was actually kept."""
    out = []
    for i, g in enumerate(groups, 1):
        area = abs(signed_area(g[0])) * cell * cell
        holes = sum(abs(signed_area(h)) for h in g[1:]) * cell * cell
        out.append(f"  island {i}: {area_str(area)}"
                   + (f" less {len(g)-1} hole(s) of {area_str(holes)}"
                      if len(g) > 1 else ""))
    return "\n".join(out)


def dropped_fragments(loops, cell, min_cells):
    """Outer rings too small to keep, with their size and where they sit.

    Reported rather than silently discarded: a fragment is usually a scrap of
    proxy a carve left behind, but whether it was meant is the artist's call.
    """
    out = []
    for ring in loops:
        area = signed_area(ring)
        if area < 0 and abs(area) < min_cells:
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
            out.append({"area": abs(area) * cell * cell,
                        "size": ((max(xs) - min(xs)) * cell,
                                 (max(ys) - min(ys)) * cell),
                        "at": ((min(xs) + max(xs)) / 2 * cell,
                               (min(ys) + max(ys)) / 2 * cell)})
    return sorted(out, key=lambda d: -d["area"])
