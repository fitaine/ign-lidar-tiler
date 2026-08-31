"""Render prep: turn a lit, carved .blend into a headless dense render file.

Stage 3 of the IGN LiDAR Tiler (see PLAN.md), as one command:

    python prepare_render.py --scene scenes/aiguille/scene.json \
        --blend "aiguille.blend" --target 150000000

It runs the four steps in order and records the result in the manifest:

  1. solve the voxel for the requested point count, by measurement
  2. export the carve mask from the .blend
  3. build the dense PLY tile by tile
  4. crop it to the mask, then write the render .blend

Every step is an existing script rather than a reimplementation, so the UI and
the command line take exactly the same path.

The output is NOT for opening in the GUI. The point of the whole design is
that the dense cloud never enters a viewport.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent

BLENDER_CANDIDATES = [
    r"C:\Program Files\Blender Foundation\Blender 5.0\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.5\blender.exe",
    r"C:\Program Files\Blender Foundation\Blender 4.2\blender.exe",
]


def find_blender(explicit=None):
    if explicit:
        if not Path(explicit).is_file():
            sys.exit(f"--blender-exe not found: {explicit}")
        return explicit
    env = os.environ.get("BLENDER_EXE")
    if env and Path(env).is_file():
        return env
    for c in BLENDER_CANDIDATES:
        if Path(c).is_file():
            return c
    for d in sorted(Path(r"C:\Program Files\Blender Foundation").glob("Blender *"),
                    reverse=True):
        exe = d / "blender.exe"
        if exe.is_file():
            return str(exe)
    sys.exit("could not find blender.exe; pass --blender-exe or set BLENDER_EXE")


def run(cmd, label):
    print(f"\n[prep] === {label} ===", flush=True)
    print("[prep] " + " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd),
          flush=True)
    r = subprocess.run([str(c) for c in cmd])
    if r.returncode != 0:
        sys.exit(f"[prep] {label} failed with {r.returncode}")


def run_capture(cmd, label):
    """Run, echoing output live AND returning it.

    capture_output=True holds everything until the process exits. That was
    fine when the voxel solve probed three tiles; it now measures every tile
    and can run for half an hour, during which the log showed nothing after
    "solving the voxel" and looked hung.
    """
    print(f"\n[prep] === {label} ===", flush=True)
    proc = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    lines = []
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        lines.append(line)
    proc.wait()
    if proc.returncode != 0:
        sys.exit(f"[prep] {label} failed with {proc.returncode}")
    return "".join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scene", required=True, help="scene.json of the source scene")
    p.add_argument("--blend", required=True, help="The lit, carved .blend")
    p.add_argument("--target", type=int, default=None,
                   help="Dense target point count (default 150M)")
    p.add_argument("--voxel", type=float, default=None,
                   help="Skip solving and use this voxel")
    p.add_argument("--multiplier", type=float, default=None,
                   help="Ball radius multiplier; defaults to the scene's")
    p.add_argument("--cell", type=float, default=3.0,
                   help="Carve mask cell size in metres (default 3.0)")
    p.add_argument("--object", default=None,
                   help="Cloud object; omit to use the add-on's tag")
    p.add_argument("--drop", action="append", default=[],
                   help="Delete this object from the render file. Repeatable.")
    p.add_argument("--no-crop", action="store_true",
                   help="Skip the carve mask and keep the whole footprint")
    p.add_argument("--strip-volumes", action="store_true",
                   help="Remove volumetrics. Off by default: they are part of the "
                        "image, not overhead. Use this only when measuring what "
                        "the point cloud alone costs.")
    p.add_argument("--out", default=None, help="Output .blend")
    p.add_argument("--blender-exe", default=None)
    a = p.parse_args()

    scene_path = Path(a.scene)
    if not scene_path.is_file():
        sys.exit(f"scene.json not found: {scene_path}")
    man = json.loads(scene_path.read_text(encoding="utf-8"))
    folder = scene_path.parent
    blend = Path(a.blend)
    if not blend.is_file():
        sys.exit(f".blend not found: {blend}")

    tiles_dir = Path(man.get("tiles_dir") or (folder / "tiles"))
    if not tiles_dir.is_dir():
        sys.exit(f"tile archive not found: {tiles_dir}\n"
                 f"       (scene.json's tiles_dir, or <scene folder>/tiles)")
    raster = folder / man["raster"] if man.get("raster") else None
    origin_xyz = [float(v) for v in man["origin"]]
    origin = ",".join(str(v) for v in man["origin"])
    mult = a.multiplier if a.multiplier is not None else man.get("radius_multiplier", 1.0)
    target = a.target or 150_000_000
    name = man.get("name", folder.name)
    blender = find_blender(a.blender_exe)

    print(f"[prep] scene   : {name}", flush=True)
    print(f"[prep] blend   : {blend}", flush=True)
    print(f"[prep] tiles   : {tiles_dir}", flush=True)
    print(f"[prep] origin  : {origin}", flush=True)
    print(f"[prep] blender : {blender}", flush=True)

    # ── 2. carve mask ────────────────────────────────────────────────────────
    mask = folder / f"{name}-carve-mask.npz"
    if not a.no_crop:
        cmd = [blender, "-b", blend, "--python", HERE / "extract_mask.py", "--",
               "--cell", a.cell, "--origin", origin, "--out", mask]
        if a.object:
            cmd += ["--object", a.object]
        run(cmd, "exporting the carve mask")

    # What survived the carve decides two things: how fine the voxel has to be
    # to hit the target AFTER cropping, and which tiles are worth touching at
    # all. On a 16 km2 footprint carved to 2 km2, ignoring this both misses the
    # target eightfold and does eight times more work than needed.
    coverage, mask_bbox = 1.0, None
    if not a.no_crop:
        import numpy as np
        m = np.load(mask, allow_pickle=True)
        keys, mn, dims, mcell = m["keys"], m["mn"], m["dims"], float(m["cell"])
        ix = keys // (dims[1] * dims[2])
        iy = (keys // dims[2]) % dims[1]
        planim = np.unique(ix * dims[1] + iy)
        # Coverage must be measured against the area solve_voxel scales by,
        # which is the whole tile footprint, not the carved cloud's own
        # bounding box. Those are the same thing only when the cloud fills its
        # footprint, which is exactly what an extended scene does not do.
        kept_m2 = float(planim.size) * mcell * mcell
        scene_m2 = max(len(man.get("tiles", [])), 1) * 1e6
        coverage = min(1.0, kept_m2 / scene_m2)
        # absolute bbox of what was kept, with a cell of slack
        gx0, gx1 = int(ix.min()), int(ix.max()) + 1
        gy0, gy1 = int(iy.min()), int(iy.max()) + 1
        mask_bbox = (origin_xyz[0] + mn[0] + (gx0 - 1) * mcell,
                     origin_xyz[1] + mn[1] + (gy0 - 1) * mcell,
                     origin_xyz[0] + mn[0] + (gx1 + 1) * mcell,
                     origin_xyz[1] + mn[1] + (gy1 + 1) * mcell)
        area = (mask_bbox[2] - mask_bbox[0]) * (mask_bbox[3] - mask_bbox[1]) / 1e6
        print(f"[prep] carve keeps {kept_m2/1e6:.2f} km2 of a "
              f"{scene_m2/1e6:.0f} km2 footprint ({100*coverage:.1f}%), "
              f"bounding {area:.2f} km2", flush=True)

    # ── voxel, now that the carve is known ───────────────────────────────────
    if a.voxel:
        voxel = a.voxel
        print(f"[prep] voxel {voxel} (given)", flush=True)
    else:
        cmd = [sys.executable, HERE / "solve_voxel.py",
               "--tiles", tiles_dir, "--target", target, "--multiplier", mult,
               "--coverage", f"{coverage:.6f}"]
        n_tiles = len(list(tiles_dir.glob("*.copc.laz")))
        print(f"[prep] the solve measures all {n_tiles} tiles, which takes a few "
              f"minutes each; progress follows", flush=True)
        n_tiles = len(list(tiles_dir.glob("*.copc.laz")))
        print(f"[prep] the solve measures all {n_tiles} tiles at the coarse "
              f"voxel; a few minutes each, progress follows", flush=True)
        out = run_capture(cmd, f"solving the voxel for {target:,} points")
        voxel = None
        for line in out.splitlines():
            if line.strip().startswith("--voxel"):
                voxel = float(line.split()[-1])
        if voxel is None:
            sys.exit("[prep] could not parse a voxel from solve_voxel")
    radius = voxel / 2.0 * mult

    # The carved area caps how many points can exist at all. Asking for more
    # than the source holds over that area silently produces a cloud far below
    # the target, so say it instead.
    raw = man.get("raw_points")
    if raw and not a.no_crop and coverage < 1.0:
        density = raw / scene_m2                      # raw returns per m2
        ceiling = kept_m2 * density * 0.6             # unique voxels, not returns
        native_spacing = (1.0 / density) ** 0.5
        if voxel < native_spacing:
            print(f"[prep] NOTE the solved voxel {voxel:.3f} m is finer than the "
                  f"source spacing {native_spacing:.3f} m, so the target is not "
                  f"reachable over this carve.", flush=True)
            print(f"[prep]      {kept_m2/1e6:.2f} km2 at {density:.0f} returns/m2 "
                  f"holds roughly {ceiling/1e6:.0f}M points at most. Carve a "
                  f"larger area, or lower the target.", flush=True)

    # ── dense PLY ────────────────────────────────────────────────────────────
    dense_name = f"{name}-dense"
    cmd = [sys.executable, HERE / "densify_tiled.py",
           "--tiles", tiles_dir, "--voxel", voxel, "--origin", origin,
           "--multiplier", mult, "--name", dense_name, "--out", folder]
    if raster and raster.is_file():
        cmd += ["--raster", raster]
    if mask_bbox:
        cmd += ["--bbox", ",".join(f"{v:.2f}" for v in mask_bbox)]
    run(cmd, f"building the dense cloud at voxel {voxel:.3f}")

    suffix = f"{round(radius * 100):03d}"
    dense = folder / f"{dense_name}-{suffix}.ply"
    if not dense.is_file():
        sys.exit(f"[prep] expected {dense} but it is not there")

    # ── 4. crop, then the render file ────────────────────────────────────────
    final = dense
    if not a.no_crop:
        cropped = folder / f"{dense_name}-{suffix}-carved.ply"
        run([sys.executable, HERE / "crop_to_mask.py",
             "--ply", dense, "--mask", mask, "--origin", origin, "--out", cropped],
            "cropping to the carve mask")
        final = cropped

    # Name the file after what it actually holds, not what was asked for. The
    # solver is a fit, so a 150M request can land at 172M, and a file called
    # "150M" holding 172M is a lie you would only catch after a render — and
    # those two numbers sit on opposite sides of what a 12 GB card handles.
    with open(final, "rb") as f:
        head = f.read(4096).decode("ascii", errors="replace")
    n_pts = next(int(l.split()[-1]) for l in head.splitlines()
                 if l.startswith("element vertex"))
    if abs(n_pts - target) > 0.05 * target:
        print(f"[prep] NOTE asked for {target:,} points, built {n_pts:,} "
              f"({100*(n_pts-target)/target:+.1f}%). Naming the file after the "
              f"real count.", flush=True)

    out_blend = Path(a.out) if a.out else blend.with_name(
        f"{blend.stem} - {round(n_pts/1e6)}M-HEADLESS.blend")
    cmd = [blender, "-b", blend, "--python", HERE / "make_render_blend.py", "--",
           "--ply", final, "--radius", radius, "--out", out_blend]
    if a.object:
        cmd += ["--object", a.object]
    for d in a.drop:
        cmd += ["--drop", d]
    if a.strip_volumes:
        cmd += ["--strip-volumes"]
    run(cmd, "writing the render .blend")

    # ── record it ────────────────────────────────────────────────────────────
    man.setdefault("variants", [])
    man["variants"] = [v for v in man["variants"] if v.get("file") != final.name]
    man["variants"].append({"role": "dense", "file": final.name, "voxel": voxel,
                            "radius": radius, "points": n_pts,
                            "cropped_to_carve": not a.no_crop})
    man.setdefault("renders", []).append({
        "date": date.today().isoformat(), "source_blend": str(blend),
        "render_blend": str(out_blend), "variant": final.name,
        "radius": radius, "points": n_pts})
    scene_path.write_text(json.dumps(man, indent=2), encoding="utf-8")

    print(f"\n[prep] {final.name}: {n_pts:,} points", flush=True)
    print(f"[prep] radius {radius:.4f}", flush=True)
    print(f"[prep] wrote {out_blend}", flush=True)
    print(f"[prep] recorded in {scene_path}", flush=True)
    print("[prep] HEADLESS ONLY - do not open the render file in the GUI", flush=True)


if __name__ == "__main__":
    main()
