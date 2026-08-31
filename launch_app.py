"""Start the IGN LiDAR Tiler server if it is not up, then open the browser.

This is what the desktop shortcut runs. It is idempotent: double-clicking the
icon while the server is already running just opens a new tab, it does not
start a second server or complain.

The server gets its own minimised console window rather than being hidden
outright, so there is an obvious way to stop it: close that window.
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


def alert(message):
    """There is no console behind a desktop icon, so errors go in a dialog."""
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, message, "IGN LiDAR Tiler", 0x10)


def responding(timeout=1.0):
    try:
        with urllib.request.urlopen(URL, timeout=timeout):
            return True
    except (urllib.error.URLError, OSError):
        return False


def start_server():
    """Launch server.py in its own minimised console window.

    CREATE_NEW_CONSOLE already detaches the child from this process's console,
    and it cannot be combined with DETACHED_PROCESS: Windows rejects the pair
    with "the parameter is incorrect".
    """
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 7  # SW_SHOWMINNOACTIVE
    interpreter = sys.executable.replace("pythonw.exe", "python.exe")
    return subprocess.Popen(
        [interpreter, str(HERE / "server.py")],
        cwd=str(HERE),
        startupinfo=startupinfo,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        close_fds=True,
    )


def main():
    if not responding():
        try:
            start_server()
        except OSError as exc:
            alert(f"Could not start the server:\n\n{exc}")
            return 1

        deadline = time.time() + STARTUP_TIMEOUT
        while time.time() < deadline:
            if responding():
                break
            time.sleep(0.4)
        else:
            alert(f"The server did not answer on port {PORT} within "
                  f"{STARTUP_TIMEOUT:.0f} seconds.\n\n"
                  f"Check the server window for the error.")
            return 1

    webbrowser.open(URL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
