"""IGN LiDAR Tiler — Blender add-on.

Tags a point cloud object with the scene it came from, so the render-prep step
can find it without being told which object to swap. Stage 2 of the plan.

Install: Edit > Preferences > Add-ons > Install..., pick this file, enable it.
The panel appears in Properties > Object > IGN LiDAR Tiler.

The tag is a custom property on the object, not a naming convention: names get
changed, and a rename should not silently break the render pipeline.

The radius shown here is the RAW value. Blender displays the Mesh to Points
radius multiplied by the scene's unit scale, so a scene at scale_length 0.01
shows 0.00325 for a raw 0.325. Both numbers are printed so the panel value is
never a surprise.

Trim to camera deletes the points outside the frame, with a margin so the
shadow casters and the horizon just off-screen survive. Trimming the sparse
cloud is what lets the dense rebuild spend its whole point budget on what is
actually in the picture: the render-prep mask is voxelized from whatever
vertices are left here.
"""

bl_info = {
    "name": "IGN LiDAR Tiler",
    "author": "Tiphaine Buccino",
    "version": (0, 2, 0),
    "blender": (4, 0, 0),
    "location": "Properties > Object > IGN LiDAR Tiler",
    "description": "Tag a LiDAR point cloud with its scene, and trim it to the camera",
    "category": "Object",
}

import json
from pathlib import Path

import bpy
import numpy as np
from bpy.props import FloatProperty, StringProperty
from bpy.types import Operator, Panel

TAG_SCENE = "ign_lidar_scene"      # absolute path to scene.json
TAG_NAME = "ign_lidar_name"        # scene name, for readability
TAG_VARIANT = "ign_lidar_variant"  # which variant this object holds


def find_mesh_to_points(node_group, _seen=None):
    """Find the Mesh to Points node, following nested groups."""
    if node_group is None:
        return None, None
    _seen = _seen or set()
    if node_group.name in _seen:
        return None, None
    _seen.add(node_group.name)
    for nd in node_group.nodes:
        if nd.bl_idname == "GeometryNodeMeshToPoints":
            return node_group, nd
        if nd.bl_idname == "GeometryNodeGroup":
            g, n = find_mesh_to_points(nd.node_group, _seen)
            if n is not None:
                return g, n
    return None, None


def object_mesh_to_points(ob):
    for mod in ob.modifiers:
        ng = getattr(mod, "node_group", None)
        if ng is None:
            continue
        g, n = find_mesh_to_points(ng)
        if n is not None:
            return g, n
    return None, None


def load_manifest(ob):
    path = ob.get(TAG_SCENE)
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def find_manifest():
    """The scene.json belonging to the open .blend, if it is somewhere obvious.

    Scenes keep their manifest beside the clouds rather than beside the .blend,
    so look in the places it actually lives before asking.
    """
    if not bpy.data.filepath:
        return None
    folder = Path(bpy.data.filepath).parent
    for candidate in (folder / "scene.json",
                      folder / "LIDAR" / "scene.json",
                      folder / "LIDAR" / "output" / "scene.json",
                      folder.parent / "LIDAR" / "scene.json"):
        if candidate.is_file():
            return candidate
    for sub in sorted(folder.glob("*/scene.json")):
        return sub
    return None


def tagged_objects(context=None):
    """Every object in the file carrying a scene tag."""
    return [o for o in bpy.data.objects if o.get(TAG_SCENE)]


