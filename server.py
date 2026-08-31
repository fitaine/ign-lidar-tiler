"""Local web app: pick an area on a map, get a scene.

Stage 1 UI of the IGN LiDAR Tiler (see PLAN.md):

    python server.py            # then open http://localhost:8765

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
from urllib.parse import parse_qs, urlparse

import fetch_tiles
from lambert93 import to_lambert, to_wgs84

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
PORT = 8765

JOBS = {}
JOBS_LOCK = threading.Lock()


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


def run_job(job_id, args):
    with JOBS_LOCK:
        JOBS[job_id]["state"] = "running"
    proc = subprocess.Popen([sys.executable, "-u", str(HERE / "acquire.py")] + args,
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
            if (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) > 4e9:
                return self._send(200, {"type": "FeatureCollection", "features": [],
                                        "note": "zoom in to load the tile grid"})
            try:
                feats = fetch_tiles.list_tiles(bbox)
            except Exception as ex:
                return self._send(502, {"error": f"IGN WFS: {ex}"})
            return self._send(200, tiles_as_geojson(feats))
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

        if u.path == "/api/acquire":
            need = ("name", "out", "ring")
            if any(k not in body for k in need):
                return self._send(400, {"error": f"need {need}"})
            ring = [list(to_lambert(lon, lat)) for lon, lat in body["ring"]]
            gj = HERE / "_selection.geojson"
            gj.write_text(json.dumps({"type": "Polygon", "coordinates": [ring]}),
                          encoding="utf-8")
            args = ["--name", body["name"], "--out", body["out"],
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
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"IGN LiDAR Tiler running at http://localhost:{port}")
    print("Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
