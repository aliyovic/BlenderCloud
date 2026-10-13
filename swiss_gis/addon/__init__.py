bl_info = {
    "name": "Swiss GIS Local", "author": "Ali Aouada", "version": (0, 1, 3),
    "blender": (5, 1, 0), "location": "3D View > Sidebar > Swiss GIS",
    "description": "Align local LV95 scans, terrain and aerial imagery", "category": "3D View",
}

from pathlib import Path
import json
import threading
import textwrap
import uuid

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
from bpy_extras.io_utils import ImportHelper, ExportHelper

from .core import Project, DataError, inspect_scan, prepare
from .scene import build, frame_area, scene_origin, scene_project_id, export_reference


_job = None
_epoch = 0


def _path(value):
    return bpy.path.abspath(value) if value else ""


def _manifest(scene):
    value = scene.get("swiss_gis_manifest_json")
    return json.loads(value) if value else None


class SWISS_GIS_Settings(bpy.types.PropertyGroup):
    scan: StringProperty(name="LAS / LAZ", subtype="FILE_PATH")
    terrain_folder: StringProperty(name="Terrain folder", subtype="DIR_PATH")
    imagery_folder: StringProperty(name="Aerial folder", subtype="DIR_PATH")
    margin: FloatProperty(name="Surroundings metres", default=50, min=1, max=2000)
    use_scan_bounds: BoolProperty(name="Use scan bounds", default=True)
    easting: StringProperty(name="LV95 east", default="2689160.3295")
    northing: StringProperty(name="LV95 north", default="1254971.5305")
    assume_lv95: BoolProperty(name="Assume LV95 when CRS is missing", default=True)
    vertical_datum: EnumProperty(name="Height datum", items=[
        ("UNKNOWN", "Unknown", "Preserve source heights without claiming a datum"),
        ("LN02", "LN02, user declared", "Use only when the scan supplier confirms LN02"),
        ("LHN95", "LHN95, user declared", "Use only when the scan supplier confirms LHN95"),
        ("ELLIPSOIDAL", "Ellipsoidal, user declared", "No height conversion is applied")], default="UNKNOWN")
    max_points: IntProperty(name="Preview point budget", default=750000, min=1000, max=5000000)
    terrain_step: FloatProperty(name="Terrain grid metres", default=2, min=.25, max=20)
    radius: FloatProperty(name="Point radius metres", default=.025, min=.001, max=.25, precision=3)
    status: StringProperty(default="Choose local scan and context files")


class SWISS_GIS_OT_inspect(bpy.types.Operator):
    bl_idname = "swiss_gis.inspect_scan"
    bl_label = "Read scan location"

    def execute(self, context):
        settings = context.scene.swiss_gis
        try:
            header = inspect_scan(_path(settings.scan), settings.assume_lv95)
            low, high = header["bounds"]
            settings.easting = f"{(low[0]+high[0])/2:.6f}"
            settings.northing = f"{(low[1]+high[1])/2:.6f}"
            settings.status = f"{header['point_count']:,} points, LV95" + (" assumed" if header["crs_assumed"] else " from header")
        except Exception as exc:
            settings.status = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class SWISS_GIS_OT_build(bpy.types.Operator):
    bl_idname = "swiss_gis.build_local"
    bl_label = "Build local scene"

    @classmethod
    def poll(cls, context):
        return _job is None

    def execute(self, context):
        global _job
        settings = context.scene.swiss_gis
        try:
            project_id = scene_project_id(context.scene)
            origin = scene_origin(context.scene)
            center = None if settings.use_scan_bounds else (float(settings.easting), float(settings.northing))
            identity = {"project_id": project_id} if project_id is not None else {}
            project = Project(scan=_path(settings.scan), terrain_folder=_path(settings.terrain_folder),
                              imagery_folder=_path(settings.imagery_folder), margin=settings.margin,
                              max_points=settings.max_points, terrain_step=settings.terrain_step,
                              vertical_datum=settings.vertical_datum, assume_lv95=settings.assume_lv95,
                              center=center, origin=origin, **identity)
            inspect_scan(project.scan, project.assume_lv95)
            cache = Path(bpy.utils.user_resource("DATAFILES", path="swiss_gis", create=True))
            destination = cache / uuid.uuid4().hex
            job = {"scene": context.scene, "view_layer": context.view_layer, "output": destination,
                   "radius": settings.radius, "epoch": _epoch, "progress": "Preparing local files",
                   "cancel": threading.Event(), "done": threading.Event()}
            def worker():
                try:
                    job["manifest"] = prepare(project, destination,
                                              lambda message: job.update(progress=message), job["cancel"].is_set)
                except Exception as exc:
                    job["error"] = str(exc)
                finally:
                    job["done"].set()
            _job = job
            settings.status = job["progress"]
            threading.Thread(target=worker, name="Swiss GIS local reader", daemon=True).start()
            bpy.app.timers.register(_poll_job, first_interval=.2)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            settings.status = str(exc)
            return {"CANCELLED"}
        return {"FINISHED"}


