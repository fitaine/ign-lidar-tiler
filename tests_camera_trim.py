"""Check the camera trim maths without Blender, on a hand-built projection.

The add-on cannot be imported in a plain interpreter, so bpy is stubbed: the
mask function only needs numpy plus a projection matrix and two transforms.
The camera sits at the origin looking down -Z, so a point's coordinates are
already camera-space and the expected answers are readable by eye.
"""
import sys
import types

import numpy as np

sys.path.insert(0, r"C:/Users/Tiphaine/Pictures/3D/LIDAR PROJECT/ign-lidar-tiler/blender_addon")


def stub_bpy():
    bpy = types.ModuleType("bpy")
    prop = lambda *a, **k: None
    props = types.ModuleType("bpy.props")
    props.FloatProperty = props.StringProperty = prop
    tps = types.ModuleType("bpy.types")
    tps.Operator = type("Operator", (), {})
    tps.Panel = type("Panel", (), {})
    bpy.props, bpy.types = props, tps
    sys.modules.update({"bpy": bpy, "bpy.props": props, "bpy.types": tps})


stub_bpy()
import ign_lidar_tiler as addon


class Mat(list):
    def inverted(self):
        return Mat(np.linalg.inv(np.array(self)).tolist())

    def __matmul__(self, other):
        return Mat((np.array(self) @ np.array(other)).tolist())


IDENTITY = Mat(np.eye(4).tolist())
HALF_X, HALF_Y = 0.5, 0.28   # tan of the half field of view, per axis


def projection(near=0.1, far=1000.0):
    r, t = HALF_X * near, HALF_Y * near
    return Mat([[near / r, 0, 0, 0],
                [0, near / t, 0, 0],
                [0, 0, -(far + near) / (far - near), -2 * far * near / (far - near)],
                [0, 0, -1, 0]])


class Camera:
    name = "Camera"
    matrix_world = IDENTITY

    def calc_matrix_camera(self, depsgraph, x, y, scale_x, scale_y):
        return projection()


class Cloud:
    type = "MESH"
    matrix_world = IDENTITY

    def __init__(self, points):
        flat = np.asarray(points, dtype=np.float32).ravel().tolist()

        class Verts:
            def __len__(self):
                return len(flat) // 3

            def foreach_get(self, _attr, buf):
                buf[:] = flat

        self.data = types.SimpleNamespace(vertices=Verts())


class Context:
    scene = types.SimpleNamespace(render=types.SimpleNamespace(
        resolution_x=1920, resolution_y=1080, pixel_aspect_x=1.0, pixel_aspect_y=1.0))

    def evaluated_depsgraph_get(self):
        return None


# label -> (point, kept at margin 0, kept at margin 0.15)
CASES = {
    "dead centre, 10 m ahead":      ((0.0, 0.0, -10.0), True, True),
    "behind the camera":            ((0.0, 0.0, 10.0), False, False),
    "just inside the right edge":   ((HALF_X * 10 * 0.98, 0.0, -10.0), True, True),
    "5% past the right edge":       ((HALF_X * 10 * 1.05, 0.0, -10.0), False, True),
    "10% past the right edge":      ((HALF_X * 10 * 1.10, 0.0, -10.0), False, True),
    "20% past the right edge":      ((HALF_X * 10 * 1.20, 0.0, -10.0), False, False),
    "well above the top edge":      ((0.0, HALF_Y * 10 * 2.0, -10.0), False, False),
    "below the bottom edge":        ((0.0, -HALF_Y * 10 * 1.5, -10.0), False, False),
}


class OrthoCamera(Camera):
    """Ortho projection: no perspective divide, w is a constant 1."""

    SCALE = 20.0  # ortho_scale, so the frame is +/- 10 m across the wide axis

    def calc_matrix_camera(self, depsgraph, x, y, scale_x, scale_y):
        half_x, half_y = self.SCALE / 2, self.SCALE / 2 * (HALF_Y / HALF_X)
        near, far = 0.1, 1000.0
        return Mat([[1 / half_x, 0, 0, 0],
                    [0, 1 / half_y, 0, 0],
                    [0, 0, -2 / (far - near), -(far + near) / (far - near)],
                    [0, 0, 0, 1]])


ORTHO_CASES = {
    "inside the ortho frame":   ((9.0, 0.0, -10.0), True),
    "outside the ortho frame":  ((11.0, 0.0, -10.0), False),
    "same X but far away":      ((9.0, 0.0, -900.0), True),
    "behind the camera":        ((0.0, 0.0, 10.0), False),
}


def check_ortho():
    cloud = Cloud([p for p, _ in ORTHO_CASES.values()])
    keep, _, _ = addon.camera_keep_mask(cloud, OrthoCamera(), Context(), 0.0)
    failures = 0
    for (label, (_, want)), got in zip(ORTHO_CASES.items(), keep):
        if bool(got) != want:
            failures += 1
            print(f"FAIL  ortho  {label}: kept={bool(got)}, expected {want}")
        else:
            print(f"ok    ortho  {label}")
    return failures


def main():
    cloud = Cloud([p for p, _, _ in CASES.values()])
    cam, ctx = Camera(), Context()
    failures = 0

    for margin, column in ((0.0, 1), (0.15, 2)):
        keep, _, n = addon.camera_keep_mask(cloud, cam, ctx, margin)
        assert n == len(CASES)
        for (label, case), got in zip(CASES.items(), keep):
            want = case[column]
            if bool(got) != want:
                failures += 1
                print(f"FAIL  margin {margin:.2f}  {label}: kept={bool(got)}, expected {want}")
            else:
                print(f"ok    margin {margin:.2f}  {label}")

    # distance gates, on the centre point 10 m out
    single = Cloud([(0.0, 0.0, -10.0)])
    for kwargs, want in (({"near": 20.0}, False), ({"near": 5.0}, True),
                         ({"far": 5.0}, False), ({"far": 50.0}, True)):
        keep, _, _ = addon.camera_keep_mask(single, cam, ctx, 0.15, **kwargs)
        if bool(keep[0]) != want:
            failures += 1
            print(f"FAIL  {kwargs}: kept={bool(keep[0])}, expected {want}")
        else:
            print(f"ok    {kwargs}")

    failures += check_ortho()

    print("\nall good" if not failures else f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