class IGNLT_OT_tag(Operator):
    bl_idname = "ignlt.tag"
    bl_label = "Tag with scene.json"
    bl_description = "Record which scene this cloud came from"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})

    def invoke(self, context, event):
        # The browser pre-fills the field with the CURRENT .blend and filter_glob
        # only filters the list, not what is typed. Clicking straight through
        # therefore handed the operator a .blend, which failed as a utf-8 decode
        # error deep in json - a message about nothing the reader did. Start on
        # the scene's own manifest when it can be found, and on the name when it
        # cannot.
        found = find_manifest()
        self.filepath = str(found) if found else "scene.json"
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        ob = context.object
        p = Path(self.filepath)
        if p.suffix.lower() != ".json":
            self.report({"ERROR"}, f"{p.name} is not a manifest: pick the "
                                   f"scene.json from the scene's LIDAR folder")
            return {"CANCELLED"}
        if not p.is_file():
            self.report({"ERROR"}, f"not a file: {p}")
            return {"CANCELLED"}
        try:
            man = json.loads(p.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            self.report({"ERROR"}, f"{p.name} is not text, so it is not a "
                                   f"scene.json. Pick the manifest instead.")
            return {"CANCELLED"}
        except Exception as e:
            self.report({"ERROR"}, f"could not read {p.name}: {e}")
            return {"CANCELLED"}
        if "variants" not in man or "origin" not in man:
            self.report({"ERROR"}, f"{p.name} is not a scene manifest")
            return {"CANCELLED"}

        ob[TAG_SCENE] = str(p)
        ob[TAG_NAME] = man.get("name", p.parent.name)
        # Guess which variant this object holds, by point count.
        n = len(ob.data.vertices) if ob.type == "MESH" else len(ob.data.points)
        best = min(man["variants"], key=lambda v: abs(v.get("points", 0) - n),
                   default=None)
        if best is not None:
            ob[TAG_VARIANT] = best.get("file", "")
        self.report({"INFO"}, f"tagged {ob.name} with {man.get('name')}")
        return {"FINISHED"}


class IGNLT_OT_untag(Operator):
    bl_idname = "ignlt.untag"
    bl_label = "Remove tag"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        ob = context.object
        for k in (TAG_SCENE, TAG_NAME, TAG_VARIANT):
            if k in ob.keys():
                del ob[k]
        return {"FINISHED"}


class IGNLT_OT_apply_radius(Operator):
    bl_idname = "ignlt.apply_radius"
    bl_label = "Apply the manifest radius"
    bl_description = ("Set Mesh to Points radius to this variant's derived value, "
                      "at which neighbouring spheres touch without overlapping")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        ob = context.object
        man = load_manifest(ob)
        if man is None:
            self.report({"ERROR"}, "no readable scene.json on this object")
            return {"CANCELLED"}
        var = next((v for v in man["variants"]
                    if v.get("file") == ob.get(TAG_VARIANT)), None)
        if var is None:
            self.report({"ERROR"}, "this object's variant is not in the manifest")
            return {"CANCELLED"}
        group, node = object_mesh_to_points(ob)
        if node is None:
            self.report({"ERROR"}, "no Mesh to Points node on this object")
            return {"CANCELLED"}
        sock = node.inputs["Radius"]
        if sock.is_linked:
            self.report({"ERROR"}, "Radius is linked; not overriding it")
            return {"CANCELLED"}
        old = sock.default_value
        sock.default_value = var["radius"]
        scale = context.scene.unit_settings.scale_length
        self.report({"INFO"}, f"radius raw {old:.4f} -> {var['radius']:.4f} "
                              f"(panel shows {var['radius']*scale:.5f})")
        return {"FINISHED"}


# attribute data_type -> (rna property name, components). Anything not listed
# is dropped on rebuild rather than guessed at.
ATTR_KIND = {
    "FLOAT": ("value", 1),
    "INT": ("value", 1),
    "BOOLEAN": ("value", 1),
    "FLOAT2": ("vector", 2),
    "FLOAT_VECTOR": ("vector", 3),
    "FLOAT_COLOR": ("color", 4),
    "BYTE_COLOR": ("color", 4),
}


def cloud_elements(data):
    """The point-domain collection, whichever kind of cloud this is."""
    return data.points if hasattr(data, "points") else data.vertices


def cloud_coords(ob):
    """(n, 3) float32 array of the object's points, in object space."""
    data = ob.data
    if ob.type == "POINTCLOUD":
        n = len(data.points)
        co = np.empty(n * 3, dtype=np.float32)
        data.points.foreach_get("position", co)
    else:
        n = len(data.vertices)
        co = np.empty(n * 3, dtype=np.float32)
        data.vertices.foreach_get("co", co)
    return co.reshape(n, 3), n


def copy_point_attributes(src, dst, keep, skip=()):
    """Carry every point-domain attribute across the mask: Col, radius, the lot."""
    n = int(keep.shape[0])
    for a in list(src.attributes):
        if a.domain != "POINT" or a.name in skip or a.name.startswith("."):
            continue
        kind = ATTR_KIND.get(a.data_type)
        if kind is None:
            continue
        prop, dim = kind
        dtype = {"INT": np.int32, "BOOLEAN": bool}.get(a.data_type, np.float32)
        buf = np.empty(n * dim, dtype=dtype)
        a.data.foreach_get(prop, buf)
        vals = buf.reshape(n, dim)[keep] if dim > 1 else buf[keep]
        tgt = dst.attributes.get(a.name)
        if tgt is None:
            tgt = dst.attributes.new(name=a.name, type=a.data_type, domain="POINT")
        tgt.data.foreach_set(prop, vals.ravel())


def camera_keep_mask(ob, cam, context, margin, near=0.0, far=0.0):
    """Which vertices fall inside the camera frame, with a margin.

    Returns (keep, coords, n). Done with numpy on the whole array: at 25M
    points a per-vertex Python loop is minutes, this is a couple of seconds.

    `margin` expands the frame as a fraction of its half-width, so 0.15 keeps
    a 15% band outside the picture. That band is what casts shadows into
    frame and closes the horizon, which is why trimming to exactly the frame
    looks wrong.
    """
    co, n = cloud_coords(ob)

    r = context.scene.render
    depsgraph = context.evaluated_depsgraph_get()
    proj = cam.calc_matrix_camera(
        depsgraph,
        x=r.resolution_x, y=r.resolution_y,
        scale_x=r.pixel_aspect_x, scale_y=r.pixel_aspect_y,
    )
    mv = cam.matrix_world.inverted() @ ob.matrix_world

    MV = np.array(mv, dtype=np.float64)
    P = np.array(proj, dtype=np.float64)

    # camera space first: needed for the in-front test and for distances
    view = co.astype(np.float64) @ MV[:3, :3].T + MV[:3, 3]
    # Blender cameras look down -Z, so anything in front has a negative z here
    infront = view[:, 2] < 0.0

    clip = view @ P[:3, :3].T + P[:3, 3]
    w = view @ P[3, :3] + P[3, 3]
    ortho = np.all(np.abs(P[3, :3]) < 1e-12)
    if ortho:
        w = np.ones_like(w)
    else:
        infront &= w > 0.0
    with np.errstate(invalid="ignore", divide="ignore"):
        ndc_x = clip[:, 0] / w
        ndc_y = clip[:, 1] / w

    lim = 1.0 + max(margin, 0.0)
    keep = infront & (np.abs(ndc_x) <= lim) & (np.abs(ndc_y) <= lim)

    if near > 0.0 or far > 0.0:
        dist = np.linalg.norm(view, axis=1)
        if near > 0.0:
            keep &= dist >= near
        if far > 0.0:
            keep &= dist <= far
    return keep, co, n


def rebuild_from_mask(ob, co, keep):
    """Replace the object's geometry with only the kept points.

    Rebuilding beats bpy.ops.mesh.delete on a cloud this size, and it is the
    only way to carry Col (and radius, on a point cloud) across without a
    round trip through edit mode.
    """
    kept = co[keep]
    k = int(kept.shape[0])
    old = ob.data

    if ob.type == "POINTCLOUD":
        new = bpy.data.pointclouds.new(old.name)
        try:
            new.points.add(k)
        except AttributeError:
            bpy.data.pointclouds.remove(new)
            raise RuntimeError(
                "this Blender build cannot resize a point cloud from Python; "
                "convert the object to a mesh (Object > Convert > Mesh) and trim that")
        new.points.foreach_set("position", kept.ravel())
        skip = ("position",)
    else:
        new = bpy.data.meshes.new(old.name)
        new.vertices.add(k)
        new.vertices.foreach_set("co", kept.ravel())
        skip = ("position",)

    for m in old.materials:
        new.materials.append(m)
    copy_point_attributes(old, new, keep, skip=skip)
    if hasattr(new, "update"):   # meshes need it, point clouds have no such call
        new.update()

    ob.data = new
    if old.users == 0:
        if ob.type == "POINTCLOUD":
            bpy.data.pointclouds.remove(old)
        else:
            bpy.data.meshes.remove(old)
    return k


class IGNLT_OT_trim_camera(Operator):
    bl_idname = "ignlt.trim_camera"
    bl_label = "Trim to camera"
    bl_description = ("Delete points outside the camera frame, keeping a margin "
                      "so shadow casters and the horizon survive")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        ob = context.object
        cam = context.scene.camera
        if cam is None or cam.type != "CAMERA":
            self.report({"ERROR"}, "the scene has no active camera")
            return {"CANCELLED"}
        if ob.type not in {"MESH", "POINTCLOUD"}:
            self.report({"ERROR"}, "select the point cloud object first")
            return {"CANCELLED"}
        if ob.mode != "OBJECT":
            self.report({"ERROR"}, "leave edit mode first, the trim rebuilds the object")
            return {"CANCELLED"}

        s = context.scene
        keep, co, n = camera_keep_mask(ob, cam, context, s.ignlt_margin,
                                       s.ignlt_near, s.ignlt_far)
        k = int(keep.sum())
        if k == 0:
            self.report({"ERROR"}, "that would delete everything; check the "
                                   "camera and the margin")
            return {"CANCELLED"}
        if k == n:
            self.report({"INFO"}, "everything is already inside the frame")
            return {"FINISHED"}

        try:
            rebuild_from_mask(ob, co, keep)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"},
                    f"kept {k:,} of {n:,} ({100.0*k/n:.1f}%), "
                    f"removed {n-k:,} outside the frame")
        return {"FINISHED"}


