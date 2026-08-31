"""Start the IGN LiDAR Tiler server if it is not up, then open the browser.

This is what the desktop shortcut runs. It is idempotent: double-clicking the
icon while the server is already running just opens a new tab, it does not
start a second server or complain.

Nothing shows a console: the launcher runs under pythonw, and the server is
started with CREATE_NO_WINDOW. A windowless process has nowhere to print, so
its output is redirected to server.log — without that redirection the first
print() in the server would die on a missing stdout.

With no window there is no window to close, so the server's PID is recorded
in server.pid and stop_app.py shuts it down.
"""
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from server import PORT  # single source of truth for the port

URL = f"http://localhost:{PORT}/"
STARTUP_TIMEOUT = 30.0
LOG = HERE / "server.log"
PIDFILE = HERE / "server.pid"


def alert(message):
    """There is no console behind a desktop icon, so errors go in a dialog."""
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, message, "IGN LiDAR Tiler", 0x10)


def tail(path, lines=12):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no log was written)"
    return "".join(text.splitlines(keepends=True)[-lines:]) or "(the log is empty)"


def responding(timeout=1.0):
    try:
        with urllib.request.urlopen(URL, timeout=timeout):
            return True
    except (urllib.error.URLError, OSError):
        return False


def start_server():
    """Launch server.py with no console window, its output going to server.log."""
    log = open(LOG, "a", buffering=1, encoding="utf-8", errors="replace")
    log.write("\n--- started " + time.strftime("%Y-%m-%d %H:%M:%S") + " ---\n")
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "server.py")],
        cwd=str(HERE),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )
    PIDFILE.write_text(str(proc.pid), encoding="utf-8")
    return proc


def main():
    if not responding():
        try:
            start_server()
        except OSError as exc:
            alert("Could not start the server:" + "\n\n" + str(exc))
            return 1

        deadline = time.time() + STARTUP_TIMEOUT
        while time.time() < deadline:
            if responding():
                break
            time.sleep(0.4)
        else:
            alert(f"The server did not answer on port {PORT} within "
                  f"{STARTUP_TIMEOUT:.0f} seconds."
                  + "\n\n" + f"The end of {LOG.name}:" + "\n\n" + tail(LOG))
            return 1

    webbrowser.open(URL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
