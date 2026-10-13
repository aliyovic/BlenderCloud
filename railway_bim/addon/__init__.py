"""Railway BIM tools for fitted scans and editable IFC 4.3 projects."""

bl_info = {
    "name": "Railway BIM",
    "author": "Ali Aouada",
    "version": (0, 1, 2),
    "blender": (5, 1, 0),
    "location": "3D View > Sidebar > Railway BIM",
    "description": "Build and edit railway IFC models from fitted scan evidence",
    "category": "3D View",
}

import json
from pathlib import Path
import threading
import textwrap

try:
    import bpy
except ModuleNotFoundError:
    bpy = None


_job = None
_epoch = 0
_register_cache = {}


def _bonsai():
    if bpy is not None and not hasattr(bpy.types.Scene, "BIMProperties"):
        raise RuntimeError("Enable Bonsai before using Railway BIM.")
    try:
        import bonsai.tool as tool
    except ImportError as exc:
        raise RuntimeError("Enable Bonsai before using Railway BIM.") from exc
    return tool


def _selected_asset(context):
    obj = context.active_object
    if obj is None:
        return None
    tool = _bonsai()
    element = tool.Ifc.get_entity(obj)
    if element is None:
        return None
    import ifcopenshell.util.element

    psets = ifcopenshell.util.element.get_psets(element)
    evidence = psets.get("Pset_RailwayBIMAsset", {})
    if not evidence.get("AssetId"):
        return {"guid": element.GlobalId, "element": element, "obj": obj}
    return {"guid": element.GlobalId, "asset_id": evidence["AssetId"], "element": element, "obj": obj}


def _register_path(ifc_path):
    path = Path(ifc_path)
    return path.with_name(path.stem + ".asset-register.json")


def _asset_record(context):
    selected = _selected_asset(context)
    if selected is None:
        return None
    ifc_path = _bonsai().Ifc.get_path()
    path = _register_path(ifc_path)
    if not path.is_file():
        return None
    stat = path.stat()
    stamp = (str(path), stat.st_mtime_ns, stat.st_size)
    if _register_cache.get("stamp") != stamp:
        document = json.loads(path.read_text())
        _register_cache.clear()
        _register_cache.update(stamp=stamp, assets={asset["ifc_guid"]: asset for asset in document["assets"]})
    asset = _register_cache["assets"].get(selected["guid"])
    if asset:
        return {**selected, "record": asset, "ifc_path": ifc_path}
    return None


def _tag_redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _poll_job():
    global _job
    job = _job
    if job is None:
        return None
    if not job["done"]:
        return 0.2
    _job = None
    if job["epoch"] == _epoch and job["scene"] in bpy.data.scenes:
        settings = bpy.data.scenes[job["scene"]].railway_bim
        if job.get("error"):
            settings.status = "Failed: " + job["error"]
        else:
            result = job["result"]
            settings.last_result = json.dumps(result)
            if job["kind"] == "build":
                settings.status = "IFC created. Open it in Bonsai to inspect the assets."
            else:
                settings.status = "Validation: " + result.get("status", "complete")
    _tag_redraw()
    return None


def _start_job(context, kind, function, *args, **kwargs):
    global _job
    if _job is not None:
        raise RuntimeError("Wait for the current Railway BIM job to finish.")
    job = {"kind": kind, "scene": context.scene.name, "epoch": _epoch, "done": False}
    _job = job
    context.scene.railway_bim.status = "Building IFC..." if kind == "build" else "Checking IFC..."

    def work():
        try:
            job["result"] = function(*args, **kwargs)
        except Exception as exc:
            job["error"] = str(exc)
        finally:
            job["done"] = True

    threading.Thread(target=work, name="railway-bim-" + kind, daemon=True).start()
    if not bpy.app.timers.is_registered(_poll_job):
        bpy.app.timers.register(_poll_job, first_interval=0.2, persistent=True)


