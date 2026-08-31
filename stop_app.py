"""Stop the IGN LiDAR Tiler server.

The server runs windowless, so there is nothing to close by hand. This finds
it and ends it, quietly: no dialog when it works, and none when there was
nothing running either. Only a real failure is worth interrupting for.

The PID file is a hint, not the truth — it goes stale if the machine was
rebooted and another process inherited the number. Whoever is actually
listening on the port is the truth, so that is checked first, and nothing is
killed unless it is a Python process.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from server import PORT

PIDFILE = HERE / "server.pid"
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW}


def alert(message):
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, message, "IGN LiDAR Tiler", 0x10)


def run(args):
    return subprocess.run(args, capture_output=True, text=True, **NO_WINDOW).stdout


def listening_pid():
    """PID of whatever holds the port, via netstat so nothing extra is needed."""
    for line in run(["netstat", "-ano", "-p", "TCP"]).splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{PORT}"):
            return int(parts[4])
    return None


def is_python(pid):
    out = run(["tasklist", "/FI", f"PID eq {pid}", "/NH"])
    return "python" in out.lower()


def main():
    pid = listening_pid()
    if pid is None:
        recorded = PIDFILE.read_text(encoding="utf-8").strip() if PIDFILE.exists() else ""
        PIDFILE.unlink(missing_ok=True)
        if recorded.isdigit() and is_python(int(recorded)):
            pid = int(recorded)          # alive but not listening yet, still ours
        else:
            return 0                     # nothing to stop

    if not is_python(pid):
        alert(f"Port {PORT} is held by process {pid}, which is not Python.\n\n"
              "Leaving it alone.")
        return 1

    result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                            capture_output=True, text=True, **NO_WINDOW)
    if result.returncode != 0:
        alert(f"Could not stop the server (process {pid}):\n\n"
              + (result.stderr or result.stdout).strip())
        return 1

    PIDFILE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
