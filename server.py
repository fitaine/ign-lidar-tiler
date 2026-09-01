"""Local web app: pick an area on a map, get a scene.

Stage 1 UI of the IGN LiDAR Tiler (see PLAN.md):

    python server.py            # then open http://localhost:8770

Built on the standard library. FastAPI was the original plan, but nothing here
needs it and this machine has no web framework installed; a local single-user
tool is not worth adding dependencies for.

Coordinates: the map works in longitude/latitude, everything downstream works
in Lambert 93. lambert93.py does the conversion and agrees with GDAL to eight
decimal places.
"""

import json
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import fetch_tiles
from lambert93 import to_lambert, to_wgs84

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
# NOT 8765: that is rendafar's port. Squatting on it made rendafar's desktop
# app send its requests here, where an unknown route replies {"error": "not
# found"} — which it showed as a popup, looking like a missing .blend.
PORT = 8770

JOBS = {}
JOBS_LOCK = threading.Lock()


def clean_path(raw):
    """Windows paths arrive mangled in several ways.

    'Copy as path' wraps them in quotes; copying from a file:// URL or from
    some apps percent-encodes the spaces. Both produce a path that does not
    exist, with a confusing error. Normalise before touching the disk.
    """
    if raw is None:
        return None
    p = str(raw).strip().strip('"').strip("'")
    if p.lower().startswith("file:///"):
        p = p[8:]
    if "%" in p:
        p = unquote(p)
    return p.replace("/", "\\") if ":" in p[:3] else p


HIDDEN = 0x2
SYSTEM = 0x4


def _is_noise(entry):
    r"""Windows keeps legacy junctions like Documents\Mes images that exist
    only for backwards compatibility and deny listing outright. They are
    marked hidden+system; showing them just offers dead ends."""
    try:
        attrs = entry.stat(follow_symlinks=False).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attrs & HIDDEN) or bool(attrs & SYSTEM)


def list_dir(path):
    """Directory listing for the file browser."""
    if not path:
        drives = []
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            d = Path(f"{letter}:\\")
            if d.exists():
                drives.append(str(d))
        return {"path": "", "parent": None, "dirs": drives, "files": []}
    p = Path(path)
    if not p.is_dir():
        p = p.parent
    dirs, files = [], []
    try:
        entries = sorted(p.iterdir(), key=lambda x: x.name.lower())
    except (PermissionError, OSError) as ex:
        return {"path": str(p), "parent": str(p.parent), "dirs": [], "files": [],
                "error": f"cannot list this folder ({ex.__class__.__name__})"}
    for e in entries:
        if _is_noise(e):
            continue
        try:
            if e.is_dir():
                dirs.append(e.name)
            else:
                files.append({"name": e.name, "size": e.stat().st_size})
        except OSError:
            continue
    parent = str(p.parent) if p.parent != p else ""
    return {"path": str(p), "parent": parent, "dirs": dirs, "files": files}


def tiles_as_geojson(feats):
    """Tile footprints, reprojected to WGS84 for the map."""
    out = []
    for f in feats:
        ring = fetch_tiles.ring_of(f)
        out.append({
            "type": "Feature",
            "geometry": {"type": "Polygon",
                         "coordinates": [[list(to_wgs84(x, y)) for x, y in
                                          ((p[0], p[1]) for p in ring)]]},
            "properties": {"name": Path(f["properties"]["url"].split("?")[0]).name,
                           "url": f["properties"]["url"]},
        })
    return {"type": "FeatureCollection", "features": out}