def _merge_reference_fact(saved, incoming, value_key=None):
    from copy import deepcopy

    ranks = {"UNKNOWN": 0, "ASSUMED": 1, "DECLARED": 2, "VERIFIED": 3}
    if (not isinstance(saved, dict) or not isinstance(incoming, dict)
            or saved.get("status") not in ranks or incoming.get("status") not in ranks):
        raise ValueError("The project reference fact has an invalid status.")
    if value_key:
        old, new = saved.get(value_key), incoming.get(value_key)
        if old not in (None, "", "UNKNOWN") and new not in (None, "", "UNKNOWN") and old != new:
            raise ValueError("The IFC has a conflicting project reference. Open it in a new scene.")
    merged = deepcopy(saved)
    for key, value in incoming.items():
        if key not in ("status", "evidence") and merged.get(key) in (None, "", "UNKNOWN"):
            merged[key] = deepcopy(value)
    if ranks[incoming["status"]] > ranks[saved["status"]]:
        merged["status"] = incoming["status"]
        merged["evidence"] = "\n".join(dict.fromkeys(value for value in
                                                  (saved.get("evidence", ""), incoming.get("evidence", "")) if value))
    return merged


def _merge_project_contract(saved, incoming):
    """Fill compatible IFC project fields while keeping saved settings and evidence."""
    from copy import deepcopy

    if saved is None:
        return deepcopy(incoming)
    if incoming is None:
        return deepcopy(saved)
    merged = deepcopy(saved)
    for key in ("tracks", "terrain_regions", "sampling"):
        value = incoming.get(key)
        if value and saved.get(key) and value != saved[key]:
            raise ValueError("The IFC has conflicting project " + key + ". Open it in a new scene.")
        if value and not saved.get(key):
            merged[key] = deepcopy(value)
    for key, value in incoming.items():
        merged.setdefault(key, deepcopy(value))
    old, new = saved.get("reference"), incoming.get("reference")
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise ValueError("The project geographic reference is invalid.")
    for key, value in new.items():
        if key in ("horizontal", "vertical", "official_chainage", "design_alignment", "gauge") and key in old:
            value_key = "crs" if key == "horizontal" else "datum" if key == "vertical" else None
            merged["reference"][key] = _merge_reference_fact(old[key], value, value_key)
        else:
            merged["reference"].setdefault(key, deepcopy(value))
    return merged


