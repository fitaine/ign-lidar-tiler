"""Crop a dense PLY to the occupancy mask exported from a carved .blend.

Stage 3a of the IGN LiDAR Tiler (see PLAN.md):

    python crop_to_mask.py --ply dense.ply --mask mask.npz --out dense-cropped.ply

Streams the PLY in chunks so a 150M-point file never has to be held in memory.
Both files must share the same --origin, since the mask is built from the
mesh's local coordinates, which are the PLY's coordinates.
"""

import argparse
import sys
from pathlib import Path

import numpy as np

# X,Y,Z float32 + R,G,B uint8, which is what densify.py writes.
DTYPE = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
                  ("red", "u1"), ("green", "u1"), ("blue", "u1")])
CHUNK = 8_000_000


def read_header(path):
    """Return (n_points, header_length). Only the layout densify.py writes."""
    with open(path, "rb") as f:
        raw = f.read(4096)
    end = raw.find(b"end_header\n")
    if end < 0:
        sys.exit(f"{path}: no end_header found in the first 4 KB")
    header = raw[:end].decode("ascii", errors="replace")
    if "binary_little_endian" not in header:
        sys.exit(f"{path}: only binary_little_endian is supported")
    n = None
    for line in header.splitlines():
        if line.startswith("element vertex"):
            n = int(line.split()[-1])
    if n is None:
        sys.exit(f"{path}: no 'element vertex' line")
    props = [l for l in header.splitlines() if l.startswith("property")]
    expected = ["property float32 x", "property float32 y", "property float32 z",
                "property uint8 red", "property uint8 green", "property uint8 blue"]
    norm = [p.replace("float ", "float32 ").replace("uchar ", "uint8 ") for p in props]
    if norm != expected:
        sys.exit(f"{path}: unexpected property layout:\n  " + "\n  ".join(props))
    return n, end + len(b"end_header\n")


def write_header(f, n):
    f.write(b"ply\nformat binary_little_endian 1.0\n")
    f.write(b"comment cropped to a Blender carve mask by crop_to_mask.py\n")
    f.write(f"element vertex {n}\n".encode())
    f.write(b"property float x\nproperty float y\nproperty float z\n")
    f.write(b"property uchar red\nproperty uchar green\nproperty uchar blue\n")
    f.write(b"end_header\n")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ply", required=True, help="Dense PLY to crop")
    p.add_argument("--mask", required=True, help="mask.npz from extract_mask.py")
    p.add_argument("--out", required=True, help="Output PLY")
    p.add_argument("--origin", default=None,
                   help="X,Y,Z the dense PLY was recentred by, checked against the mask")
    p.add_argument("--dilate", action="store_true",
                   help="Also keep the 26 neighbouring cells, softening the mask edge")
    a = p.parse_args()

    m = np.load(a.mask, allow_pickle=True)
    keys, mn, dims, cell = m["keys"], m["mn"], m["dims"], float(m["cell"])
    mask_origin = m["origin"]

    if a.origin is not None and not np.isnan(mask_origin).any():
        want = np.array([float(v) for v in a.origin.split(",")], dtype=np.float64)
        if not np.allclose(want, mask_origin, atol=1e-3):
            sys.exit(f"origin mismatch: mask was built at {mask_origin}, "
                     f"PLY says {want}. The crop would be misaligned.")

    if a.dilate:
        offs = np.array([(i, j, k) for i in (-1, 0, 1) for j in (-1, 0, 1)
                         for k in (-1, 0, 1)], dtype=np.int64)
        ix = keys // (dims[1] * dims[2])
        iy = (keys // dims[2]) % dims[1]
        iz = keys % dims[2]
        base = np.stack([ix, iy, iz], axis=1)
        grown = (base[:, None, :] + offs[None, :, :]).reshape(-1, 3)
        ok = np.all((grown >= 0) & (grown < dims), axis=1)
        grown = grown[ok]
        keys = np.unique((grown[:, 0] * dims[1] + grown[:, 1]) * dims[2] + grown[:, 2])
        print(f"[crop] dilated mask to {keys.size:,} cells", flush=True)

    keys = np.sort(keys)
    n_in, offset = read_header(a.ply)
    src = Path(a.ply)
    tmp = Path(a.out).with_suffix(".partial")

    kept = 0
    with open(src, "rb") as fin, open(tmp, "wb") as fout:
        fin.seek(offset)
        done = 0
        while done < n_in:
            take = min(CHUNK, n_in - done)
            buf = np.fromfile(fin, dtype=DTYPE, count=take)
            if buf.size == 0:
                break
            xyz = np.stack([buf["x"], buf["y"], buf["z"]], axis=1).astype(np.float64)
            idx = np.floor((xyz - mn) / cell).astype(np.int64)
            inside = np.all((idx >= 0) & (idx < dims), axis=1)
            k = np.full(buf.size, -1, dtype=np.int64)
            ii = idx[inside]
            k[inside] = (ii[:, 0] * dims[1] + ii[:, 1]) * dims[2] + ii[:, 2]
            pos = np.searchsorted(keys, k)
            pos[pos >= keys.size] = 0
            hit = inside & (keys[pos] == k)
            buf[hit].tofile(fout)
            kept += int(hit.sum())
            done += buf.size
            print(f"[crop] {done:,}/{n_in:,}  kept {kept:,}", flush=True)

    out = Path(a.out)
    with open(out, "wb") as fout:
        write_header(fout, kept)
        with open(tmp, "rb") as fin:
            while True:
                block = fin.read(64 << 20)
                if not block:
                    break
                fout.write(block)
    tmp.unlink()

    print(f"[crop] {n_in:,} -> {kept:,} points ({100.0*kept/n_in:.1f}% kept)", flush=True)
    print(f"[crop] wrote {out}  ({out.stat().st_size/1e9:.2f} GB)", flush=True)


if __name__ == "__main__":
    main()
