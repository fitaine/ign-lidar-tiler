"""Write a scene.json for a scene that was built before this app existed.

Render prep needs a manifest, and the 35 scenes processed with
`lidar_pipeline.py` do not have one. Everything needed is already on disk,
just scattered:

  * the origin lives in the translation matrix of `<name>_export_ply.json`
  * the voxel lives in `filters.voxeldownsize` of `<name>_workflow.json`
  * the point count lives in the PLY header
  * the tiles live in a folder of `.copc.laz` somewhere under the scene
  * the ortho is the `-020_raster.tif` if one was batch-fetched, else the
    legacy `_raster.tif`

    python adopt.py --scene-dir "…/Mont aiguille" --dry-run
    python adopt.py --scene-dir "…/Mont aiguille" --name mont-aiguille

Nothing is modified except the new `scene.json`.

Two things it will tell you rather than paper over:

  * a PLY whose export has no `filters.transformation` was written in absolute
    Lambert 93 and is quantised to a 0.5 m lattice in Y (float32 cannot do
    better at those magnitudes). Its origin cannot be read off the pipeline,
    and the geometry itself is degraded.
  * a missing tile archive means the dense variant cannot be rebuilt, since
    the whole design regenerates it from the raw tiles.
"""

import argparse
import json
import subprocess

import winrun
import sys
from datetime import date
from pathlib import Path

PDAL_EXE = r"C:\Program Files\QGIS 3.40.5\bin\pdal.exe"
SKIP_DIRS = {"_parts", "_ortho_tiles_tmp"}


def ply_points(path):
    try:
        with open(path, "rb") as f:
            head = f.read(4096).decode("ascii", errors="replace")
    except Exception:
        return None
    for line in head.splitlines():
        if line.startswith("element vertex"):
            return int(line.split()[-1])
        if line.startswith("end_header"):
            break
    return None


def origin_from_export(js):
    """The pipeline recentres with a translation matrix; the origin is its negation."""
    for st in js.get("pipeline", []):
        if st.get("type") == "filters.transformation":
            m = [float(v) for v in st["matrix"].split()]
            if len(m) == 16:
                return [-m[3], -m[7], -m[11]]
    return None


def voxel_from_workflow(js):
    for st in js.get("pipeline", []):
        if st.get("type") == "filters.voxeldownsize":
            return float(st.get("cell"))
    return None


def find_tiles_dir(root):
    """The folder holding the most .copc.laz under this scene."""
    counts = {}
    for p in root.rglob("*.copc.laz"):
        if any(s in p.parts for s in SKIP_DIRS):
            continue
        counts[p.parent] = counts.get(p.parent, 0) + 1
    if not counts:
        return None, 0
    d = max(counts, key=counts.get)
    return d, counts[d]


def pick_raster(root, stem):
    """Prefer the native 0.20 m/px batch re-fetch over the legacy raster."""
    cands = [p for p in root.rglob("*_raster.tif")
             if not any(s in p.parts for s in SKIP_DIRS)]
    if not cands:
        return None
    native = [p for p in cands if "-020_raster" in p.name]
    pool = native or cands
    same = [p for p in pool if p.name.startswith(stem.split("-")[0])]
    return (same or pool)[0]


