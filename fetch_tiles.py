"""Find and download IGN LiDAR HD tiles for a footprint.

Stage 1a of the IGN LiDAR Tiler (see PLAN.md):

    python fetch_tiles.py --bbox 900000,6418000,903000,6420000 --dry-run
    python fetch_tiles.py --bbox 900000,6418000,903000,6420000 --out ./tiles

The tile index is the Geoplateforme WFS layer
`IGNF_LIDAR-HD_METADONNEE:metadata`. Each feature carries the 1 km tile
footprint in Lambert 93 and a direct download URL. Verified 2026-08-31 against
tiles already on disk for Mont Aiguille.

IGN retired the old `IGNF_NUAGES-DE-POINTS-LIDAR-HD:dalle` layer some time
before 2026-09-11; asking for it answers HTTP 400 "Unknown namespace", which
is what the map grid and every selection turned into. The replacement carries
the same footprints under different property names - the point cloud URL is
`url_npl`, and there is no `name` - so `list_tiles` fills in the `name`/`url`
pair the rest of the app reads.

Selection is by bounding box or by an arbitrary polygon (a GeoJSON file, which
is what the map's free-shape draw will produce). Either way the unit of
download is a whole 1 km tile, so the polygon selects tiles rather than
clipping them; clipping to the exact shape is a later PDAL step.
"""

import argparse
import json
import sys
import urllib.parse
from pathlib import Path

import net

WFS = "https://data.geopf.fr/wfs/ows"
LAYER = "IGNF_LIDAR-HD_METADONNEE:metadata"
PAGE = 1000
UA = {"User-Agent": "ign-lidar-tiler/0.1 (+https://github.com/fitaine/ign-lidar-tiler)"}


def wfs_page(bbox, start):
    q = {
        "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
        "TYPENAMES": LAYER, "SRSNAME": "EPSG:2154",
        "BBOX": f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]},EPSG:2154",
        "COUNT": str(PAGE), "STARTINDEX": str(start),
        "OUTPUTFORMAT": "application/json",
    }
    url = f"{WFS}?{urllib.parse.urlencode(q)}"
    return json.loads(net.get(url, timeout=120).decode("utf-8"))


def normalise(feature):
    """Give a feature the `name`/`url`/`id`/`points` the rest of the app expects.

    `points` is IGN's own count of the returns in that tile, which means the
    weight of a selection is known before a byte of it is downloaded.
    """
    props = feature["properties"]
    if not props.get("url"):
        url = props.get("url_npl") or ""
        props["url"] = url
        props["name"] = Path(urllib.parse.urlparse(url).path).name
        props.setdefault("id", feature.get("id"))
    props.setdefault("points", int(props.get("nombre_points") or 0))
    return feature


def list_tiles(bbox):
    out, start = [], 0
    while True:
        data = wfs_page(bbox, start)
        feats = data.get("features", [])
        out.extend(normalise(f) for f in feats)
        if len(feats) < PAGE:
            break
        start += PAGE
    return [f for f in out if f["properties"]["url"]]


def tile_area(feature):
    """Ground area of one tile in m2. Normally 1 km2, but read rather than
    assumed: `points` divided by this is what gives the density."""
    ring = ring_of(feature)
    xs = [p[0] for p in ring[:-1]]
    ys = [p[1] for p in ring[:-1]]
    return (max(xs) - min(xs)) * (max(ys) - min(ys))


def weigh(feats):
    """(points, area_m2) over a set of tiles."""
    return (sum(f["properties"].get("points") or 0 for f in feats),
            sum(tile_area(f) for f in feats))


def native_spacing(points, area_m2):
    """Mean distance between returns, sqrt(area / points).

    The floor under any voxel: below it the cells are finer than the scan
    itself, so shrinking them adds isolated specks rather than surface.
    """
    if points <= 0 or area_m2 <= 0:
        return 0.0
    return (area_m2 / float(points)) ** 0.5


def ring_of(feature):
    g = feature["geometry"]
    if g["type"] == "MultiPolygon":
        return g["coordinates"][0][0]
    return g["coordinates"][0]


def centroid(ring):
    xs = [p[0] for p in ring[:-1]]
    ys = [p[1] for p in ring[:-1]]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def point_in_ring(x, y, ring):
    """Ray casting. Ring is a closed list of [x, y]."""
    inside = False
    n = len(ring) - 1
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[i + 1][0], ring[i + 1][1]
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xin:
                inside = not inside
    return inside


def rings_of(gj):
    """Every outer ring in a GeoJSON value, as a list of rings.

    A selection is not always one blob. A foreground and a background are two
    shapes with untouched ground between them, and drawing one rectangle round
    both would download every tile in the gap. Polygon, MultiPolygon, Feature
    and FeatureCollection all reduce to the same list here.
    """
    if gj.get("type") == "FeatureCollection":
        out = []
        for f in gj.get("features", []):
            out.extend(rings_of(f))
        return out
    geom = gj.get("geometry") if gj.get("type") == "Feature" else gj
    if geom is None:
        return []
    if geom["type"] == "Polygon":
        return [geom["coordinates"][0]]
    if geom["type"] == "MultiPolygon":
        return [poly[0] for poly in geom["coordinates"]]
    raise ValueError(f"expected Polygon or MultiPolygon, got {geom['type']!r}")


def bbox_of(poly):
    xs = [q[0] for q in poly]
    ys = [q[1] for q in poly]
    return (min(xs), min(ys), max(xs), max(ys))