def run_job(job_id, args, script="acquire.py"):
    with JOBS_LOCK:
        JOBS[job_id]["state"] = "running"
    proc = subprocess.Popen([sys.executable, "-u", str(HERE / script)] + args,
                            cwd=str(HERE), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    with JOBS_LOCK:
        JOBS[job_id]["pid"] = proc.pid
    for line in proc.stdout:
        with JOBS_LOCK:
            JOBS[job_id]["log"].append(line.rstrip())
            JOBS[job_id]["log"] = JOBS[job_id]["log"][-400:]
    proc.wait()
    with JOBS_LOCK:
        JOBS[job_id]["state"] = "done" if proc.returncode == 0 else "failed"
        JOBS[job_id]["returncode"] = proc.returncode


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass                                   # quiet; the console is for jobs

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_text(encoding="utf-8"),
                              "text/html; charset=utf-8")
        if u.path == "/api/tiles":
            q = parse_qs(u.query)
            try:
                w, s, e, n = (float(q[k][0]) for k in ("w", "s", "e", "n"))
            except Exception:
                return self._send(400, {"error": "need w,s,e,n in degrees"})
            xs, ys = zip(to_lambert(w, s), to_lambert(e, s),
                         to_lambert(w, n), to_lambert(e, n))
            bbox = (min(xs), min(ys), max(xs), max(ys))
            # 4000 km2 was too tight: an ordinary window at zoom 11 exceeds it,
            # so the grid vanished at zooms where it is still useful.
            area_km2 = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) / 1e6
            if area_km2 > 20000:
                return self._send(200, {"type": "FeatureCollection", "features": [],
                                        "note": f"{area_km2:,.0f} km² in view, "
                                                f"too wide for the tile grid"})
            try:
                feats = fetch_tiles.list_tiles(bbox)
            except Exception as ex:
                return self._send(502, {"error": f"IGN WFS: {ex}"})
            return self._send(200, tiles_as_geojson(feats))
        if u.path == "/api/browse":
            q = parse_qs(u.query)
            path = clean_path(q.get("path", [""])[0])
            exts = [e.lower() for e in q.get("ext", [""])[0].split(",") if e]
            try:
                d = list_dir(path)
            except Exception as ex:
                return self._send(200, {"path": path or "", "parent": "",
                                        "dirs": [], "files": [],
                                        "error": str(ex)})
            if exts:
                d["files"] = [f for f in d["files"]
                              if any(f["name"].lower().endswith(e) for e in exts)]
            return self._send(200, d)

        if u.path.startswith("/api/job/"):
            jid = u.path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                job = JOBS.get(jid)
                return self._send(200 if job else 404, job or {"error": "no such job"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})

        if u.path == "/api/select":
            ring = body.get("ring") or []
            if len(ring) < 4:
                return self._send(400, {"error": "need a closed ring of lon/lat"})
            poly = [list(to_lambert(lon, lat)) for lon, lat in ring]
            xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
            bbox = (min(xs), min(ys), max(xs), max(ys))
            try:
                feats = fetch_tiles.list_tiles(bbox)
            except Exception as ex:
                return self._send(502, {"error": f"IGN WFS: {ex}"})
            feats = [f for f in feats if fetch_tiles.tile_intersects_polygon(f, poly)]
            urls = [f["properties"]["url"] for f in feats]
            sizes = fetch_tiles.head_sizes(urls)
            total = sum(sizes)
            names = [{"name": Path(u.split("?")[0]).name, "bytes": b}
                     for u, b in zip(urls, sizes)]
            return self._send(200, {
                "tiles": names, "count": len(names), "bytes": total,
                "lambert_polygon": poly, "bbox": bbox,
                "geojson": tiles_as_geojson(feats),
            })

        if u.path == "/api/pick":
            # A real Windows dialog, opened on this machine. The browser cannot
            # do it: <input type=file> never reveals a real path. Run as its own
            # process because Tk is not thread-safe and we answer on threads.
            py = sys.executable
            pyw = Path(py).with_name("pythonw.exe")
            if pyw.is_file():
                py = str(pyw)          # no console window flashing up
            args = [py, str(HERE / "pick_path.py"),
                    "--kind", body.get("kind", "file")]
            if body.get("ext"):
                args += ["--ext", body["ext"]]
            if body.get("title"):
                args += ["--title", body["title"]]
            start = clean_path(body.get("start") or "")
            if start:
                args += ["--initial", start]
            try:
                # Generous: this is waiting on a person browsing their disk.
                out = subprocess.run(args, capture_output=True, text=True,
                                     timeout=900, cwd=str(HERE),
                                     creationflags=getattr(subprocess,
                                                           "CREATE_NO_WINDOW", 0))
            except subprocess.TimeoutExpired:
                return self._send(200, {"path": "", "error": "the dialog timed out"})
            except OSError as ex:
                return self._send(200, {"path": "", "error": str(ex)})
            path = (out.stdout or "").strip()
            if not path and out.returncode != 0:
                return self._send(200, {"path": "",
                                        "error": (out.stderr or "").strip()[:200]})
            return self._send(200, {"path": path})     # empty = cancelled

        if u.path == "/api/scene":
            # Read a manifest so the render-prep panel can show what it is about
            # to work on, and refuse a path that is not one.
            sp = Path(clean_path(body.get("scene", "")) or "")
            if not sp.is_file():
                return self._send(404, {"error": f"not found: {sp}"})
            try:
                man = json.loads(sp.read_text(encoding="utf-8"))
            except Exception as ex:
                return self._send(400, {"error": f"unreadable: {ex}"})
            if "variants" not in man or "origin" not in man:
                return self._send(400, {"error": "not a scene manifest"})
            tiles = Path(man.get("tiles_dir") or (sp.parent / "tiles"))
            man["_tiles_present"] = tiles.is_dir()
            man["_tiles_dir"] = str(tiles)
            return self._send(200, man)

        if u.path == "/api/prepare":
            need = ("scene", "blend")
            if any(not body.get(k) for k in need):
                return self._send(400, {"error": f"need {need}"})
            args = ["--scene", clean_path(body["scene"]),
                    "--blend", clean_path(body["blend"]),
                    "--cell", str(body.get("cell", 3.0))]
            if body.get("voxel"):
                args += ["--voxel", str(body["voxel"])]
            else:
                args += ["--target", str(int(body.get("target", 150_000_000)))]
            if body.get("multiplier"):
                args += ["--multiplier", str(body["multiplier"])]
            if body.get("object"):
                args += ["--object", body["object"]]
            for d in (body.get("drop") or []):
                args += ["--drop", d]
            if body.get("out"):
                args += ["--out", clean_path(body["out"])]
            if body.get("whole_footprint"):
                args += ["--whole-footprint"]
            if body.get("strip_volumes"):
                args += ["--strip-volumes"]
            jid = uuid.uuid4().hex[:12]
            with JOBS_LOCK:
                JOBS[jid] = {"id": jid, "state": "queued", "log": [], "args": args}
            threading.Thread(target=run_job, args=(jid, args, "prepare_render.py"),
                             daemon=True).start()
            return self._send(200, {"job": jid})

        if u.path == "/api/acquire":
            need = ("name", "out", "ring")
            if any(k not in body for k in need):
                return self._send(400, {"error": f"need {need}"})
            ring = [list(to_lambert(lon, lat)) for lon, lat in body["ring"]]
            gj = HERE / "_selection.geojson"
            gj.write_text(json.dumps({"type": "Polygon", "coordinates": [ring]}),
                          encoding="utf-8")
            args = ["--name", body["name"], "--out", clean_path(body["out"]),
                    "--geojson", str(gj),
                    "--target", str(int(body.get("target", 40_000_000))),
                    "--raster-res", str(body.get("raster_res", 0.20)),
                    "--multiplier", str(body.get("multiplier", 1.0))]
            if body.get("voxel"):
                args += ["--voxel", str(body["voxel"])]
            if body.get("crop_to_shape"):
                args += ["--crop-to-shape"]
            jid = uuid.uuid4().hex[:12]
            with JOBS_LOCK:
                JOBS[jid] = {"id": jid, "state": "queued", "log": [], "args": args}
            threading.Thread(target=run_job, args=(jid, args), daemon=True).start()
            return self._send(200, {"job": jid})

        return self._send(404, {"error": "not found"})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    if port == 8765:
        print("Port 8765 belongs to rendafar. Its app would end up talking to "
              "this server and getting confusing errors. Refusing to start.")
        sys.exit(2)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        # Binding failure must be loud: a silent fallback is how one app ends
        # up answering another app's requests.
        print(f"Cannot listen on port {port}: {e}")
        print("Something else is already using it. Pass a different port:")
        print(f"    python server.py {port + 1}")
        sys.exit(2)
    print(f"IGN LiDAR Tiler running at http://localhost:{port}")
    print("Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