def _import_reference(scene, path, tool):
    """Check the shared metre frame before Bonsai changes the scene."""
    import ifcopenshell
    import ifcopenshell.util.element
    import ifcopenshell.util.placement
    import ifcopenshell.util.unit
    import numpy as np
    import uuid
    from .core import import_swiss_crop, validate_contract

    model = ifcopenshell.open(str(path))
    conversions = model.by_type("IfcMapConversion")
    if len(conversions) != 1 or conversions[0].is_a() != "IfcMapConversion":
        raise ValueError("Open requires one translation-only IFC map conversion.")
    conversion = conversions[0]
    horizontal_crs = (conversion.TargetCRS.Name or "UNKNOWN").strip().upper() or "UNKNOWN"
    retained_crs = scene.get("railway_ifc_horizontal_crs", "UNKNOWN").strip().upper() or "UNKNOWN"
    if retained_crs != "UNKNOWN" and horizontal_crs != "UNKNOWN" and retained_crs != horizontal_crs:
        raise ValueError("The retained IFC and incoming model use different horizontal coordinate systems.")
    map_unit = conversion.TargetCRS.MapUnit
    basis = ifcopenshell.util.unit.get_project_unit(model, "LENGTHUNIT")
    seen = set()
    while basis is not None and basis.is_a("IfcConversionBasedUnit"):
        if basis.id() in seen:
            raise ValueError("The IFC project length units contain a conversion cycle.")
        seen.add(basis.id())
        basis = basis.ConversionFactor.UnitComponent
    if (basis is None or not basis.is_a("IfcSIUnit") or basis.Name != "METRE"
            or ifcopenshell.util.unit.calculate_unit_scale(model) != 1.0
            or map_unit is not None and (not map_unit.is_a("IfcSIUnit")
                                        or map_unit.Name != "METRE" or map_unit.Prefix is not None)):
        raise ValueError("Open requires metre project and map units to share the scan frame.")
    axes_scale = [conversion.XAxisAbscissa if conversion.XAxisAbscissa is not None else 1.,
                  conversion.XAxisOrdinate if conversion.XAxisOrdinate is not None else 0.,
                  conversion.Scale if conversion.Scale is not None else 1.]
    if not np.allclose(axes_scale, [1., 0., 1.], atol=1e-12, rtol=0):
        raise ValueError("A rotated or scaled IFC map frame cannot share the scan scene. Open it separately.")
    context = conversion.SourceCRS
    if (not context.is_a("IfcGeometricRepresentationContext")
            or not np.allclose(ifcopenshell.util.placement.get_axis2placement(context.WorldCoordinateSystem),
                               np.eye(4), atol=1e-8, rtol=0)):
        raise ValueError("The IFC needs an unshifted engineering context to share the scan frame.")
    origin = [conversion.Eastings, conversion.Northings, conversion.OrthogonalHeight]
    if not np.isfinite(origin).all():
        raise ValueError("The IFC map origin must contain three finite coordinates.")
    height_datums = [conversion.TargetCRS.VerticalDatum]
    projects = model.by_type("IfcProject")
    if len(projects) != 1:
        raise ValueError("Open requires one IFC project.")
    project_id = ifcopenshell.util.element.get_psets(projects[0]).get("Pset_RailwayBIMProject", {}).get("ProjectId")
    if project_id is not None:
        project_id = str(uuid.UUID(project_id))

    contract = None
    project_path = path.with_suffix(".railway-project.json")
    if project_path.is_file():
        project = json.loads(project_path.read_text())
        if project.get("schema") == "railway-bim-project/2":
            contract = validate_contract(project["contract"], project["source_component_spec"], project["source_header"])
        elif project.get("schema") == "railway-bim-project/1":
            contract = import_swiss_crop(project["source_component_spec"], project["source_header"])
        else:
            raise ValueError("The railway project sidecar version is unsupported.")
        if project_id is not None and project_id != contract["project_id"]:
            raise ValueError("The railway project identity disagrees with the IFC.")
        project_id = contract["project_id"]
        if (not np.allclose(contract["reference"]["local_origin_m"], origin, atol=1e-6, rtol=0)
                or contract["reference"]["horizontal"].get("crs", "UNKNOWN") != conversion.TargetCRS.Name):
            raise ValueError("The railway project reference disagrees with the IFC map conversion.")
        vertical = contract["reference"]["vertical"]
        if vertical.get("status") != "UNKNOWN":
            height_datums.append(vertical.get("datum"))

    imported_height_datum = next((value.strip().upper() for value in height_datums
                                  if isinstance(value, str) and value.strip() and value.strip().upper() != "UNKNOWN"), "UNKNOWN")
    retained_project_id = scene.get("railway_ifc_project_id")
    if retained_project_id is not None:
        try:
            retained_project_id = str(uuid.UUID(retained_project_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("The retained IFC project identity is invalid.") from exc
        if project_id is not None and retained_project_id != project_id:
            raise ValueError("This IFC belongs to another project. Open it in a new scene.")
    anchors = []
    existing_contract = None
    if "railway_project_contract_json" in scene:
        existing_contract = json.loads(scene["railway_project_contract_json"])
        if (not isinstance(existing_contract, dict) or existing_contract.get("schema") != "railway-project-contract/1"
                or existing_contract.get("units") != "metre"):
            raise ValueError("The scene has an unsupported railway project reference.")
        identity = str(uuid.UUID(existing_contract["project_id"]))
        if retained_project_id is not None and identity != retained_project_id:
            raise ValueError("The saved contract and retained IFC belong to different projects.")
        if project_id is not None and identity != project_id:
            raise ValueError("This IFC belongs to another project. Open it in a new scene.")
        anchors.append(existing_contract["reference"]["local_origin_m"])
        crs = existing_contract["reference"]["horizontal"].get("crs")
        if crs and crs != "UNKNOWN" and crs != conversion.TargetCRS.Name:
            raise ValueError("The scene and IFC use different horizontal coordinate systems.")
        vertical = existing_contract["reference"]["vertical"]
        if vertical.get("status") != "UNKNOWN":
            height_datums.append(vertical.get("datum"))
    if "swiss_gis_origin_json" in scene:
        anchors.append(json.loads(scene["swiss_gis_origin_json"]))
    for key in ("scan_scene_origin", "LV95_origin_m"):
        if key in scene:
            anchors.append(list(scene[key]))
    if "swiss_gis_epsg" in scene and conversion.TargetCRS.Name != "EPSG:" + str(scene["swiss_gis_epsg"]):
        raise ValueError("The scene and IFC use different horizontal coordinate systems.")
    if not anchors and any("scan_cache" in obj for obj in scene.objects):
        raise ValueError("This scene contains an unreferenced scan. Open the IFC in a new scene.")
    if any(len(anchor) != 3 or not np.isfinite(anchor).all() for anchor in anchors):
        raise ValueError("The scene has an invalid geographic origin.")
    if any(not np.allclose(anchor, origin, atol=1e-6, rtol=0) for anchor in anchors):
        raise ValueError("The scene and IFC use different geographic origins. Open the IFC in a new scene.")
    height_datums.append(scene.get("swiss_gis_vertical_datum"))
    height_datums.append(scene.get("railway_ifc_vertical_datum"))
    height_datums.extend(obj.get("swiss_gis_terrain_vertical_datum") for obj in scene.objects)
    bundle = scene.get("swiss_gis_bundle")
    if bundle and any(obj.get("swiss_gis_role") == "terrain" and "swiss_gis_terrain_vertical_datum" not in obj
                      and any(collection.get("swiss_gis_project") == bundle for collection in obj.users_collection)
                      for obj in scene.objects):
        try:
            manifest = json.loads(scene.get("swiss_gis_manifest_json", "null"))
        except (ValueError, TypeError):
            manifest = None
        if isinstance(manifest, dict):
            terrain_datums = {source["vertical_datum"] for source in manifest.get("terrain_sources", [])
                              if source.get("vertical_datum") not in (None, "", "UNKNOWN")}
            if len(terrain_datums) == 1:
                height_datums.extend(terrain_datums)
    known_datums = {value.strip().upper() for value in height_datums
                    if isinstance(value, str) and value.strip() and value.strip().upper() != "UNKNOWN"}
    if len(known_datums) > 1:
        raise ValueError("The scene and IFC have conflicting known height datums. Open the IFC in a new scene.")
    if tool.Georeference.get_georeference_props().has_blender_offset:
        raise ValueError("Clear the existing Bonsai false origin before opening IFC in the shared scan frame.")
    return origin, _merge_project_contract(existing_contract, contract), imported_height_datum, project_id, horizontal_crs


if bpy is not None:
    from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty, StringProperty

    class RailwayBIMSettings(bpy.types.PropertyGroup):
        reconstruction_dir: StringProperty(name="Fitted crop folder", subtype="DIR_PATH")
        output_ifc: StringProperty(name="IFC file", subtype="FILE_PATH")
        replace_existing: BoolProperty(name="Replace existing output", default=False,
                                      description="Replace the generated IFC and its evidence files")
        status: StringProperty(default="Choose the fitted crop folder and output IFC file.")
        last_result: StringProperty(options={"HIDDEN"})
        parameter: EnumProperty(
            name="Dimension",
            items=[
                ("width_u", "Sleeper width", "Width along the track in metres"),
                ("length_v", "Sleeper length", "Length across the track in metres"),
                ("depth", "Sleeper depth", "Reconstruction depth in metres"),
                ("head_width_m", "Rail head width", "Reconstructed rail head width in metres"),
                ("height_m", "Mast visible height", "Height above the stored mast bottom in metres"),
            ],
        )
        value_m: FloatProperty(name="Value m", default=0.24, precision=4)

    class RAILWAY_BIM_OT_build(bpy.types.Operator):
        bl_idname = "railway_bim.build_ifc"
        bl_label = "Build railway IFC"
        bl_description = "Create IFC 4.3 assets from an existing fitted railway crop"

        def execute(self, context):
            try:
                from .core import build_ifc

                settings = context.scene.railway_bim
                if not settings.reconstruction_dir or not settings.output_ifc:
                    raise ValueError("Choose a fitted crop folder and an output IFC file.")
                _start_job(context, "build", build_ifc, bpy.path.abspath(settings.reconstruction_dir),
                           bpy.path.abspath(settings.output_ifc), overwrite=settings.replace_existing)
            except Exception as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            return {"FINISHED"}

    class RAILWAY_BIM_OT_open(bpy.types.Operator):
        bl_idname = "railway_bim.open_ifc"
        bl_label = "Open IFC in Bonsai"
        bl_description = "Import the railway IFC while retaining non-IFC scan objects"

        @classmethod
        def poll(cls, context):
            return _job is None

        def execute(self, context):
            try:
                tool = _bonsai()
                settings = context.scene.railway_bim
                path = Path(bpy.path.abspath(settings.output_ifc))
                if not path.is_file():
                    raise ValueError("Build or select an existing railway IFC first.")
                if tool.Ifc.get():
                    raise ValueError("An IFC project is already open. Open the generated .blend in another window.")
                origin, contract, height_datum, project_id, horizontal_crs = _import_reference(context.scene, path, tool)
                project_settings = tool.Project.get_project_props()
                false_origin_mode = project_settings.false_origin_mode
                project_settings.false_origin_mode = "DISABLED"
                try:
                    result = bpy.ops.bim.load_project(filepath=str(path), should_start_fresh_session=False)
                finally:
                    project_settings.false_origin_mode = false_origin_mode
                if "FINISHED" not in result:
                    raise RuntimeError("Bonsai did not import the IFC.")
                if "scan_scene_origin" not in context.scene:
                    context.scene["scan_scene_origin"] = origin
                if contract is not None:
                    context.scene["railway_project_contract_json"] = json.dumps(contract, allow_nan=False)
                if height_datum != "UNKNOWN" or "railway_ifc_vertical_datum" not in context.scene:
                    context.scene["railway_ifc_vertical_datum"] = height_datum
                if project_id is not None:
                    context.scene["railway_ifc_project_id"] = project_id
                if horizontal_crs != "UNKNOWN" or "railway_ifc_horizontal_crs" not in context.scene:
                    context.scene["railway_ifc_horizontal_crs"] = horizontal_crs
                settings.status = "IFC open. Select an asset to inspect or edit it."
            except Exception as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            return {"FINISHED"}

    class RAILWAY_BIM_OT_validate(bpy.types.Operator):
        bl_idname = "railway_bim.validate_ifc"
        bl_label = "Check saved IFC"
        bl_description = "Reopen the saved IFC and check its schema, assets, and coordinates"

        def execute(self, context):
            try:
                from .validation import validate_ifc

                settings = context.scene.railway_bim
                active_path = _bonsai().Ifc.get_path()
                path = Path(active_path or bpy.path.abspath(settings.output_ifc))
                if not path.is_file():
                    raise ValueError("Choose an existing IFC file.")
                report = path.with_name(path.stem + ".validation.json")
                reconstruction = bpy.path.abspath(settings.reconstruction_dir) if settings.reconstruction_dir else None
                _start_job(context, "validate", validate_ifc, str(path), reconstruction, report_path=str(report))
            except Exception as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            return {"FINISHED"}

    class RAILWAY_BIM_OT_read_dimension(bpy.types.Operator):
        bl_idname = "railway_bim.read_dimension"
        bl_label = "Read selected dimension"

        @classmethod
        def poll(cls, context):
            return _job is None

        def execute(self, context):
            try:
                selected = _asset_record(context)
                if selected is None:
                    raise ValueError("Select an asset built by Railway BIM.")
                parameters = selected["record"]["parameters"]
                settings = context.scene.railway_bim
                if settings.parameter == "height_m":
                    value = parameters["z_top"] - parameters["z_bottom"]
                else:
                    value = parameters[settings.parameter]
                settings.value_m = value
                settings.status = "Read dimension from " + selected["record"]["asset_id"]
            except KeyError:
                self.report({"ERROR"}, "That dimension does not apply to the selected asset.")
                return {"CANCELLED"}
            except Exception as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            return {"FINISHED"}

    class RAILWAY_BIM_OT_rebuild(bpy.types.Operator):
        bl_idname = "railway_bim.rebuild_asset"
        bl_label = "Apply dimension and save IFC"
        bl_description = "Save current IFC edits, change the selected asset, and refresh its geometry"

        @classmethod
        def poll(cls, context):
            return _job is None

        def execute(self, context):
            try:
                from .core import rebuild_asset, validate_asset_changes
                import ifcopenshell
                import ifcopenshell.util.representation
                import bonsai.core.geometry
                import bonsai.bim.handler

                selected = _asset_record(context)
                if selected is None:
                    raise ValueError("Select an asset built by Railway BIM.")
                if context.mode != "OBJECT":
                    raise ValueError("Finish mesh editing before changing a reconstruction dimension.")
                settings = context.scene.railway_bim
                path = selected["ifc_path"]
                changes = validate_asset_changes(selected["record"], {settings.parameter: settings.value_m})
                tool = _bonsai()
                result = bpy.ops.bim.save_project(filepath=path)
                if "FINISHED" not in result:
                    raise RuntimeError("Bonsai did not save the current IFC edits.")
                result = rebuild_asset(path, selected["record"]["asset_id"], changes)
                model = ifcopenshell.open(path)
                tool.Ifc.set(model)
                tool.Ifc.clear_history()
                element = model.by_guid(selected["guid"])
                obj = selected["obj"]
                tool.Ifc.link(element, obj)
                representation = ifcopenshell.util.representation.get_representation(element, "Model", "Body", "MODEL_VIEW")
                bonsai.core.geometry.switch_representation(tool.Ifc, tool.Geometry, obj=obj,
                                                          representation=representation, apply_openings=True)
                tool.Geometry.record_object_position(obj)
                tool.Ifc.finish_edit(obj)
                bonsai.bim.handler.refresh_ui_data()
                settings.last_result = json.dumps(result)
                settings.status = "Saved dimension change for " + selected["record"]["asset_id"]
            except Exception as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            return {"FINISHED"}

    class RAILWAY_BIM_PT_panel(bpy.types.Panel):
        bl_label = "Railway BIM"
        bl_idname = "RAILWAY_BIM_PT_panel"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "Railway BIM"

        def draw(self, context):
            layout = self.layout
            settings = context.scene.railway_bim
            inputs = layout.column()
            inputs.enabled = _job is None
            inputs.prop(settings, "reconstruction_dir")
            inputs.prop(settings, "output_ifc")
            inputs.prop(settings, "replace_existing")
            inputs.operator("railway_bim.build_ifc", icon="OUTLINER_OB_GROUP_INSTANCE")
            inputs.operator("railway_bim.open_ifc", icon="FILE_FOLDER")
            inputs.operator("railway_bim.validate_ifc", icon="CHECKMARK")
            chars = max(24, int((context.region.width - 32) / (7 * context.preferences.system.ui_scale)))
            for line in textwrap.wrap(settings.status, width=chars):
                layout.label(text=line)
            try:
                selected = _asset_record(context)
            except Exception:
                selected = None
            if selected:
                record = selected["record"]
                box = layout.box()
                box.label(text=record["asset_id"])
                box.label(text="Select the dimension that applies to this asset.")
                box.prop(settings, "parameter")
                box.operator("railway_bim.read_dimension")
                box.prop(settings, "value_m")
                row = box.row()
                row.enabled = _job is None
                row.operator("railway_bim.rebuild_asset")
            if settings.last_result:
                try:
                    result = json.loads(settings.last_result)
                    for note in result.get("review_items", []):
                        message = note.get("detail", str(note)) if isinstance(note, dict) else str(note)
                        for line in textwrap.wrap(message, width=chars):
                            layout.label(text=line, icon="INFO")
                except ValueError:
                    pass

    @bpy.app.handlers.persistent
    def _before_load(_):
        global _epoch
        _epoch += 1
        _register_cache.clear()

    _classes = (RailwayBIMSettings, RAILWAY_BIM_OT_build, RAILWAY_BIM_OT_open,
                RAILWAY_BIM_OT_validate, RAILWAY_BIM_OT_read_dimension, RAILWAY_BIM_OT_rebuild,
                RAILWAY_BIM_PT_panel)

    def register():
        for cls in _classes:
            bpy.utils.register_class(cls)
        bpy.types.Scene.railway_bim = PointerProperty(type=RailwayBIMSettings)
        bpy.app.handlers.load_pre.append(_before_load)
        if _job is not None and not bpy.app.timers.is_registered(_poll_job):
            bpy.app.timers.register(_poll_job, first_interval=0.2, persistent=True)

    def unregister():
        _before_load(None)
        if bpy.app.timers.is_registered(_poll_job):
            bpy.app.timers.unregister(_poll_job)
        if _before_load in bpy.app.handlers.load_pre:
            bpy.app.handlers.load_pre.remove(_before_load)
        del bpy.types.Scene.railway_bim
        for cls in reversed(_classes):
            bpy.utils.unregister_class(cls)
