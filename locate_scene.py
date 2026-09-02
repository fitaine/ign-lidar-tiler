"""Recover the Lambert 93 origin of a cloud whose origin was never recorded.

Some scenes predate the pipeline's `--origin` bookkeeping, and some had their
source tiles deleted years ago. Their .blend holds a recentred cloud and
nothing says where on Earth it sits, so it cannot be extended or densified.

This georeferences it by shape. It builds a ground-height raster from the
cloud (export_height_grid.py), fetches IGN's MNT over a search area, and finds
the translation that best aligns the two by masked normalised
cross-correlation. Terrain is distinctive enough that the peak is unambiguous
when it is right, and the reported score says whether to believe it.

    python locate_scene.py --grid grid.npz --centre 991745,6496487 --radius 4000

Elevation comes from ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES as a GeoTIFF,
which costs a few MB rather than the tens of GB that downloading candidate
LiDAR tiles would.
"""

import argparse
import sys
import urllib.parse
from pathlib import Path

import numpy as np

import net
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

WMS = "https://data.geopf.fr/wms-r"
DTM_LAYER = "ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES"
UA = {"User-Agent": "ign-lidar-tiler/0.1"}
NODATA = -1000.0


def fetch_dtm(minx, miny, maxx, maxy, cell, out):
    w = int(round((maxx - minx) / cell))
    h = int(round((maxy - miny) / cell))
    if max(w, h) > 4000:
        sys.exit(f"DTM request would be {w}x{h}px; the server caps at 4000. "
                 f"Use a bigger --cell or a smaller --radius.")
    q = {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap",
         "LAYERS": DTM_LAYER, "STYLES": "", "CRS": "EPSG:2154",
         "BBOX": f"{minx},{miny},{maxx},{maxy}",
         "WIDTH": str(w), "HEIGHT": str(h), "FORMAT": "image/geotiff"}
    url = f"{WMS}?{urllib.parse.urlencode(q)}"
    Path(out).write_bytes(net.get(url, timeout=300))
    a = np.array(Image.open(out)).astype(np.float64)
    a = np.flipud(a)                       # image row 0 is north; we want y up
    valid = a > NODATA
    if valid.mean() < 0.5:
        sys.exit("the DTM came back mostly nodata; check the search area")
    a[~valid] = np.nan
    return a, w, h


def box_mean(a, w, wt=None):
    """Windowed mean over a (w x w) box, via summed-area tables. `wt` is an
    optional weight/validity mask so invalid cells do not drag the mean."""
    if wt is None:
        wt = np.ones_like(a)
    def sat(x):
        c = np.cumsum(np.cumsum(np.pad(x, ((1, 0), (1, 0))), axis=0), axis=1)
        return c
    r = w // 2
    pa = np.pad(a * wt, r, mode="edge")
    pw = np.pad(wt, r, mode="edge")
    Sa, Sw = sat(pa), sat(pw)
    h, wd = a.shape
    ys, xs = np.arange(h), np.arange(wd)
    y0, y1 = ys[:, None], ys[:, None] + w
    x0, x1 = xs[None, :], xs[None, :] + w
    num = Sa[y1, x1] - Sa[y0, x1] - Sa[y1, x0] + Sa[y0, x0]
    den = Sw[y1, x1] - Sw[y0, x1] - Sw[y1, x0] + Sw[y0, x0]
    return num / np.maximum(den, 1e-9)


def highpass(a, mask, w):
    """Remove the large-scale trend. Over alpine terrain the regional slope
    dominates the raw elevations and correlates with everything, which makes
    the match surface flat and the peak meaningless. What identifies a place
    is the fine detail on top of that trend."""
    wt = mask.astype(np.float64)
    filled = np.where(mask, a, 0.0)
    return (filled - box_mean(filled, w, wt)) * wt


def xcorr(big, small):
    """Cross-correlation of `big` with `small`, same size as `big`, via FFT."""
    fb = np.fft.rfft2(big)
    fs = np.fft.rfft2(small, s=big.shape)
    return np.fft.irfft2(fb * np.conj(fs), s=big.shape)


