"""Acquire a scene: select tiles, download, fetch the ortho, build the sparse PLY.

Stage 1a of the IGN LiDAR Tiler (see PLAN.md):

    python acquire.py --name mont-aiguille --bbox 900100,6418100,902900,6419900 \
        --target 40000000 --out "D:/scenes/mont-aiguille"

Writes everything into one scene folder, with `scene.json` as the single
source of truth. Filenames keep the `-NNN` radius suffix so existing habits
still read correctly, but nothing downstream parses a filename.

The ortho fetch is reused from lidar_pipeline.py rather than reimplemented:
its tiled WMS logic is proven on 35 scenes.
"""

import argparse
import importlib.util
import json
import subprocess

import winrun
import sys
from datetime import date
from pathlib import Path

import fetch_tiles
import net
from densify import radius_for, suffix_for

PDAL_EXE = r"C:\Program Files\QGIS 3.40.5\bin\pdal.exe"
PIPELINE_PY = Path(__file__).resolve().parent.parent / "lidar_pipeline.py"


def load_pipeline_module():
    """Import lidar_pipeline.py from the parent LIDAR PROJECT folder."""
    if not PIPELINE_PY.is_file():
        sys.exit(f"lidar_pipeline.py not found at {PIPELINE_PY}")
    spec = importlib.util.spec_from_file_location("lidar_pipeline", PIPELINE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tile_summary(path):
    r = winrun.run([PDAL_EXE, "info", "--summary", str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"pdal info failed on {path.name}: {r.stderr[:300]}")
    return json.loads(r.stdout)["summary"]


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--bbox", help="minx,miny,maxx,maxy in Lambert 93")
    g.add_argument("--geojson", help="GeoJSON Polygon or MultiPolygon, Lambert 93")
    # The map selects tiles, not ground: clicking one, lassoing a dozen and
    # subtracting three is a set that no single shape describes. So it says
    # which tiles, and the drawn shapes come separately as the carve.
    g.add_argument("--tiles-json", help="JSON list of the tiles to fetch, each "
                                        "{name, url, points}. What the map's "
                                        "selection sends.")
    p.add_argument("--name", required=True, help="Scene name")
    p.add_argument("--out", required=True, help="Scene folder")
    p.add_argument("--target", type=int, default=40_000_000,
                   help="Sparse target point count (default 40M)")
    p.add_argument("--voxel", type=float, default=None,
                   help="Skip solving and use this voxel")
    p.add_argument("--origin", default=None,
                   help="Force the origin, X,Y,Z in Lambert 93, instead of deriving "
                        "it from the tiles. This is how you EXTEND an existing "
                        "scene: pass its origin and a wider footprint, and the new "
                        "cloud lands in the same local frame, so the cameras and "
                        "lighting in the .blend stay valid.")
    p.add_argument("--raster-res", type=float, default=0.20,
                   help="Ortho resolution m/px (default 0.20, IGN native)")
    p.add_argument("--layer", default="ortho", help="WMS layer shortcut")
    p.add_argument("--multiplier", type=float, default=1.0,
                   help="Ball radius multiplier, 1.0 = spheres touch")
    p.add_argument("--skip-download", action="store_true",
                   help="Tiles are already present")
    # A scene that predates the app already HAS its light cloud: it is sitting
    # in the .blend, framed and carved years ago. Building a second one just to
    # satisfy the manifest costs a PDAL pass and a gigabyte for nothing.
    p.add_argument("--no-sparse", action="store_true",
                   help="Do not build a sparse PLY. For adopting an old scene "
                        "whose proxy is already in its .blend: the tiles, the "
                        "ortho and the manifest are written, nothing else.")
    # Tiles forgotten on a scene that is already built: fetch them into its
    # archive and stop. The merge and the ortho are redone afterwards, and the
    # scene.json already there must not be overwritten by a half-built one.
    p.add_argument("--download-only", action="store_true",
                   help="Download and check the selected tiles, then stop. No "
                        "ortho, no PLY, no scene.json.")
    p.add_argument("--tiles-dir", default=None,
                   help="Existing tile archive to use instead of <out>/tiles")
    p.add_argument("--carve", default=None,
                   help="GeoJSON of the ground to keep, used with --tiles-json. "
                        "Only meaningful with --crop-to-shape.")
    p.add_argument("--carve-out", default=None,
                   help="GeoJSON of ground to cut back OUT of the carve")
    p.add_argument("--crop-to-shape", action="store_true",
                   help="Clip to the drawn polygon instead of keeping whole 1 km "
                        "tiles with a staircase edge (costs one extra PDAL pass)")
    p.add_argument("--dry-run", action="store_true",
                   help="Report the selection, extent and origin, then stop")
    a = p.parse_args()

    out = Path(a.out)
    tiles_dir = Path(a.tiles_dir) if a.tiles_dir else out / "tiles"
    out.mkdir(parents=True, exist_ok=True)
    tiles_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. select and download ───────────────────────────────────────────────
    # `sel` is the selection, one dict per tile: name, url, and the return
    # count IGN's index reports for it.
    carve = a.carve or (a.geojson if a.crop_to_shape else None)
    # The ground actually asked for, which is not the same as the ground the
    # tiles cover: an adopted scene names a footprint inside one tile, and the
    # ortho should follow the footprint. Kept separate from the tile extent.
    polys = want_bbox = None
    if a.tiles_json:
        sel = json.loads(Path(a.tiles_json).read_text(encoding="utf-8"))
        if isinstance(sel, dict):
            sel = sel.get("tiles", [])
        if not sel:
            sys.exit("--tiles-json holds no tiles")
        sel.sort(key=lambda t: t["name"])
    else:
        if a.geojson:
            gj = json.loads(Path(a.geojson).read_text(encoding="utf-8"))
            polys = fetch_tiles.rings_of(gj)
            if not polys:
                sys.exit("--geojson holds no polygon")
            feats = fetch_tiles.list_tiles_for(polys)
        else:
            want_bbox = tuple(float(v) for v in a.bbox.split(","))
            feats = fetch_tiles.list_tiles(want_bbox)
            feats.sort(key=lambda f: f["properties"]["name"])
        if not feats:
            sys.exit("no tiles found for that footprint")
        sel = [{"name": f["properties"]["name"], "url": f["properties"]["url"],
                "points": f["properties"].get("points") or 0,
                "area": fetch_tiles.tile_area(f)} for f in feats]

    if carve:
        polys = fetch_tiles.rings_of(
            json.loads(Path(carve).read_text(encoding="utf-8")))
    shapes = f" from {len(polys)} shapes" if polys and len(polys) > 1 else ""
    print(f"[acquire] {len(sel)} tiles selected{shapes}", flush=True)

    # ── what IGN says is in them, before a byte is downloaded ────────────────
    # The index carries a per-tile return count, so the two questions that used
    # to need the whole download to answer - is there enough here, and how fine
    # can the voxel go - are answerable now, while the selection can still be
    # redrawn.
    wfs_points = sum(t.get("points") or 0 for t in sel)
    wfs_area = sum(t.get("area") or 1e6 for t in sel)
    spacing = fetch_tiles.native_spacing(wfs_points, wfs_area)
    if wfs_points:
        print(f"[acquire] IGN says {wfs_points:,} returns over "
              f"{wfs_area/1e6:.0f} km2, {wfs_points/max(wfs_area,1):.0f} per m2, "
              f"mean spacing {spacing:.2f} m", flush=True)
        if not a.voxel and not a.no_sparse and not a.download_only                 and a.target > wfs_points:
            sys.exit(f"[acquire] the target of {a.target:,} points is more than "
                     f"the {wfs_points:,} returns these tiles hold. No voxel "
                     f"reaches it - lower the target or select more tiles. "
                     f"Nothing was downloaded.")
        if not a.voxel and not a.no_sparse and not a.download_only:
            print(f"[acquire] the finest voxel worth asking for here is about "
                  f"{spacing:.2f} m; below that the cells are finer than the "
                  f"scan", flush=True)

    # A preview has to answer BEFORE the download, not after it. This used to
    # sit past the download and the readability check, so --dry-run fetched
    # every gigabyte and only then reported what it would have fetched.
    if a.dry_run:
        urls = [t["url"] for t in sel]
        sizes = fetch_tiles.head_sizes(urls)
        here = sum(1 for url, sz in zip(urls, sizes)
                   if sz and (tiles_dir / Path(url.split("?")[0]).name).is_file()
                   and (tiles_dir / Path(url.split("?")[0]).name).stat().st_size == sz)
        total = sum(sz for sz in sizes if sz)
        print(f"[acquire] {total/1e9:.1f} GB to download"
              + (f", {here} tile(s) already here" if here else ""), flush=True)
        for t, sz in zip(sel, sizes):
            print(f"[acquire]   {t['name']}  {(sz or 0)/1e6:.0f} MB", flush=True)
        print(f"[acquire] dry run: nothing downloaded", flush=True)
        return

    if not a.skip_download:
        urls = [t["url"] for t in sel]
        sizes = fetch_tiles.head_sizes(urls)
        total_bytes = sum(s for s in sizes if s)
        already = sum(1 for url, s in zip(urls, sizes)
                      if s and (tiles_dir / Path(url.split("?")[0]).name).is_file()
                      and (tiles_dir / Path(url.split("?")[0]).name).stat().st_size == s)
        if total_bytes:
            print(f"[acquire] {total_bytes/1e9:.1f} GB to fetch from IGN"
                  + (f", {already} tile(s) already here" if already else ""),
                  flush=True)
        print(f"[acquire] downloads resume where they stop, so it is safe to "
              f"close this and run it again", flush=True)

        failed, done_bytes = [], 0
        for i, (url, size) in enumerate(zip(urls, sizes), 1):
            dest = tiles_dir / Path(url.split("?")[0]).name
            left = ((total_bytes - done_bytes) / 1e9) if total_bytes else 0
            print(f"[acquire]   [{i}/{len(urls)}] {dest.name}"
                  + (f"   {size/1e6:.0f} MB, {left:.1f} GB still to go"
                     if size else ""), flush=True)
            try:
                # net.download, not fetch_tiles.download: it resumes from a
                # .part file, retries the failures IGN actually produces, and
                # skips a tile that is already complete. The two had drifted
                # apart and acquire was still calling the old one, which does
                # not even take `expected` - every tile "GAVE UP" instantly.
                net.download(url, dest, expected=size or None,
                             log=lambda m: print(f"[acquire]   {m}", flush=True))
                done_bytes += size or 0
            except Exception as e:
                print(f"[acquire]      could not fetch this one: {e}", flush=True)
                failed.append(dest.name)

        if failed:
            print(f"\n[acquire] {len(urls) - len(failed)} of {len(urls)} tiles are "
                  f"here. {len(failed)} did not arrive:", flush=True)
            for name in failed:
                print(f"[acquire]     {name}", flush=True)
            sys.exit("[acquire] Run the same job again and it will pick up where "
                     "it left off - finished tiles are skipped and half-finished "
                     "ones continue. If they keep failing, IGN is probably down.")
        print(f"[acquire] all {len(urls)} tiles are here", flush=True)

    # A shared tile archive holds more than this scene. Build from the
    # selection, not from whatever happens to be in the folder.
    wanted = {t["name"] for t in sel}
    tiles = sorted(t for t in tiles_dir.glob("*.copc.laz") if t.name in wanted)
    if not tiles:
        sys.exit(f"no tiles in {tiles_dir}")
    missing = wanted - {t.name for t in tiles}
    if missing:
        sys.exit(f"[acquire] {len(missing)} selected tile(s) are not in "
                 f"{tiles_dir}: {sorted(missing)[:5]}")

    # Confirm every tile actually opens. A truncated download is accepted
    # silently by everything until PDAL fails on it much later.
    print(f"[acquire] checking {len(tiles)} tiles are readable ...", flush=True)
    bad = []
    for t in tiles:
        r = winrun.run([PDAL_EXE, "info", "--summary", str(t)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            bad.append(t.name)
    if bad:
        sys.exit(f"[acquire] these tiles are unreadable, delete them and re-run "
                 f"to fetch again: {bad}")

    # ── 2. scene extent and shared origin ────────────────────────────────────
    minx = miny = minz = float("inf")
    maxx = maxy = maxz = float("-inf")
    raw_total = 0
    tile_boxes = []
    for t in tiles:
        s = tile_summary(t)
        b = s["bounds"]
        raw_total += s["num_points"]
        tile_boxes.append((b["minx"], b["miny"], b["maxx"], b["maxy"]))
        minx = min(minx, b["minx"]); maxx = max(maxx, b["maxx"])
        miny = min(miny, b["miny"]); maxy = max(maxy, b["maxy"])
        minz = min(minz, b["minz"]); maxz = max(maxz, b["maxz"])
    bbox = (minx, miny, maxx, maxy)

    if a.origin:
        origin = [float(v) for v in a.origin.split(",")]
        if len(origin) != 3:
            sys.exit("--origin needs three comma-separated values")
        print(f"[acquire] using the given origin (extending an existing scene)", flush=True)
    else:
        # Her convention: X and Y floored to the kilometre, Z at the scene floor.
        origin = [float(int(minx // 1000) * 1000),
                  float(int(miny // 1000) * 1000), round(minz, 2)]
    print(f"[acquire] extent {maxx-minx:.0f} x {maxy-miny:.0f} m, "
          f"relief {maxz-minz:.0f} m, {raw_total:,} raw points", flush=True)
    print(f"[acquire] origin {origin}", flush=True)

    # ── 3. voxel ─────────────────────────────────────────────────────────────
    if a.voxel:
        voxel = a.voxel
        print(f"[acquire] voxel {voxel} (given)", flush=True)
    else:
        import solve_voxel
        print(f"[acquire] solving voxel for {a.target:,} points ...", flush=True)
        r = winrun.run([sys.executable, str(Path(__file__).parent / "solve_voxel.py"),
                            "--tiles", str(tiles_dir), "--target", str(a.target)],
                           capture_output=True, text=True)
        print(r.stdout, flush=True)
        if r.returncode != 0:
            sys.exit(f"solve_voxel failed: {r.stderr[:400]}")
        voxel = None
        for line in r.stdout.splitlines():
            if line.strip().startswith("--voxel"):
                voxel = float(line.split()[-1])
        if voxel is None:
            sys.exit("could not parse a voxel from solve_voxel")
        print(f"[acquire] voxel {voxel} (solved)", flush=True)

    radius = radius_for(voxel, a.multiplier)

    # ── 4. ortho ─────────────────────────────────────────────────────────────
    lp = load_pipeline_module()
    layer = lp.KNOWN_LAYERS.get(a.layer, a.layer)
    raster = out / f"{a.name}-{round(a.raster_res*100):03d}_raster.tif"

    # The ortho only has to cover the ground that will end up coloured. Whole
    # tiles are kept unless the build is clipped, so the tile extent is right
    # for a normal acquire - but an adopted scene, or one clipped to a drawn
    # shape, colours far less than that. Millau asked for 12 km2 of ortho to
    # colour 2.18: 849 MB where 150 would have done, and that repeats for every
    # scene adopted. Pad by a tile of slack so the crop never samples the edge.
    rminx, rminy, rmaxx, rmaxy = minx, miny, maxx, maxy
    clipped = a.no_sparse or (a.crop_to_shape and polys is not None)
    if clipped:
        pad = 50.0
        boxes = [fetch_tiles.bbox_of(q) for q in (polys or [])]
        kept = ((min(b[0] for b in boxes), min(b[1] for b in boxes),
                 max(b[2] for b in boxes), max(b[3] for b in boxes))
                if boxes else (want_bbox or bbox))
        rminx = max(minx, kept[0] - pad); rminy = max(miny, kept[1] - pad)
        rmaxx = min(maxx, kept[2] + pad); rmaxy = min(maxy, kept[3] + pad)
        saved = 1 - ((rmaxx - rminx) * (rmaxy - rminy)) / ((maxx - minx) * (maxy - miny))
        if saved > 0.05:
            print(f"[acquire] ortho over the {(rmaxx-rminx)/1000:.1f} x "
                  f"{(rmaxy-rminy)/1000:.1f} km being kept, not the "
                  f"{(maxx-minx)/1000:.1f} x {(maxy-miny)/1000:.1f} km of tiles "
                  f"({100*saved:.0f}% less to fetch)", flush=True)

    # One rectangle has to span every shape, and for a foreground here and a
    # background some kilometres away most of that rectangle is ground the
    # scene never touches. The ortho grid skips any cell no selected LiDAR tile
    # sits under, so the gap costs nothing.
    def covered(tx0, ty0, tx1, ty1):
        return any(bx0 < tx1 and bx1 > tx0 and by0 < ty1 and by1 > ty0
                   for bx0, by0, bx1, by1 in tile_boxes)

    if raster.is_file():
        print(f"[acquire] reusing {raster.name}", flush=True)
    else:
        print(f"[acquire] fetching ortho at {a.raster_res} m/px ...", flush=True)
        lp.fetch_raster_tiled(rminx, rminy, rmaxx, rmaxy, a.raster_res, layer,
                              str(raster), keep=covered)

    # ── 5. sparse PLY ────────────────────────────────────────────────────────
    # Built tile by tile: peak memory then follows the largest tile rather than
    # the whole scene, which is what makes billion-point scenes possible.
    ply = out / f"{a.name}-{suffix_for(radius)}.ply"
    if a.no_sparse:
        print(f"[acquire] --no-sparse: leaving the light cloud to the .blend "
              f"that already holds one", flush=True)
        n_out = 0
    else:
        print(f"[acquire] building {ply.name} tile by tile ...", flush=True)
        r = winrun.stream([sys.executable, str(Path(__file__).parent / "densify_tiled.py"),
                            "--tiles", str(tiles_dir), "--raster", str(raster),
                            "--voxel", str(voxel), "--origin", ",".join(str(v) for v in origin),
                            "--multiplier", str(a.multiplier),
                            "--name", a.name, "--out", str(out)]
                           + (["--polygon", carve] if (a.crop_to_shape and carve) else [])
                           + (["--polygon-out", a.carve_out]
                              if (a.crop_to_shape and a.carve_out) else []))
        if r.returncode != 0:
            sys.exit(f"densify_tiled failed with {r.returncode}")

        with open(ply, "rb") as f:
            head = f.read(4096).decode("ascii", errors="replace")
        n_out = next(int(l.split()[-1]) for l in head.splitlines()
                     if l.startswith("element vertex"))

    # ── 6. manifest ──────────────────────────────────────────────────────────
    scene = {
        "name": a.name,
        "created": date.today().isoformat(),
        "crs": "EPSG:2154",
        "origin": origin,
        "bbox": [minx, miny, maxx, maxy],
        "relief": round(maxz - minz, 2),
        "footprint": ({"type": "MultiPolygon",
                       "coordinates": [[q] for q in polys]} if polys else None),
        "shapes": len(polys or []),
        "cropped_to_footprint": bool(a.crop_to_shape and polys),
        "tiles_dir": str(tiles_dir),
        "tiles": [t.name for t in tiles],
        "raw_points": raw_total,
        "index_points": wfs_points,
        "native_spacing": round(spacing, 4),
        "raster": raster.name,
        "raster_res": a.raster_res,
        "radius_multiplier": a.multiplier,
        "origin_forced": bool(a.origin),
        "variants": ([] if a.no_sparse else [{
            "role": "sparse", "file": ply.name, "voxel": voxel,
            "radius": radius, "points": n_out,
        }]),
        "renders": [],
    }
    (out / "scene.json").write_text(json.dumps(scene, indent=2), encoding="utf-8")

    if a.no_sparse:
        print(f"\n[acquire] tiles, ortho and manifest are ready; the light cloud "
              f"stays the one already in your .blend")
    else:
        print(f"\n[acquire] {ply.name}: {n_out:,} points, "
              f"{ply.stat().st_size/1e9:.2f} GB")
        print(f"[acquire] radius {radius:.4f}")
    print(f"[acquire] wrote {out/'scene.json'}")


if __name__ == "__main__":
    main()