class IGNLT_OT_preview_camera(Operator):
    bl_idname = "ignlt.preview_camera"
    bl_label = "Count what would go"
    bl_description = "Report how much the trim would remove, changing nothing"

    def execute(self, context):
        ob = context.object
        cam = context.scene.camera
        if cam is None or cam.type != "CAMERA":
            self.report({"ERROR"}, "the scene has no active camera")
            return {"CANCELLED"}
        s = context.scene
        keep, _, n = camera_keep_mask(ob, cam, context, s.ignlt_margin,
                                      s.ignlt_near, s.ignlt_far)
        k = int(keep.sum())
        self.report({"INFO"},
                    f"would keep {k:,} of {n:,} ({100.0*k/n:.1f}%) "
                    f"through {cam.name!r} at {100*s.ignlt_margin:.0f}% margin")
        return {"FINISHED"}


class IGNLT_PT_panel(Panel):
    bl_label = "IGN LiDAR Tiler"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type in {"MESH", "POINTCLOUD"}

    def draw(self, context):
        layout = self.layout
        ob = context.object
        col = layout.column()

        n = len(ob.data.vertices) if ob.type == "MESH" else len(ob.data.points)
        col.label(text=f"{n:,} points", icon="OUTLINER_OB_POINTCLOUD")

        group, node = object_mesh_to_points(ob)
        if node is not None:
            raw = node.inputs["Radius"].default_value
            scale = context.scene.unit_settings.scale_length
            col.label(text=f"radius raw {raw:.4f}")
            if abs(scale - 1.0) > 1e-6:
                col.label(text=f"panel shows {raw*scale:.5f}  (unit scale {scale:g})",
                          icon="INFO")
        else:
            col.label(text="no Mesh to Points node found", icon="ERROR")

        col.separator()
        cam = context.scene.camera
        box = col.box()
        box.label(text="Trim to camera", icon="CAMERA_DATA")
        if cam is None:
            box.label(text="no active camera in the scene", icon="ERROR")
        else:
            box.label(text=f"through {cam.name}")
            box.prop(context.scene, "ignlt_margin")
            row = box.row(align=True)
            row.prop(context.scene, "ignlt_near")
            row.prop(context.scene, "ignlt_far")
            box.operator("ignlt.preview_camera", icon="VIEWZOOM")
            box.operator("ignlt.trim_camera", icon="TRASH")

        col.separator()
        man = load_manifest(ob)
        if not ob.get(TAG_SCENE):
            col.operator("ignlt.tag", icon="FILE_FOLDER")
            col.label(text="Untagged: render prep cannot find this cloud.")
            return

        if man is None:
            col.label(text="scene.json missing or unreadable:", icon="ERROR")
            col.label(text=str(ob.get(TAG_SCENE)))
            row = col.row()
            row.operator("ignlt.tag", text="Re-tag", icon="FILE_REFRESH")
            row.operator("ignlt.untag", icon="X")
            return

        box = col.box()
        box.label(text=f"scene: {man.get('name','?')}", icon="CHECKMARK")
        o = man.get("origin", [])
        if len(o) == 3:
            box.label(text=f"origin {o[0]:.0f}, {o[1]:.0f}, {o[2]:.2f}")
        box.label(text=f"variant: {ob.get(TAG_VARIANT,'?')}")
        for v in man.get("variants", []):
            mark = "> " if v.get("file") == ob.get(TAG_VARIANT) else "   "
            box.label(text=f"{mark}{v.get('role','?')}  {v.get('points',0):,} pts  "
                           f"voxel {v.get('voxel','?')}  r {v.get('radius','?')}")

        col.separator()
        col.operator("ignlt.apply_radius", icon="MOD_PHYSICS")
        col.operator("ignlt.untag", icon="X")


CLASSES = (IGNLT_OT_tag, IGNLT_OT_untag, IGNLT_OT_apply_radius,
           IGNLT_OT_preview_camera, IGNLT_OT_trim_camera, IGNLT_PT_panel)

PROPS = {
    "ignlt_margin": FloatProperty(
        name="Margin",
        description=("Band kept outside the frame, as a fraction of the frame's "
                     "half-width. 0 trims to exactly what is on camera; raise it "
                     "to keep the shadow casters just off-screen"),
        default=0.15, min=0.0, soft_max=1.0, step=1, precision=2, subtype="FACTOR"),
    "ignlt_near": FloatProperty(
        name="Near",
        description="Also drop anything closer to the camera than this, in metres. 0 = off",
        default=0.0, min=0.0, soft_max=500.0, unit="LENGTH"),
    "ignlt_far": FloatProperty(
        name="Far",
        description="Also drop anything further from the camera than this, in metres. 0 = off",
        default=0.0, min=0.0, soft_max=50000.0, unit="LENGTH"),
}


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    for name, prop in PROPS.items():
        setattr(bpy.types.Scene, name, prop)


def unregister():
    for name in PROPS:
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