def list_tiles_for(polys):
    """Tiles touched by ANY of the shapes, asked for shape by shape.

    One query over the combined bounding box would work, but for the case this
    exists to serve - a foreground here, a background eight kilometres away -
    it asks IGN for the whole rectangle between them, and then throws almost
    all of it away. A query per shape keeps both the question and the answer
    the size of what was actually drawn.
    """
    seen, out = set(), []
    for poly in polys:
        for f in list_tiles(bbox_of(poly)):
            name = f["properties"]["name"]
            if name in seen or not tile_intersects_polygon(f, poly):
                continue
            seen.add(name)
            out.append(f)
    out.sort(key=lambda f: f["properties"]["name"])
    return out


def tile_intersects_polygon(feature, poly):
    """A 1 km tile is kept if any of its corners or its centre is inside the
    polygon, or any polygon vertex falls inside the tile. Cheap and adequate
    at this scale: tiles are 1 km, drawn shapes are far larger."""
    ring = ring_of(feature)
    xs = [p[0] for p in ring[:-1]]
    ys = [p[1] for p in ring[:-1]]
    tminx, tmaxx, tminy, tmaxy = min(xs), max(xs), min(ys), max(ys)
    pts = [(tminx, tminy), (tmaxx, tminy), (tmaxx, tmaxy), (tminx, tmaxy),
           ((tminx + tmaxx) / 2, (tminy + tmaxy) / 2)]
    if any(point_in_ring(px, py, poly) for px, py in pts):
        return True
    return any(tminx <= vx <= tmaxx and tminy <= vy <= tmaxy for vx, vy in poly[:-1])


def head_size(url):
    return net.head_size(url)


def head_sizes(urls, workers=12):
    """Size many tiles at once. Serially this took ~15 s for 20 tiles, which is
    long enough to feel broken while dragging a selection on the map."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(head_size, urls))


def download(url, dest, **kwargs):
    """Fetch one tile. Thin wrapper over net.download, which owns the logic.

    There used to be a second implementation here: no resume, no retry, and a
    signature that did not accept `expected`. acquire.py called this one, so
    every tile failed instantly with "download() got an unexpected keyword
    argument 'expected'" - a whole 25-tile job lost to two functions with the
    same name drifting apart. One implementation now, and this name still
    works for anything that already calls it.
    """
    import net

    return net.download(url, dest, **kwargs)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--bbox", help="minx,miny,maxx,maxy in Lambert 93 (EPSG:2154)")
    g.add_argument("--geojson", help="GeoJSON file with one Polygon, Lambert 93")
    p.add_argument("--out", default=None, help="Download folder")
    p.add_argument("--dry-run", action="store_true",
                   help="List the tiles and the download size, fetch nothing")
    p.add_argument("--manifest", default=None, help="Write the tile list as JSON")
    a = p.parse_args()

    polys = None
    if a.geojson:
        gj = json.loads(Path(a.geojson).read_text(encoding="utf-8"))
        try:
            polys = rings_of(gj)
        except ValueError as ex:
            sys.exit(str(ex))
        if not polys:
            sys.exit("--geojson holds no polygon")
    else:
        bbox = tuple(float(v) for v in a.bbox.split(","))
        if len(bbox) != 4:
            sys.exit("--bbox needs minx,miny,maxx,maxy")
        polys = [[[bbox[0], bbox[1]], [bbox[2], bbox[1]], [bbox[2], bbox[3]],
                  [bbox[0], bbox[3]], [bbox[0], bbox[1]]]]

    shapes = f"{len(polys)} shapes" if len(polys) > 1 else "1 shape"
    print(f"querying the IGN LiDAR HD tile index over {shapes} ...", flush=True)
    feats = list_tiles_for(polys)
    if not feats:
        sys.exit("no tiles found for that footprint")
    urls = [f["properties"]["url"] for f in feats]
    sizes = head_sizes(urls)
    rows = []
    total = 0
    for f, url, size in zip(feats, urls, sizes):
        total += size
        rows.append({"name": Path(urllib.parse.urlparse(url).path).name,
                     "url": url, "bytes": size,
                     "id": f["properties"].get("id"), "ring": ring_of(f)})

    pts, area = weigh(feats)
    spacing = native_spacing(pts, area)
    print(f"\n{len(rows)} tiles, {area/1e6:.0f} km2, {total/1e9:.2f} GB to download")
    print(f"{pts:,} returns, {pts/max(area,1):.0f} per m2, "
          f"mean spacing {spacing:.2f} m\n")
    for r in rows:
        print(f"  {r['name']:<48} {r['bytes']/1e6:8.1f} MB")

    if a.manifest:
        Path(a.manifest).write_text(json.dumps(
            {"bbox": [min(bbox_of(q)[0] for q in polys),
                      min(bbox_of(q)[1] for q in polys),
                      max(bbox_of(q)[2] for q in polys),
                      max(bbox_of(q)[3] for q in polys)], "tiles": [{k: r[k] for k in ("name", "url", "bytes", "id")}
                                     for r in rows]}, indent=2), encoding="utf-8")
        print(f"\nwrote {a.manifest}")

    if a.dry_run:
        return
    if not a.out:
        sys.exit("--out is required unless --dry-run")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    print()
    for r in rows:
        dest = out / r["name"]
        if dest.is_file() and (r["bytes"] == 0 or dest.stat().st_size == r["bytes"]):
            print(f"  {dest.name}  already present, skipping")
            continue
        download(r["url"], dest)
    print(f"\ndone, {len(rows)} tiles in {out}")


if __name__ == "__main__":
    main()
