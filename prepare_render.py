"""Render prep: turn a lit, carved .blend into a headless dense render file.

Stage 3 of the IGN LiDAR Tiler (see PLAN.md), as one command:

    python prepare_render.py --scene scenes/aiguille/scene.json \
        --blend "aiguille.blend" --target 150000000

It runs the steps in order and records the result in the manifest:

  1. export the carve footprint from the proxy: its outline, islands and holes
  2. count the source points inside that footprint
  3. under the budget, keep every return; over it, downsample to the budget
  4. build the dense PLY tile by tile, cutting the tiles to the footprint
  5. check that its spheres actually touch, and write the render .blend

Nothing crops the dense cloud after the fact. The tiles are cut to the shape
instead, so a point inside the carve is never dropped for being somewhere the
sparse proxy happened not to sample.

Use --whole-footprint to ignore the carve and process every tile.

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
    p.add_argument("--cell", type=float, default=4.0,
                   help="Footprint rasterising step in metres (default 4.0). It "
                        "decides how closely the outline follows the carve; it "
                        "never removes points")
    p.add_argument("--object", default=None,
                   help="Cloud object; omit to use the add-on's tag")
    p.add_argument("--drop", action="append", default=[],
                   help="Delete this object from the render file. Repeatable.")
    # The proxy's outline decides what gets built, which is the whole point of
    # carving a light cloud first. Use --whole-footprint to ignore the carve and
    # process every tile, the way lidar_pipeline.py does.
    p.add_argument("--whole-footprint", action="store_true",
                   help="Ignore the carve and process the entire tile footprint")
    p.add_argument("--crop", action="store_true",
                   help="Accepted for compatibility; the carve footprint is "
                        "used by default now")
    p.add_argument("--no-crop", dest="whole_footprint", action="store_true",
                   help="Alias for --whole-footprint")
    p.add_argument("--strip-volumes", action="store_true",
                   help="Remove volumetrics. Off by default: they are part of the "
                        "image, not overhead. Use this only when measuring what "
                        "the point cloud alone costs.")
    p.add_argument("--allow-sparse", action="store_true",
                   help="Write the render file even when the cloud came out far "
                        "short of the target, or when its spheres do not touch. "
                        "Off by default: that combination is a wasted render.")
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

    # ── 2. the carve footprint ───────────────────────────────────────────────
    # The proxy's outline IS the shape: islands stay islands, holes cut in the
    # middle stay holes. PDAL cuts the source tiles to it and every return
    # inside is kept.
    #
    # This replaces a 3D occupancy mask that the dense cloud was cropped
    # against afterwards. That mask kept a dense point only where a sparse
    # proxy point sat in the same 3 m cell, so on flat ground — one proxy point
    # every couple of metres, dense points whose Z fell in the neighbouring
    # cell — it deleted points that belonged. Holes on flat surfaces was its
    # signature. Nothing crops the dense cloud now; the tiles are cut instead.
    footprint = folder / f"{name}-carve.geojson"
    if not a.whole_footprint:
        cmd = [blender, "-b", blend, "--python", HERE / "extract_outline.py", "--",
               "--cell", a.cell, "--origin", origin, "--out", footprint]
        if a.object:
            cmd += ["--object", a.object]
        run(cmd, "exporting the carve footprint")
        if not footprint.is_file():
            sys.exit(f"[prep] expected {footprint} but it is not there")

    # ── 3. count the source inside it, then decide ───────────────────────────
    # Count first, downsample second. The old order solved a voxel for the
    # budget over the whole footprint and cropped afterwards, so the budget was
    # spent on ground the camera never sees.
    voxel = a.voxel
    if voxel is None and not a.whole_footprint:
        out = run_capture(
            [sys.executable, HERE / "plan_density.py", "--tiles", tiles_dir,
             "--polygon", footprint, "--target", target],
            f"counting the source inside the carve, against {target:,}")
        counted = None
        for line in out.splitlines():
            if line.startswith("source points inside the carve:"):
                counted = int(line.split(":")[1].strip().replace(",", ""))
        if counted is None:
            sys.exit("[prep] could not read a count from plan_density")
        if counted <= target:
            voxel = 0.0
            print(f"[prep] {counted:,} points inside the carve, under the "
                  f"{target:,} budget: keeping every return", flush=True)
        else:
            print(f"[prep] {counted:,} points inside the carve, over the "
                  f"{target:,} budget: downsampling to it", flush=True)

    if voxel is None:
        coverage = 1.0
        cmd = [sys.executable, HERE / "solve_voxel.py",
               "--tiles", tiles_dir, "--target", target, "--multiplier", mult,
               "--coverage", f"{coverage:.6f}"]
        out = run_capture(cmd, f"solving the voxel for {target:,} points")
        for line in out.splitlines():
            if line.strip().startswith("--voxel"):
                voxel = float(line.split()[-1])
        if voxel is None:
            sys.exit("[prep] could not parse a voxel from solve_voxel")
    elif a.voxel is not None:
        print(f"[prep] voxel {voxel} (given)", flush=True)

    # ── 4. dense PLY ─────────────────────────────────────────────────────────
    dense_name = f"{name}-dense"
    cmd = [sys.executable, HERE / "densify_tiled.py",
           "--tiles", tiles_dir, "--voxel", voxel, "--origin", origin,
           "--multiplier", mult, "--name", dense_name, "--out", folder]
    if raster and raster.is_file():
        cmd += ["--raster", raster]
    if not a.whole_footprint:
        cmd += ["--polygon", footprint]
    run(cmd, "building the dense cloud"
             + (" with every return" if voxel <= 0 else f" at voxel {voxel:.3f}"))

    # With no downsampling the radius comes from the built cloud's own spacing,
    # so the filename is the only place that knows it.
    import check_fill
    if voxel > 0:
        radius = voxel / 2.0 * mult
        dense = folder / f"{dense_name}-{round(radius * 100):03d}.ply"
    else:
        found = sorted(folder.glob(f"{dense_name}-[0-9][0-9][0-9].ply"),
                       key=lambda p: p.stat().st_mtime)
        if not found:
            sys.exit(f"[prep] no {dense_name}-NNN.ply was written")
        dense = found[-1]
        radius = int(dense.stem.rsplit("-", 1)[1]) / 100.0
    if not dense.is_file():
        sys.exit(f"[prep] expected {dense} but it is not there")
    final = dense

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

    # ── does this cloud actually hold a surface? ─────────────────────────────
    # A shortfall used to be a note, and a note is not enough: La Plagne asked
    # for 150M, built 71M, said so in one line, and went on to write a render
    # file whose points do not touch each other. What renders as a surface is
    # decided by whether the spheres meet, so measure that here, on the file
    # that is about to be rendered, while stopping is still cheap.
    import check_fill
    fill = check_fill.measure(final, voxel)
    tag, note = check_fill.verdict(fill)
    print(f"[prep] fill check: {fill['touching_mean']:.2f} of "
          f"{check_fill.SURFACE_SLOTS} neighbours "
          f"touching, lattice {100*fill['fill']:.0f}% filled, "
          f"{100*fill['isolated']:.1f}% of points isolated -> {tag}", flush=True)

    # The target is only a promise when the solver was asked to hit it. Naming
    # a voxel yourself says the density matters and the count is whatever the
    # ground holds — refusing that would be the guard second-guessing a
    # deliberate choice.
    short = n_pts < 0.80 * target and not a.voxel
    if (tag == "DUST" or short) and not a.allow_sparse:
        print(f"[prep] STOP before writing the render file.", flush=True)
        if short:
            print(f"[prep]   asked for {target:,} points, the data gave "
                  f"{n_pts:,} ({100*n_pts/target:.0f}%).", flush=True)
        if tag == "DUST":
            print(f"[prep]   {note}", flush=True)
            print(f"[prep]   at voxel {voxel:.3f} the cells are finer than the "
                  f"scan's own sampling. A coarser voxel will look better AND "
                  f"render faster.", flush=True)
        print(f"[prep]   the dense PLY is kept at {final}", flush=True)
        print(f"[prep]   rerun with a coarser --voxel, or --allow-sparse to "
              f"proceed anyway.", flush=True)
        sys.exit(3)
    if tag == "THIN":
        print(f"[prep] WARNING {note}", flush=True)

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
                            "cropped_to_carve": not a.whole_footprint})
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