def ply_bounds(path):
    r = winrun.run([PDAL_EXE, "info", "--summary", str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)["summary"]["bounds"]
    except Exception:
        return None


def voxel_from_suffix(stem, multiplier=1.0):
    """`-NNN` is the nominal radius in centimetres, so voxel = 2 x radius."""
    tail = stem.rsplit("-", 1)[-1]
    if tail.isdigit() and len(tail) == 3:
        return round(int(tail) / 100.0 * 2.0 / multiplier, 4)
    return None


def tile_stats(tiles):
    minx = miny = minz = float("inf")
    maxx = maxy = maxz = float("-inf")
    total = 0
    for t in tiles:
        r = winrun.run([PDAL_EXE, "info", "--summary", str(t)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  ! pdal could not read {t.name}", flush=True)
            continue
        s = json.loads(r.stdout)["summary"]
        b = s["bounds"]
        total += s["num_points"]
        minx = min(minx, b["minx"]); maxx = max(maxx, b["maxx"])
        miny = min(miny, b["miny"]); maxy = max(maxy, b["maxy"])
        minz = min(minz, b["minz"]); maxz = max(maxz, b["maxz"])
    if total == 0:
        return None
    return {"bbox": [minx, miny, maxx, maxy], "raw_points": total,
            "relief": round(maxz - minz, 2), "minz": minz}


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scene-dir", required=True, help="Existing scene folder")
    p.add_argument("--name", default=None, help="Scene name (default: from the PLY)")
    p.add_argument("--out", default=None, help="Where to write scene.json")
    p.add_argument("--multiplier", type=float, default=1.0,
                   help="Ball radius multiplier to record (1.0 = spheres touch)")
    p.add_argument("--origin", default=None,
                   help="X,Y,Z in Lambert 93, when the origin was recovered some "
                        "other way: from locate_scene.py, or by subtracting a "
                        "recentred cloud's bounds from the absolute PLY it came "
                        "from. Overrides whatever the pipeline files say.")
    p.add_argument("--dry-run", action="store_true", help="Report, write nothing")
    a = p.parse_args()

    root = Path(a.scene_dir)
    if not root.is_dir():
        sys.exit(f"not a folder: {root}")

    plys = sorted(p for p in root.rglob("*.ply")
                  if not any(s in p.parts for s in SKIP_DIRS))
    if not plys:
        sys.exit(f"no .ply found under {root}")

    print(f"scene folder : {root}")
    tiles_dir, n_tiles = find_tiles_dir(root)
    print(f"tile archive : {tiles_dir or 'NOT FOUND'}"
          + (f"  ({n_tiles} tiles)" if tiles_dir else ""))

    variants, origins, problems, no_export = [], [], [], []
    for ply in plys:
        stem = ply.stem
        exp = ply.with_name(f"{stem}_export_ply.json")
        wf = ply.with_name(f"{stem}_workflow.json")
        origin = voxel = None
        if exp.is_file():
            try:
                js = json.loads(exp.read_text(encoding="utf-8"))
                origin = origin_from_export(js)
                if origin is None:
                    problems.append(
                        f"{ply.name}: exported in absolute Lambert 93, so it is "
                        f"quantised to a 0.5 m lattice in Y and its origin is not "
                        f"recorded. Re-export it before relying on it.")
            except Exception as ex:
                problems.append(f"{ply.name}: unreadable export json ({ex})")
        else:
            no_export.append(ply.name)     # may still be resolved by its bounds
        if wf.is_file():
            try:
                voxel = voxel_from_workflow(json.loads(wf.read_text(encoding="utf-8")))
            except Exception:
                pass
        n = ply_points(ply)
        radius = (voxel / 2.0 * a.multiplier) if voxel else None
        if origin:
            origins.append(tuple(round(v, 3) for v in origin))
        inferred = False
        if voxel is None:
            voxel = voxel_from_suffix(stem, a.multiplier)
            inferred = voxel is not None
            radius = (voxel / 2.0 * a.multiplier) if voxel else None
        variants.append({"role": "sparse", "file": ply.name,
                         "path": str(ply.relative_to(root)).replace("\\", "/"),
                         "voxel": voxel, "radius": radius, "points": n,
                         "voxel_inferred": inferred,
                         "origin_known": origin is not None})
        npts = f"{n:,}" if n else "?"
        print(f"  {ply.name:<44} {npts:>14} pts  "
              f"voxel {voxel if voxel else '?'}  origin {origin if origin else '?'}")

    uniq = sorted(set(origins))
    if len(uniq) > 1:
        problems.append(f"variants disagree on the origin: {uniq}. They will not "
                        f"align with each other; pick one scene per manifest.")
    origin = list(uniq[0]) if uniq else None

    # An origin worked out elsewhere wins. A PLY written in absolute Lambert 93
    # records none, so the pipeline files cannot supply one - but subtracting
    # the recentred cloud's bounds from that PLY's gives it exactly, and a
    # matched terrain gives it to the metre. Either beats refusing to adopt.
    if a.origin:
        given = [float(v) for v in a.origin.split(",")]
        if len(given) != 3:
            sys.exit("--origin needs three comma-separated values")
        if origin and any(abs(g - o) > 1.0 for g, o in zip(given, origin)):
            problems.append(f"the given origin {given} disagrees with the one in "
                            f"the pipeline files {origin}; using the given one")
        origin = given
        print("")
        print(f"using the origin given on the command line: {origin}")

    # A PLY with no export json still has usable evidence: if adding the
    # scene origin lands its bounds inside the tile footprint, it shares that
    # origin. Better than declaring it unknown and refusing to adopt it.
    if origin:
        for v in variants:
            if v.get("origin_known"):
                continue
            b = ply_bounds(root / v["path"])
            if b is None:
                continue
            ax, ay = b["minx"] + origin[0], b["miny"] + origin[1]
            bx, by = b["maxx"] + origin[0], b["maxy"] + origin[1]
            v["_abs"] = (ax, ay, bx, by)

    stats = None
    if tiles_dir:
        print("\nreading the tile archive ...", flush=True)
        stats = tile_stats(sorted(tiles_dir.glob("*.copc.laz")))
    else:
        problems.append("no tile archive found, so a dense variant cannot be "
                        "rebuilt. Re-download the tiles to use render prep.")

    if stats and origin:
        tb = stats["bbox"]
        pad = 50.0
        for v in variants:
            if v.pop("origin_known", True):
                continue
            ab = v.pop("_abs", None)
            if ab is None:
                problems.append(f"{v['file']}: could not be read, origin unknown")
                continue
            inside = (ab[0] > tb[0] - pad and ab[1] > tb[1] - pad
                      and ab[2] < tb[2] + pad and ab[3] < tb[3] + pad)
            if inside:
                v["origin_inferred"] = True
                if v["file"] in no_export:
                    no_export.remove(v["file"])
                print(f"  {v['file']}: bounds match the scene origin, adopting it")
            else:
                problems.append(f"{v['file']}: bounds do not match the scene origin; "
                                f"left out of the manifest")
                v["excluded"] = True
    for v in variants:
        v.pop("origin_known", None)
        v.pop("_abs", None)
    excluded = [v["file"] for v in variants if v.get("excluded")]
    variants = [v for v in variants if not v.get("excluded")]

    # the densest variant is the dense one; the rest are working clouds
    with_pts = [v for v in variants if v.get("points")]
    if len(with_pts) > 1:
        densest = max(with_pts, key=lambda v: v["points"])
        for v in variants:
            v["role"] = "dense" if v is densest else "sparse"

    for fn in no_export:
        problems.append(f"{fn}: no _export_ply.json and its bounds could not be "
                        f"checked, so its origin is unknown")

    name = a.name or plys[0].stem.rsplit("-", 1)[0]
    raster = pick_raster(root, plys[0].stem)
    man = {
        "name": name,
        "created": date.today().isoformat(),
        "adopted_from": str(root),
        "crs": "EPSG:2154",
        "origin": origin,
        "bbox": stats["bbox"] if stats else None,
        "relief": stats["relief"] if stats else None,
        "footprint": None,
        "cropped_to_footprint": False,
        "tiles_dir": str(tiles_dir) if tiles_dir else None,
        "tiles": sorted(t.name for t in tiles_dir.glob("*.copc.laz")) if tiles_dir else [],
        "raw_points": stats["raw_points"] if stats else None,
        "raster": str(raster.relative_to(root)).replace("\\", "/") if raster else None,
        "raster_res": 0.20 if (raster and "-020_raster" in raster.name) else None,
        "radius_multiplier": a.multiplier,
        "variants": variants,
        "renders": [],
    }

    print(f"\nname         : {name}")
    print(f"origin       : {origin}")
    print(f"raster       : {raster.name if raster else 'NOT FOUND'}")
    if stats:
        print(f"raw points   : {stats['raw_points']:,}   relief {stats['relief']} m")
    if problems:
        print("\nproblems:")
        for pr in problems:
            print(f"  ! {pr}")

    if a.dry_run:
        print("\ndry run, nothing written")
        return
    if origin is None:
        sys.exit("\nrefusing to write a manifest with no origin: render prep would "
                 "produce a misaligned cloud. Re-export the PLY first.")

    out = Path(a.out) if a.out else root / "scene.json"
    out.write_text(json.dumps(man, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
