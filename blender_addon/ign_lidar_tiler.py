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
"""

bl_info = {
    "name": "IGN LiDAR Tiler",
    "author": "Tiphaine Buccino",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "Properties > Object > IGN LiDAR Tiler",
    "description": "Tag a LiDAR point cloud with its scene, for dense render prep",
    "category": "Object",
}

import json
from pathlib import Path

import bpy
from bpy.props import StringProperty
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
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        ob = context.object
        p = Path(self.filepath)
        if not p.is_file():
            self.report({"ERROR"}, f"not a file: {p}")
            return {"CANCELLED"}
        try:
            man = json.loads(p.read_text(encoding="utf-8"))
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


CLASSES = (IGNLT_OT_tag, IGNLT_OT_untag, IGNLT_OT_apply_radius, IGNLT_PT_panel)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
