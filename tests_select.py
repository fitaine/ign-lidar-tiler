"""Check the selection: many shapes, tile sets, and what the index already knows.

A selection used to be one drawn ring, which could say "this ground" and
nothing else. It is now a SET OF TILES, built by adding and subtracting
gestures, and the shapes survive only as the carve. None of that needs IGN to
be reachable to test — the WFS answer is stubbed, because what is being
checked is the bookkeeping, not the network.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_tiles
import densify_tiled
import server


def check(label, ok):
    print(f"{'ok  ' if ok else 'FAIL'}  {label}")
    return 0 if ok else 1


def square(x, y, side=1000):
    """A tile footprint, closed, anticlockwise from the SW corner."""
    return [[x, y], [x + side, y], [x + side, y + side], [x, y + side], [x, y]]


def feature(x, y, points=1_000_000):
    name = f"LHD_FXX_{int(x/1000):04d}_{int((y+1000)/1000):04d}_PTS.copc.laz"
    return {"type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [square(x, y)]},
            "properties": {"name": name, "points": points,
                           "url": server.DOWNLOAD_HOST + "download/" + name}}


# ── shapes in, rings out ────────────────────────────────────────────────────
def test_rings_of():
    failures = 0
    one = {"type": "Polygon", "coordinates": [square(0, 0)]}
    two = {"type": "MultiPolygon",
           "coordinates": [[square(0, 0)], [square(9000, 0)]]}
    failures += check("a Polygon is one ring", len(fetch_tiles.rings_of(one)) == 1)
    failures += check("a MultiPolygon is two", len(fetch_tiles.rings_of(two)) == 2)
    failures += check("a Feature unwraps",
                      len(fetch_tiles.rings_of(
                          {"type": "Feature", "geometry": two})) == 2)
    failures += check("a FeatureCollection unwraps",
                      len(fetch_tiles.rings_of(
                          {"type": "FeatureCollection", "features": [
                              {"type": "Feature", "geometry": one},
                              {"type": "Feature", "geometry": two}]})) == 3)
    try:
        fetch_tiles.rings_of({"type": "LineString", "coordinates": []})
        failures += check("a LineString is refused", False)
    except ValueError:
        failures += check("a LineString is refused", True)
    return failures


# ── a shape far from another does not drag the gap in with it ───────────────
def test_list_tiles_for():
    """A foreground here and a background eight kilometres away.

    The whole point of asking shape by shape: one query over the combined
    rectangle would return the 27 tiles between them as well.
    """
    asked = []

    def stub(bbox):
        asked.append(bbox)
        # every tile whose square overlaps the asked rectangle
        out = []
        for gy in range(6570000, 6584000, 1000):
            for gx in range(990000, 994000, 1000):
                if gx < bbox[2] and gx + 1000 > bbox[0] \
                        and gy < bbox[3] and gy + 1000 > bbox[1]:
                    out.append(feature(gx, gy))
        return out

    real, fetch_tiles.list_tiles = fetch_tiles.list_tiles, stub
    try:
        fg = square(990100, 6572100, 1800)
        bg = square(990100, 6580100, 1800)
        got = fetch_tiles.list_tiles_for([fg, bg])
    finally:
        fetch_tiles.list_tiles = real

    failures = 0
    failures += check("one query per shape", len(asked) == 2)
    failures += check(f"{len(got)} tiles, not the gap as well", len(got) == 8)
    ys = {f["properties"]["name"].split("_")[3] for f in got}
    failures += check("nothing from between the two shapes",
                      not {"6576", "6577", "6578"} & ys)

    # Two shapes that overlap must not hand the same tile back twice.
    real, fetch_tiles.list_tiles = fetch_tiles.list_tiles, stub
    try:
        got = fetch_tiles.list_tiles_for([square(990100, 6572100, 1800),
                                          square(990600, 6572600, 1800)])
    finally:
        fetch_tiles.list_tiles = real
    names = [f["properties"]["name"] for f in got]
    failures += check("an overlap is not counted twice",
                      len(names) == len(set(names)))
    return failures


# ── what the index says, before anything is downloaded ──────────────────────
def test_weigh():
    feats = [feature(0, 0, 100_000_000), feature(1000, 0, 44_000_000)]
    points, area = fetch_tiles.weigh(feats)
    failures = 0
    failures += check("returns are summed", points == 144_000_000)
    failures += check("area is read, not assumed", abs(area - 2e6) < 1)
    # 144M returns over 2 km2 is 72 per m2, so about 12 cm apart.
    failures += check("mean spacing follows sqrt(area/points)",
                      abs(fetch_tiles.native_spacing(points, area) - 0.1179) < 1e-3)
    failures += check("an empty selection has no spacing",
                      fetch_tiles.native_spacing(0, 1e6) == 0.0)
    return failures


# ── the tile set arrives over HTTP, so it is checked ────────────────────────
def test_clean_tiles():
    good = [{"name": "LHD_FXX_0900_6420_PTS.copc.laz",
             "url": server.DOWNLOAD_HOST + "download/x.copc.laz",
             "points": 5, "area": 1e6}]
    failures = 0
    failures += check("a real tile passes", len(server.clean_tiles(good)) == 1)
    for label, bad in [
        ("somewhere else's URL", [dict(good[0], url="https://evil.test/x.copc.laz")]),
        ("a path in the filename", [dict(good[0], name="../../x.copc.laz")]),
        ("not a tile at all", [dict(good[0], name="x.exe")]),
        ("an empty selection", []),
    ]:
        try:
            server.clean_tiles(bad)
            failures += check(f"{label} is refused", False)
        except ValueError:
            failures += check(f"{label} is refused", True)
    return failures


# ── subtracting is not the same as never having drawn it ────────────────────
def test_carve_out():
    keep = "MULTIPOLYGON (((0 0, 1 0, 1 1, 0 1, 0 0)))"
    cut = "MULTIPOLYGON (((0 0, 0.5 0, 0.5 0.5, 0 0.5, 0 0)))"
    pipe = densify_tiled.tile_pipeline(
        Path("t.copc.laz"), None, 0.5, (0, 0, 0), (0, 0, 10, 10),
        Path("out.ply"), polygon=keep, polygon_out=cut)["pipeline"]
    crops = [st for st in pipe if st["type"] == "filters.crop"]
    failures = 0
    failures += check("bounds, keep and cut are three crops", len(crops) == 3)
    failures += check("the kept shape crops inside",
                      crops[1].get("polygon") == keep and not crops[1].get("outside"))
    failures += check("the cut shape crops OUTSIDE",
                      crops[2].get("polygon") == cut and crops[2].get("outside") is True)
    failures += check("the cut comes after the keep",
                      pipe.index(crops[2]) > pipe.index(crops[1]))

    plain = densify_tiled.tile_pipeline(
        Path("t.copc.laz"), None, 0.5, (0, 0, 0), (0, 0, 10, 10),
        Path("out.ply"), polygon=keep)["pipeline"]
    failures += check("nothing subtracted means no second crop",
                      sum(1 for st in plain if st["type"] == "filters.crop") == 2)
    return failures


# ── a MultiPolygon carve survives the round trip to PDAL ────────────────────
def test_multipolygon_carve():
    gj = {"type": "MultiPolygon",
          "coordinates": [[square(0, 0)], [square(9000, 0)]]}
    with tempfile.TemporaryDirectory() as wd:
        f = Path(wd) / "carve.geojson"
        f.write_text(json.dumps(gj), encoding="utf-8")
        wkt = densify_tiled.load_footprint(str(f))
    return check("both shapes reach filters.crop",
                 wkt.startswith("MULTIPOLYGON") and wkt.count("((") == 2)


def main():
    failures = 0
    print("--- shapes ---")
    failures += test_rings_of()
    print("\n--- selecting shape by shape ---")
    failures += test_list_tiles_for()
    print("\n--- what the index knows ---")
    failures += test_weigh()
    print("\n--- the tile set is checked, not trusted ---")
    failures += test_clean_tiles()
    print("\n--- add and subtract reach the carve ---")
    failures += test_carve_out()
    failures += test_multipolygon_carve()
    print("\nall good" if not failures else f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
