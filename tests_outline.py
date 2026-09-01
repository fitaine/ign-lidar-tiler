"""Check the footprint geometry on shapes drawn by hand.

Every case here is one that broke, or would have: three separate islands like
Avoriaz, a hole cut in the middle of a carve, a hole inside an island, and a
shape that pinches to one cell wide where a boundary walk gets stuck.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import outline


def grid_from(rows):
    """'.#' art -> boolean grid, indexed [x][y] the way occupancy() produces."""
    return np.array([[c == "#" for c in row] for row in rows], dtype=bool)


def polygons(grid, min_cells=1):
    return outline.group(outline.rings(grid), min_cells=min_cells)


def check(label, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    return 0 if ok else 1


def main():
    f = 0

    print("--- one solid block ---")
    g = polygons(grid_from(["....", ".##.", ".##.", "...."]))
    f += check("one island, no holes", len(g) == 1 and len(g[0]) == 1)
    f += check("area is 4 cells", abs(outline.signed_area(g[0][0])) == 4)

    print("\n--- three islands, like Avoriaz fg/mid/bg ---")
    g = polygons(grid_from([
        "#.#.#",
        "#.#.#",
        ".....",
        "#####",
    ]))
    f += check("islands are not thrown away", len(g) == 4, f"found {len(g)}")

    print("\n--- a hole cut in the middle ---")
    g = polygons(grid_from([
        "#####",
        "#...#",
        "#...#",
        "#...#",
        "#####",
    ]))
    ok = len(g) == 1 and len(g[0]) == 2
    f += check("outer ring plus one hole", ok, f"rings={[len(x) for x in g]}")
    if ok:
        outer, hole = g[0]
        f += check("hole is inside its outer ring",
                   outline.contains(outer, hole[0]))
        f += check("hole winds the other way",
                   outline.signed_area(outer) < 0 < outline.signed_area(hole))

    print("\n--- an island standing inside a hole ---")
    # a ring carved out, with something left standing in the middle of it
    g = polygons(grid_from([
        "#####",
        "#...#",
        "#.#.#",
        "#...#",
        "#####",
    ]))
    shapes = sorted(len(x) for x in g)
    f += check("two shapes: the ring with its hole, and the island inside",
               shapes == [1, 2], f"rings per shape {shapes}")

    print("\n--- a hole inside one island, another island beside it ---")
    g = polygons(grid_from([
        "#####.###",
        "#...#.###",
        "#####.###",
    ]))
    with_hole = [x for x in g if len(x) > 1]
    f += check("two islands", len(g) == 2, f"found {len(g)}")
    f += check("the hole lands in the island that encloses it", len(with_hole) == 1)

    print("\n--- pinch point, one cell wide ---")
    g = polygons(grid_from([
        "###..",
        "..#..",
        "..###",
    ]))
    f += check("still closes into one ring", len(g) == 1 and len(g[0]) == 1)

    print("\n--- specks are dropped ---")
    g = polygons(grid_from([
        "####.",
        "####.",
        "....#",
    ]), min_cells=2)
    f += check("a single stray cell is ignored", len(g) == 1, f"found {len(g)}")

    print("\n--- occupancy and coordinates round-trip ---")
    xy = np.array([[10.0, 20.0], [10.5, 20.5], [13.0, 20.0], [10.0, 23.0]])
    grid, mn = outline.occupancy(xy, cell=1.0)
    f += check("every point lands in a kept cell",
               all(grid[int((p[0]-mn[0])//1.0), int((p[1]-mn[1])//1.0)] for p in xy))
    gj = outline.to_geojson(polygons(grid), mn, 1.0, (1000.0, 2000.0))
    f += check("geojson is a MultiPolygon", gj["type"] == "MultiPolygon")
    xs = [p[0] for poly in gj["coordinates"] for r in poly for p in r]
    f += check("coordinates are in Lambert 93", min(xs) > 1000.0, f"min x {min(xs):.1f}")

    print("\n--- WKT for PDAL ---")
    wkt = outline.multipolygon_wkt(gj)
    f += check("starts as a MULTIPOLYGON", wkt.startswith("MULTIPOLYGON ((("))
    holey = {"type": "MultiPolygon", "coordinates": [
        [[[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
         [[1, 1], [1, 2], [2, 2], [2, 1], [1, 1]]]]}
    w = outline.multipolygon_wkt(holey)
    f += check("a hole becomes a second ring", w.count("(") >= 4 and "1 1" in w)

    print("\nall good" if not f else f"\n{f} failure(s)")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
