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
import sys
from datetime import date
from pathlib import Path

import fetch_tiles
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
    r = subprocess.run([PDAL_EXE, "info", "--summary", str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"pdal info failed on {path.name}: {r.stderr[:300]}")
    return json.loads(r.stdout)["summary"]


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--bbox", help="minx,miny,maxx,maxy in Lambert 93")
    g.add_argument("--geojson", help="GeoJSON Polygon in Lambert 93")
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
    p.add_argument("--tiles-dir", default=None,
                   help="Existing tile archive to use instead of <out>/tiles")
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
    poly = None
    if a.geojson:
        gj = json.loads(Path(a.geojson).read_text(encoding="utf-8"))
        geom = gj["geometry"] if gj.get("type") == "Feature" else gj
        poly = geom["coordinates"][0]
        xs = [q[0] for q in poly]; ys = [q[1] for q in poly]
        bbox = (min(xs), min(ys), max(xs), max(ys))
    else:
        bbox = tuple(float(v) for v in a.bbox.split(","))

    feats = fetch_tiles.list_tiles(bbox)
    if poly is not None:
        feats = [f for f in feats if fetch_tiles.tile_intersects_polygon(f, poly)]
    if not feats:
        sys.exit("no tiles found for that footprint")
    feats.sort(key=lambda f: f["properties"]["name"])
    print(f"[acquire] {len(feats)} tiles selected", flush=True)

    if not a.skip_download:
        for f in feats:
            url = f["properties"]["url"]
            dest = tiles_dir / Path(url.split("?")[0]).name
            if dest.is_file():
                print(f"[acquire]   {dest.name} already present", flush=True)
                continue
            fetch_tiles.download(url, dest)

    tiles = sorted(tiles_dir.glob("*.copc.laz"))
    if not tiles:
        sys.exit(f"no tiles in {tiles_dir}")

    # ── 2. scene extent and shared origin ────────────────────────────────────
    minx = miny = minz = float("inf")
    maxx = maxy = maxz = float("-inf")
    raw_total = 0
    for t in tiles:
        s = tile_summary(t)
        b = s["bounds"]
        raw_total += s["num_points"]
        minx = min(minx, b["minx"]); maxx = max(maxx, b["maxx"])
        miny = min(miny, b["miny"]); maxy = max(maxy, b["maxy"])
        minz = min(minz, b["minz"]); maxz = max(maxz, b["maxz"])

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

    if a.dry_run:
        print(f"[acquire] dry run: would build a sparse PLY into {out}", flush=True)
        return

    # ── 3. voxel ─────────────────────────────────────────────────────────────
    if a.voxel:
        voxel = a.voxel
        print(f"[acquire] voxel {voxel} (given)", flush=True)
    else:
        import solve_voxel
        print(f"[acquire] solving voxel for {a.target:,} points ...", flush=True)
        r = subprocess.run([sys.executable, str(Path(__file__).parent / "solve_voxel.py"),
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
    if raster.is_file():
        print(f"[acquire] reusing {raster.name}", flush=True)
    else:
        print(f"[acquire] fetching ortho at {a.raster_res} m/px ...", flush=True)
        lp.fetch_raster_tiled(minx, miny, maxx, maxy, a.raster_res, layer, str(raster))

    # ── 5. sparse PLY ────────────────────────────────────────────────────────
    # Built tile by tile: peak memory then follows the largest tile rather than
    # the whole scene, which is what makes billion-point scenes possible.
    ply = out / f"{a.name}-{suffix_for(radius)}.ply"
    print(f"[acquire] building {ply.name} tile by tile ...", flush=True)
    r = subprocess.run([sys.executable, str(Path(__file__).parent / "densify_tiled.py"),
                        "--tiles", str(tiles_dir), "--raster", str(raster),
                        "--voxel", str(voxel), "--origin", ",".join(str(v) for v in origin),
                        "--multiplier", str(a.multiplier),
                        "--name", a.name, "--out", str(out)]
                       + (["--polygon", a.geojson] if (a.crop_to_shape and a.geojson) else []))
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
        "footprint": ({"type": "Polygon", "coordinates": [poly]} if poly else None),
        "cropped_to_footprint": bool(a.crop_to_shape and poly),
        "tiles_dir": str(tiles_dir),
        "tiles": [t.name for t in tiles],
        "raw_points": raw_total,
        "raster": raster.name,
        "raster_res": a.raster_res,
        "radius_multiplier": a.multiplier,
        "origin_forced": bool(a.origin),
        "variants": [{
            "role": "sparse", "file": ply.name, "voxel": voxel,
            "radius": radius, "points": n_out,
        }],
        "renders": [],
    }
    (out / "scene.json").write_text(json.dumps(scene, indent=2), encoding="utf-8")

    print(f"\n[acquire] {ply.name}: {n_out:,} points, {ply.stat().st_size/1e9:.2f} GB")
    print(f"[acquire] radius {radius:.4f}")
    print(f"[acquire] wrote {out/'scene.json'}")


if __name__ == "__main__":
    main()
