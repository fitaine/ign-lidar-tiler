"""Find and download IGN LiDAR HD tiles for a footprint.

Stage 1a of the IGN LiDAR Tiler (see PLAN.md):

    python fetch_tiles.py --bbox 900000,6418000,903000,6420000 --dry-run
    python fetch_tiles.py --bbox 900000,6418000,903000,6420000 --out ./tiles

The tile index is the Geoplateforme WFS layer
`IGNF_NUAGES-DE-POINTS-LIDAR-HD:dalle`. Each feature carries the 1 km tile
footprint in Lambert 93 and a direct download URL. Verified 2026-08-31 against
tiles already on disk for Mont Aiguille.

Selection is by bounding box or by an arbitrary polygon (a GeoJSON file, which
is what the map's free-shape draw will produce). Either way the unit of
download is a whole 1 km tile, so the polygon selects tiles rather than
clipping them; clipping to the exact shape is a later PDAL step.
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

WFS = "https://data.geopf.fr/wfs/ows"
LAYER = "IGNF_NUAGES-DE-POINTS-LIDAR-HD:dalle"
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
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def list_tiles(bbox):
    out, start = [], 0
    while True:
        data = wfs_page(bbox, start)
        feats = data.get("features", [])
        out.extend(feats)
        if len(feats) < PAGE:
            break
        start += PAGE
    return out


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
    req = urllib.request.Request(url, method="HEAD", headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return int(r.headers.get("Content-Length", 0))
    except Exception:
        return 0


def head_sizes(urls, workers=12):
    """Size many tiles at once. Serially this took ~15 s for 20 tiles, which is
    long enough to feel broken while dragging a selection on the map."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(head_size, urls))


def download(url, dest):
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length", 0))
        got = 0
        while True:
            block = r.read(1 << 20)
            if not block:
                break
            f.write(block)
            got += len(block)
            if total:
                print(f"\r  {dest.name}  {got/1e6:7.1f} / {total/1e6:.1f} MB", end="", flush=True)
    print()
    tmp.replace(dest)
    return dest.stat().st_size


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

    poly = None
    if a.geojson:
        gj = json.loads(Path(a.geojson).read_text(encoding="utf-8"))
        geom = gj["geometry"] if gj.get("type") == "Feature" else gj
        if geom["type"] != "Polygon":
            sys.exit("--geojson needs a single Polygon")
        poly = geom["coordinates"][0]
        xs = [q[0] for q in poly]
        ys = [q[1] for q in poly]
        bbox = (min(xs), min(ys), max(xs), max(ys))
    else:
        bbox = tuple(float(v) for v in a.bbox.split(","))
        if len(bbox) != 4:
            sys.exit("--bbox needs minx,miny,maxx,maxy")

    print(f"querying the IGN LiDAR HD tile index over "
          f"{bbox[0]:.0f},{bbox[1]:.0f} .. {bbox[2]:.0f},{bbox[3]:.0f} ...", flush=True)
    feats = list_tiles(bbox)
    if poly is not None:
        before = len(feats)
        feats = [f for f in feats if tile_intersects_polygon(f, poly)]
        print(f"polygon filter: {before} -> {len(feats)} tiles", flush=True)
    if not feats:
        sys.exit("no tiles found for that footprint")

    feats.sort(key=lambda f: f["properties"]["name"])
    urls = [f["properties"]["url"] for f in feats]
    sizes = head_sizes(urls)
    rows = []
    total = 0
    for f, url, size in zip(feats, urls, sizes):
        total += size
        rows.append({"name": Path(urllib.parse.urlparse(url).path).name,
                     "url": url, "bytes": size,
                     "id": f["properties"].get("id"), "ring": ring_of(f)})

    print(f"\n{len(rows)} tiles, {len(rows)} km2, {total/1e9:.2f} GB to download\n")
    for r in rows:
        print(f"  {r['name']:<48} {r['bytes']/1e6:8.1f} MB")

    if a.manifest:
        Path(a.manifest).write_text(json.dumps(
            {"bbox": bbox, "tiles": [{k: r[k] for k in ("name", "url", "bytes", "id")}
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
