"""Locate a .blend's point cloud in Lambert 93, in one command.

Two steps that always go together: export a ground-height raster from the cloud
through Blender, then match it against IGN's elevation. Kept as one entry point
so the app can run it as a single job, and so the command line does not need
two invocations and a temporary file.

    python locate_blend.py --blend "scene.blend" --centre 701800,6331150 \
        --radius 2500

Prints the recovered origin and whether to believe it. Nothing is written to
the .blend, which is opened read-only.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import prepare_render          # for find_blender, so the search lives in one place


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--blend", required=True, help="The .blend holding the cloud")
    p.add_argument("--object", default=None,
                   help="Cloud object; omit to use the only large one")
    p.add_argument("--centre", required=True,
                   help="X,Y in Lambert 93 to search around: roughly where you "
                        "think the scene is, to a few hundred metres")
    p.add_argument("--radius", type=float, default=2500.0,
                   help="Half-width of the search area in metres. Must exceed "
                        "half the cloud's own size (default 2500)")
    p.add_argument("--cell", type=float, default=2.0,
                   help="Raster step in metres (default 2.0)")
    p.add_argument("--hp-window", type=int, default=41)
    p.add_argument("--keep-grid", default=None,
                   help="Where to keep the exported height raster")
    p.add_argument("--blender-exe", default=None)
    a = p.parse_args()

    blend = Path(a.blend)
    if not blend.is_file():
        sys.exit(f".blend not found: {blend}")
    blender = prepare_render.find_blender(a.blender_exe)

    with tempfile.TemporaryDirectory() as wd:
        grid = Path(a.keep_grid) if a.keep_grid else Path(wd) / "grid.npz"
        cmd = [blender, "-b", str(blend), "--python", str(HERE / "export_height_grid.py"),
               "--", "--cell", str(a.cell), "--out", str(grid)]
        if a.object:
            cmd += ["--object", a.object]
        print(f"[locate] reading the cloud out of {blend.name} ...", flush=True)
        r = subprocess.run(cmd, capture_output=True, text=True)
        # Blender is noisy on startup; only the grid's own lines are worth showing
        for line in r.stdout.splitlines():
            if line.startswith("[grid]"):
                print(line, flush=True)
        if r.returncode != 0 or not grid.is_file():
            sys.exit(f"[locate] could not export the height raster:\n"
                     f"{r.stdout[-800:]}\n{r.stderr[-400:]}")

        print(f"[locate] matching it against IGN's elevation ...", flush=True)
        r = subprocess.run(
            [sys.executable, str(HERE / "locate_scene.py"), "--grid", str(grid),
             "--centre", a.centre, "--radius", str(a.radius),
             "--hp-window", str(a.hp_window)],
            text=True)
        return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
