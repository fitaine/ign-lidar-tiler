"""Open a real Windows file or folder dialog and print the chosen path.

A browser cannot open a native picker and tell the page the real path, so the
in-page list of directories was the only option from the front end. It was
horrible to navigate. The server runs on the same machine, so it can open the
actual Windows dialog instead, with its sidebar, recent places, typing, and
everything else people expect.

Run as a separate short-lived process on purpose: Tk is not thread-safe and
the server answers requests on worker threads. A dedicated process sidesteps
that and cannot wedge the server if the dialog misbehaves. (Same reasoning as
rendafar's pick_file.py, which this follows.)

    python pick_path.py --kind file --ext .json --title "Choose scene.json"
    python pick_path.py --kind dir  --initial "C:/Users/.../3D"

Prints the selected path on stdout, or nothing if cancelled.
"""

import argparse
import os
import sys

EXT_LABELS = {
    ".json": "Scene manifest",
    ".blend": "Blender scene",
    ".ply": "Point cloud",
    ".tif": "GeoTIFF",
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--kind", choices=("file", "dir"), default="file")
    p.add_argument("--ext", default="", help="e.g. .json (files only)")
    p.add_argument("--title", default="")
    p.add_argument("--initial", default="")
    p.add_argument("--self-test", action="store_true",
                   help="Check Tk is usable without opening anything")
    a = p.parse_args()

    import tkinter as tk
    from tkinter import filedialog

    if a.self_test:
        root = tk.Tk()
        root.withdraw()
        root.destroy()
        sys.stdout.write("tk ok")
        return

    initial = a.initial
    if initial and not os.path.isdir(initial):
        initial = os.path.dirname(initial)
    if not initial or not os.path.isdir(initial):
        initial = os.path.expanduser("~")

    root = tk.Tk()
    root.withdraw()
    # Without this the dialog can open behind the browser window, which looks
    # exactly like nothing happened.
    root.attributes("-topmost", True)

    if a.kind == "dir":
        path = filedialog.askdirectory(
            parent=root,
            title=a.title or "Choose a folder",
            initialdir=initial,
            mustexist=False,
        )
    else:
        ext = a.ext.strip()
        if ext:
            label = EXT_LABELS.get(ext.lower(), ext.lstrip(".").upper() + " file")
            types = [(label, "*" + ext), ("All files", "*.*")]
        else:
            types = [("All files", "*.*")]
        path = filedialog.askopenfilename(
            parent=root,
            title=a.title or "Choose a file",
            initialdir=initial,
            filetypes=types,
        )
    root.destroy()

    if path:
        sys.stdout.write(os.path.normpath(path))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.stderr.write(str(e))
        sys.exit(1)
