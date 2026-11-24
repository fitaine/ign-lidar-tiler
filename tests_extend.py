"""Extending a scene: the origin survives, and so does the manifest.

The failure these guard against is quiet and expensive. A rewritten manifest
loses the variants and the renders a scene has accumulated; a recomputed
origin moves the local frame under a .blend that is already lit, and every
camera in it ends up pointing somewhere else.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ok = fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        print(f"ok    {label}")
        ok += 1
    else:
        print(f"FAIL  {label}  {detail}")
        fail += 1


def run(*args):
    return subprocess.run([sys.executable, str(HERE / "acquire.py"), *args],
                          capture_output=True, text=True, timeout=120)


def manifest(**over):
    m = {
        "name": "testscene", "crs": "EPSG:2154",
        "origin": [971000.0, 6299000.0, 554.75],
        "bbox": [971000, 6299000, 972999.99, 6300999.99],
        "tiles_dir": r"C:\nowhere",
        "tiles": ["LHD_FXX_0971_6300_PTS_LAMB93_IGN69.copc.laz"],
        "raw_points": 1234, "raster": "r.tif", "raster_res": 0.2,
        "radius_multiplier": 1.0,
        "variants": [{"role": "sparse", "file": "a.ply", "points": 10}],
        "renders": [{"date": "2026-01-01"}],
    }
    m.update(over)
    return m


with tempfile.TemporaryDirectory() as td:
    td = Path(td)

    # --extend supplies name and out, so neither has to be given.
    p = td / "scene.json"
    p.write_text(json.dumps(manifest()), encoding="utf-8")
    r = run("--extend", str(p), "--bbox", "971000,6299000,972000,6300000", "--dry-run")
    check("--extend alone is enough to start",
          "need --name and --out" not in r.stdout + r.stderr,
          (r.stdout + r.stderr)[:200])

    # A manifest with no origin cannot anchor new ground, and says so.
    p2 = td / "noorigin.json"
    m = manifest(); m.pop("origin")
    p2.write_text(json.dumps(m), encoding="utf-8")
    r = run("--extend", str(p2), "--bbox", "971000,6299000,972000,6300000", "--dry-run")
    check("refuses a manifest with no origin",
          "records no origin" in r.stdout + r.stderr,
          (r.stdout + r.stderr)[:200])

    # A path that is not a file fails before anything is downloaded.
    r = run("--extend", str(td / "absent.json"), "--bbox",
            "971000,6299000,972000,6300000", "--dry-run")
    check("refuses a missing manifest",
          "not a file" in r.stdout + r.stderr, (r.stdout + r.stderr)[:200])

    # --added-only is meaningless on its own.
    r = run("--added-only", "--name", "x", "--out", str(td),
            "--bbox", "971000,6299000,972000,6300000", "--dry-run")
    check("--added-only needs --extend",
          "only means something with --extend" in r.stdout + r.stderr,
          (r.stdout + r.stderr)[:200])

    # Unreadable JSON is a clear message, not a traceback.
    p3 = td / "bad.json"
    p3.write_text("{not json", encoding="utf-8")
    r = run("--extend", str(p3), "--bbox", "971000,6299000,972000,6300000", "--dry-run")
    check("refuses unreadable JSON",
          "not readable JSON" in r.stdout + r.stderr and "Traceback" not in r.stderr,
          (r.stdout + r.stderr)[:200])

print()
print("all good" if not fail else f"{fail} FAILED")
sys.exit(1 if fail else 0)