def _poll_job():
    global _job
    job = _job
    if job is None:
        return None
    scene = job["scene"]
    try:
        scene_present = scene.name in bpy.data.scenes
    except ReferenceError:
        scene_present = False
    if job["epoch"] != _epoch or not scene_present:
        job["cancel"].set()
        _job = None
        return None
    scene.swiss_gis.status = job["progress"]
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    if not job["done"].is_set():
        return .2
    try:
        if "error" in job:
            raise DataError(job["error"])
        if job["cancel"].is_set():
            raise DataError("Preparation canceled.")
        with bpy.context.temp_override(scene=scene, view_layer=job["view_layer"]):
            manifest = build(bpy.context, job["output"], job["radius"])
        scene.swiss_gis.status = f"Loaded {manifest['preview_points']:,} points; {len(manifest['warnings'])} data notes"
    except Exception as exc:
        scene.swiss_gis.status = str(exc)
        print("Swiss GIS:", exc)
    finally:
        _job = None
    return None


class SWISS_GIS_OT_cancel(bpy.types.Operator):
    bl_idname = "swiss_gis.cancel"
    bl_label = "Cancel preparation"

    def execute(self, context):
        if _job:
            _job["cancel"].set()
        return {"FINISHED"}


class SWISS_GIS_OT_open(bpy.types.Operator, ImportHelper):
    bl_idname = "swiss_gis.open_bundle"
    bl_label = "Open prepared local project"
    filter_glob: StringProperty(default="project.json", options={"HIDDEN"})

    def execute(self, context):
        try:
            build(context, Path(self.filepath).parent, context.scene.swiss_gis.radius)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class SWISS_GIS_OT_frame(bpy.types.Operator):
    bl_idname = "swiss_gis.frame"
    bl_label = "Frame selected area"
    top: BoolProperty(default=False)

    def execute(self, context):
        manifest = _manifest(context.scene)
        if manifest:
            frame_area(context, manifest, self.top)
        return {"FINISHED"}


class SWISS_GIS_OT_layer(bpy.types.Operator):
    bl_idname = "swiss_gis.toggle_layer"
    bl_label = "Show or hide project layer"
    role: StringProperty()

    def execute(self, context):
        bundle = context.scene.get("swiss_gis_bundle")
        objects = [obj for collection in bpy.data.collections if collection.get("swiss_gis_project") == bundle
                   for obj in collection.objects if obj.get("swiss_gis_role") == self.role and obj.name in context.view_layer.objects]
        hide = any(not obj.hide_get() for obj in objects)
        for obj in objects:
            obj.hide_set(hide)
            obj.hide_render = hide
        return {"FINISHED"}


