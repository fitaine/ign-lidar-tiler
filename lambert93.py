"""WGS84 <-> Lambert 93 (EPSG:2154), with no dependencies.

The map draws in longitude/latitude; tile selection, PDAL and every scene
origin are in Lambert 93. pyproj is not installed and this tool should not
require installing anything, so the projection is implemented directly.

Lambert Conformal Conic, 2 standard parallels, on GRS80:
    lat_0 46.5, lon_0 3.0, lat_1 44.0, lat_2 49.0, x_0 700000, y_0 6600000

Verified against gdaltransform from the QGIS install; see test_roundtrip().
"""

from math import atan, cos, exp, log, pi, sin, sqrt, tan, atan2

A = 6378137.0                      # GRS80 semi-major axis
F = 1.0 / 298.257222101            # GRS80 flattening
E = sqrt(2 * F - F * F)            # first eccentricity

LON0 = 3.0 * pi / 180.0
LAT0 = 46.5 * pi / 180.0
LAT1 = 44.0 * pi / 180.0
LAT2 = 49.0 * pi / 180.0
X0 = 700000.0
Y0 = 6600000.0


def _m(lat):
    return cos(lat) / sqrt(1 - E * E * sin(lat) ** 2)


def _t(lat):
    return tan(pi / 4 - lat / 2) / ((1 - E * sin(lat)) / (1 + E * sin(lat))) ** (E / 2)


_m1, _m2 = _m(LAT1), _m(LAT2)
_t1, _t2, _t0 = _t(LAT1), _t(LAT2), _t(LAT0)
N = log(_m1 / _m2) / log(_t1 / _t2)
BIG_F = _m1 / (N * _t1 ** N)
RHO0 = A * BIG_F * _t0 ** N


def to_lambert(lon_deg, lat_deg):
    """Longitude/latitude in degrees (WGS84) to Lambert 93 X, Y in metres."""
    lon = lon_deg * pi / 180.0
    lat = lat_deg * pi / 180.0
    rho = A * BIG_F * _t(lat) ** N
    theta = N * (lon - LON0)
    return X0 + rho * sin(theta), Y0 + RHO0 - rho * cos(theta)


def to_wgs84(x, y):
    """Lambert 93 X, Y in metres to longitude/latitude in degrees."""
    dx = x - X0
    dy = RHO0 - (y - Y0)
    rho = sqrt(dx * dx + dy * dy)
    if N < 0:
        rho = -rho
    theta = atan2(dx, dy)
    t = (rho / (A * BIG_F)) ** (1.0 / N)
    lat = pi / 2 - 2 * atan(t)
    for _ in range(12):                       # converges in a handful
        lat = pi / 2 - 2 * atan(t * ((1 - E * sin(lat)) / (1 + E * sin(lat))) ** (E / 2))
    lon = theta / N + LON0
    return lon * 180.0 / pi, lat * 180.0 / pi


def test_roundtrip():
    """Round-trip a spread of French coordinates; print the worst error."""
    worst = 0.0
    for x in range(100000, 1200001, 100000):
        for y in range(6100000, 7100001, 100000):
            lon, lat = to_wgs84(x, y)
            bx, by = to_lambert(lon, lat)
            worst = max(worst, abs(bx - x), abs(by - y))
    return worst


if __name__ == "__main__":
    print(f"round-trip worst error: {test_roundtrip()*1000:.4f} mm")
    for x, y, label in ((901500, 6419000, "Mont Aiguille"),
                        (700000, 6600000, "projection origin"),
                        (966000, 6537000, "Aravis origin")):
        lon, lat = to_wgs84(x, y)
        print(f"  {label:20} {x},{y} -> {lon:.6f}, {lat:.6f}")