def locate(cloud, mask, dtm, hp_window=41):
    """Masked normalised cross-correlation. Returns (score, dx, dy) in cells.

    The cloud's vertical datum is unknown, so both fields are compared with
    their means removed: the correlation is over shape, not absolute height.
    """
    ch, cw = cloud.shape
    if ch >= dtm.shape[0] or cw >= dtm.shape[1]:
        sys.exit("the search area must be larger than the cloud")

    dvalid_bool = np.isfinite(dtm)
    ch_hp = highpass(cloud, mask, hp_window)
    d_hp = highpass(np.nan_to_num(dtm, nan=0.0), dvalid_bool, hp_window)

    c = np.zeros_like(dtm)
    m = np.zeros_like(dtm)
    c[:ch, :cw] = ch_hp
    m[:ch, :cw] = mask.astype(np.float64)

    d = d_hp
    dvalid = dvalid_bool.astype(np.float64)

    n = xcorr(dvalid, m)                       # valid cells under the window
    num = xcorr(d, c)
    sd = xcorr(d, m)
    sd2 = xcorr(d * d, m)

    with np.errstate(invalid="ignore", divide="ignore"):
        var_d = sd2 - (sd * sd) / np.maximum(n, 1)
        denom = np.sqrt(np.maximum(var_d, 0.0)) * np.sqrt((c * c).sum())
        ncc = np.where((n > 0.6 * mask.sum()) & (denom > 0), num / denom, -1.0)

    # FFT correlation is circular, so offsets past the edge wrap around and
    # score against a window stitched from opposite sides of the search area.
    # Those are not real placements; only offsets that fit entirely inside it
    # are.
    valid = np.zeros_like(ncc, dtype=bool)
    valid[:dtm.shape[0] - ch + 1, :dtm.shape[1] - cw + 1] = True
    ncc = np.where(valid, ncc, -1.0)

    k = int(np.nanargmax(ncc))
    dy, dx = divmod(k, dtm.shape[1])
    return float(ncc.flat[k]), dx, dy, ncc


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--grid", required=True, help="npz from export_height_grid.py")
    p.add_argument("--centre", default=None, help="X,Y in Lambert 93 to search around")
    p.add_argument("--dtm-bbox", default=None,
                   help="minx,miny,maxx,maxy of a --dtm supplied externally, for "
                        "rasters that are not square around a centre")
    p.add_argument("--radius", type=float, default=4000.0,
                   help="Half-width of the search area in metres")
    p.add_argument("--cell", type=float, default=None,
                   help="DTM cell size; defaults to the cloud grid's")
    p.add_argument("--dtm", default=None, help="Reuse a DTM GeoTIFF instead of fetching")
    p.add_argument("--keep-dtm", default=None, help="Where to save the fetched DTM")
    p.add_argument("--no-verify", dest="verify", action="store_false",
                   help="Skip the cross-check at two other filter widths. The "
                        "check is what tells a real match from a lucky peak, so "
                        "only skip it when you already trust the answer.")
    p.add_argument("--hp-window", type=int, default=41,
                   help="High-pass window in cells; removes the regional slope "
                        "so the match is driven by terrain detail (default 41)")
    a = p.parse_args()

    g = np.load(a.grid, allow_pickle=True)
    cloud, mask = g["grid"], g["mask"]
    cell = float(a.cell or g["cell"])
    local_min = g["local_min"]
    ch, cw = cloud.shape
    print(f"cloud raster {cw} x {ch} at {cell} m "
          f"({100*mask.mean():.1f}% occupied)", flush=True)

    if a.dtm_bbox:
        minx, miny, maxx, maxy = (float(v) for v in a.dtm_bbox.split(","))
        if not a.dtm:
            sys.exit("--dtm-bbox only makes sense with --dtm")
    elif a.centre:
        cx, cy = (float(v) for v in a.centre.split(","))
        r = a.radius
        minx, miny = cx - r, cy - r
        maxx, maxy = cx + r, cy + r
    else:
        sys.exit("give --centre or --dtm-bbox")
    dtm_path = a.keep_dtm or "._dtm_search.tif"
    if a.dtm:
        dtm = np.flipud(np.array(Image.open(a.dtm)).astype(np.float64))
        dtm[dtm <= NODATA] = np.nan
        print(f"reusing {a.dtm}: {dtm.shape[1]} x {dtm.shape[0]}", flush=True)
    else:
        print(f"fetching the MNT over {minx:.0f},{miny:.0f} .. {maxx:.0f},{maxy:.0f} "
              f"at {cell} m ...", flush=True)
        dtm, _, _ = fetch_dtm(minx, miny, maxx, maxy, cell, dtm_path)
    dh, dw = dtm.shape

    score, dx, dy, ncc = locate(cloud, mask, dtm, hp_window=a.hp_window)

    # cell (dx,dy) is where the cloud's local (0,0) corner sits in the DTM
    ox = minx + dx * cell - local_min[0]
    oy = miny + dy * cell - local_min[1]

    # vertical offset: median difference over the overlapping valid cells
    sub = dtm[dy:dy + ch, dx:dx + cw]
    both = mask & np.isfinite(sub)
    oz = float(np.median(sub[both] - cloud[both])) if both.any() else float("nan")

    # how much better is the best peak than the rest?
    flat = np.sort(ncc[np.isfinite(ncc)].ravel())
    p99 = flat[int(0.99 * (flat.size - 1))]
    margin = score - p99

    print(f"\nbest match score {score:.4f}  (99th percentile {p99:.4f}, "
          f"margin {margin:+.4f})", flush=True)
    print(f"overlapping cells: {int(both.sum()):,} of {int(mask.sum()):,}", flush=True)
    print(f"\n  --origin {ox:.2f},{oy:.2f},{oz:.2f}", flush=True)
    print(f"  scene bbox {ox + local_min[0]:.0f},{oy + local_min[1]:.0f} .. "
          f"{ox + local_min[0] + cw*cell:.0f},{oy + local_min[1] + ch*cell:.0f}", flush=True)

    print(f"peak stands {score / p99:.1f}x above the 99th percentile", flush=True)

    # The absolute score cannot be the test on its own: it depends on how rough
    # the terrain is and how densely the cloud samples it. Millau, at 2 points
    # per square metre over a plateau, peaks at 0.32 and is right. What actually
    # separates a real match from a lucky one is whether it MOVES: the same
    # terrain matched through a different high-pass window lands in the same
    # place, a coincidence does not. So the tool now does the check that was
    # being done by hand.
    if a.verify:
        agree, spread_m = [], 0.0
        for hp in (max(11, a.hp_window // 2), a.hp_window * 2 + 1):
            s2, dx2, dy2, _ = locate(cloud, mask, dtm, hp_window=hp)
            ox2 = minx + dx2 * cell - local_min[0]
            oy2 = miny + dy2 * cell - local_min[1]
            moved = ((ox2 - ox) ** 2 + (oy2 - oy) ** 2) ** 0.5
            spread_m = max(spread_m, moved)
            agree.append(moved <= 1.5 * cell)
            print(f"  cross-check at --hp-window {hp}: "
                  f"{ox2:.2f},{oy2:.2f}  ({moved:.1f} m away, score {s2:.4f})",
                  flush=True)
        confirmed = all(agree)
    else:
        confirmed, spread_m = None, 0.0

    if confirmed:
        print(f"\nCONFIRMED. Two other filters put the origin within "
              f"{spread_m:.1f} m of this one, which a false peak does not do. "
              f"Verify by rendering before relying on it.", flush=True)
    elif confirmed is False:
        print(f"\nUNSTABLE: the origin moves by up to {spread_m:.0f} m when the "
              f"filter changes, so this peak is not the terrain. Widen --radius, "
              f"check the search centre, or use a finer --cell.", flush=True)
    elif score < 0.5 or margin < 0.05:
        print("\nUNVERIFIED and the peak is not commanding. Re-run without "
              "--no-verify before trusting this.", flush=True)
    else:
        print("\nStrong, isolated peak. Verify by rendering before relying on it.",
              flush=True)


if __name__ == "__main__":
    main()