class SWISS_GIS_OT_export(bpy.types.Operator, ExportHelper):
    bl_idname = "swiss_gis.export_reference"
    bl_label = "Export geographic reference"
    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})

    def execute(self, context):
        try:
            export_reference(context.scene, self.filepath)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class SWISS_GIS_PT_panel(bpy.types.Panel):
    bl_label = "Swiss GIS Local"
    bl_idname = "SWISS_GIS_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Swiss GIS"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.swiss_gis
        inputs = layout.column()
        inputs.enabled = _job is None
        inputs.prop(settings, "scan")
        inputs.operator("swiss_gis.inspect_scan", icon="VIEWZOOM")
        inputs.prop(settings, "assume_lv95")
        inputs.prop(settings, "vertical_datum")
        inputs.prop(settings, "terrain_folder")
        inputs.prop(settings, "imagery_folder")
        inputs.prop(settings, "use_scan_bounds")
        if not settings.use_scan_bounds:
            inputs.prop(settings, "easting")
            inputs.prop(settings, "northing")
        inputs.prop(settings, "margin")
        inputs.prop(settings, "max_points")
        inputs.prop(settings, "terrain_step")
        inputs.prop(settings, "radius")
        inputs.operator("swiss_gis.build_local", icon="IMPORT")
        if _job:
            layout.operator("swiss_gis.cancel", icon="CANCEL")
        layout.label(text=settings.status[:70])
        layout.operator("swiss_gis.open_bundle", icon="FILE_FOLDER")
        manifest = _manifest(context.scene)
        if manifest:
            box = layout.box()
            box.label(text="EPSG:2056, X east / Y north")
            box.label(text="Height datum: " + manifest["vertical_datum"])
            references = json.loads(context.scene.get("swiss_gis_source_references_json", "[]"))
            for reference in references:
                box.label(text="Source: " + ("©swisstopo" if reference == "Federal Office of Topography swisstopo" else reference))
            for axis, value in zip("ENH", manifest["origin"]):
                box.label(text=f"Origin {axis}: {value:.3f} m")
            row = box.row()
            row.operator("swiss_gis.frame", text="Area view")
            row.operator("swiss_gis.frame", text="Top view").top = True
            for role, label in (("scan", "Scan points"), ("scan_ground", "Observed scan ground"),
                                ("terrain", "Local terrain"), ("aerial_map", "Flat aerial map"), ("scan_plan", "Scan RGB plan")):
                objects = [obj for obj in context.scene.objects if obj.get("swiss_gis_role") == role
                           and any(collection.get("swiss_gis_project") == context.scene.get("swiss_gis_bundle") for collection in obj.users_collection)]
                if objects:
                    visible = any(not obj.hide_get() for obj in objects)
                    box.operator("swiss_gis.toggle_layer", text=label, icon="HIDE_OFF" if visible else "HIDE_ON").role = role
            box.operator("swiss_gis.export_reference", icon="EXPORT")
            chars = max(20, int((context.region.width - 32) / (7 * context.preferences.system.ui_scale)))
            for message in manifest["warnings"]:
                notes = box.column(align=True)
                for line in textwrap.wrap(message, width=chars):
                    notes.label(text=line)


@bpy.app.handlers.persistent
def _before_load(_):
    global _epoch, _job
    _epoch += 1
    if _job:
        _job["cancel"].set()
    _job = None


_classes = (SWISS_GIS_Settings, SWISS_GIS_OT_inspect, SWISS_GIS_OT_build, SWISS_GIS_OT_cancel,
            SWISS_GIS_OT_open, SWISS_GIS_OT_frame, SWISS_GIS_OT_layer, SWISS_GIS_OT_export, SWISS_GIS_PT_panel)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.swiss_gis = PointerProperty(type=SWISS_GIS_Settings)
    bpy.app.handlers.load_pre.append(_before_load)


def unregister():
    _before_load(None)
    if bpy.app.timers.is_registered(_poll_job):
        bpy.app.timers.unregister(_poll_job)
    if _before_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_before_load)
    del bpy.types.Scene.swiss_gis
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
