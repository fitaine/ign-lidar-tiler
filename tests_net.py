"""Exercise net.download against a server that misbehaves like IGN's."""
import hashlib
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, r"C:/Users/Tiphaine/Pictures/3D/LIDAR PROJECT/ign-lidar-tiler")
import net

BLOB = os.urandom(3_000_000)
DIGEST = hashlib.sha256(BLOB).hexdigest()
state = {"n": 0}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _range(self):
        rng = self.headers.get("Range")
        if not rng or not rng.startswith("bytes="):
            return None
        return int(rng.split("=")[1].split("-")[0])

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(BLOB)))
        self.end_headers()

    def do_GET(self):
        state["n"] += 1
        n = state["n"]
        start = self._range() or 0

        if self.path == "/flaky":
            if n == 1:                       # first: a 502, like IGN gives
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if n == 2:                       # second: cut off mid-stream
                body = BLOB[start:]
                self.send_response(206 if start else 200)
                self.send_header("Content-Length", str(len(body)))
                if start:
                    self.send_header("Content-Range",
                                     f"bytes {start}-{len(BLOB)-1}/{len(BLOB)}")
                self.end_headers()
                self.wfile.write(body[:900_000])
                self.wfile.flush()
                self.close_connection = True
                return
            body = BLOB[start:]              # third: honour Range properly
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(body)))
            if start:
                self.send_header("Content-Range",
                                 f"bytes {start}-{len(BLOB)-1}/{len(BLOB)}")
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/norange":          # server ignores Range entirely
            self.send_response(200)
            self.send_header("Content-Length", str(len(BLOB)))
            self.end_headers()
            self.wfile.write(BLOB)
            return

        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


srv = ThreadingHTTPServer(("127.0.0.1", 8799), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.4)

tmp = Path(r"C:/Users/Tiphaine/AppData/Local/Temp/claude/C--Users-Tiphaine-Pictures-3D-LIDAR-PROJECT/ba0927d0-cb36-49b7-8536-80ad29c3b581/scratchpad/nettest")
tmp.mkdir(exist_ok=True)
ok = True

def check(label, cond, extra=""):
    global ok
    ok = ok and cond
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{('  ' + extra) if extra else ''}")

# 1. a 502 then a truncated stream then success, resuming in between
dest = tmp / "flaky.bin"
if dest.exists():
    dest.unlink()
size = net.download("http://127.0.0.1:8799/flaky", dest, progress=False,
                    log=lambda m: None)
got = dest.read_bytes()
check("survives a 502 and a mid-stream cut", size == len(BLOB))
check("bytes are correct after resuming",
      hashlib.sha256(got).hexdigest() == DIGEST)
check("attempts used", state["n"] == 3, f"{state['n']} requests")
check("no .part left behind", not (tmp / "flaky.bin.part").exists())

# 2. a server that ignores Range must restart, not concatenate
state["n"] = 0
dest2 = tmp / "norange.bin"
if dest2.exists():
    dest2.unlink()
(tmp / "norange.bin.part").write_bytes(BLOB[:500_000])   # stale partial
net.download("http://127.0.0.1:8799/norange", dest2, progress=False,
             log=lambda m: None)
check("restarts cleanly when Range is ignored",
      hashlib.sha256(dest2.read_bytes()).hexdigest() == DIGEST)

# 3. a wrong expected size is caught, not accepted
state["n"] = 10
dest3 = tmp / "short.bin"
if dest3.exists():
    dest3.unlink()
try:
    net.download("http://127.0.0.1:8799/norange", dest3, expected=len(BLOB) + 99,
                 retries=2, progress=False, log=lambda m: None)
    check("rejects a size mismatch", False)
except IOError:
    check("rejects a size mismatch", True)
check("no truncated file is left as complete", not dest3.exists())

# 4. an already-complete file is skipped
state["n"] = 0
net.download("http://127.0.0.1:8799/flaky", dest, expected=len(BLOB),
             progress=False, log=lambda m: None)
check("skips a file that is already complete", state["n"] == 0)

# 5. 404 is not retried
state["n"] = 0
try:
    net.get("http://127.0.0.1:8799/nope", retries=4, log=lambda m: None)
    check("does not retry a 404", False)
except Exception:
    check("does not retry a 404", state["n"] == 1, f"{state['n']} requests")

srv.shutdown()
print("\nALL PASS" if ok else "\nFAILURES ABOVE")
sys.exit(0 if ok else 1)
