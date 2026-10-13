"""Check IFC Open against real Scan Viewer and Swiss GIS coordinates.

Authoring gate: the protected contract is the shared Blender geographic frame.
Open must reject another anchor before changing objects or the active IFC, and
an empty import must record an anchor that later GIS preparation uses. The
credible regression is the former Open operator, which loads engineering
coordinates beside a retained scan without comparing or saving either anchor.
Known height datums must agree before loading even when numeric origins match.
The earlier preflight accepted LN02 and LHN95 together without transforming Z.
Height controls use complete version-two deliveries from the domain builder.
Only the separate conflict cases alter their IFC datum declaration.
Imported IFC height facts must survive a compatible UNKNOWN scene contract,
so later GIS publication cannot add a contradictory known height layer.
Domain IFC tests cannot exercise Bonsai's placement or retained Blender objects.
This helper extends the installed extension owner with real operators, generated
source points and a temporary .blend reset. It adds no production test hook.
"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import shutil
import sys
import time
import uuid

import addon_utils
import bpy
import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element
import laspy
import numpy as np


def _bounds(element):
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    shape = ifcopenshell.geom.create_shape(settings, element)
    points = np.asarray(shape.geometry.verts).reshape(-1, 3)
    return np.array([points.min(0), points.max(0)])


def _mesh_bounds(obj):
    bpy.context.view_layer.update()
    points = np.array([tuple(obj.matrix_world @ vertex.co) for vertex in obj.data.vertices])
    return np.array([points.min(0), points.max(0)])


def _state(tool):
    scene = bpy.context.scene
    def value(item):
        return item.to_list() if hasattr(item, "to_list") else item
    return {"properties": {key: value(scene[key]) for key in scene.keys()},
            "objects": {obj.as_pointer(): (obj.name, np.asarray(obj.matrix_world).tolist()) for obj in scene.objects},
            "collections": {obj.as_pointer(): obj.name for obj in scene.collection.children},
            "active": bpy.context.active_object.as_pointer() if bpy.context.active_object else None,
            "ifc": tool.Ifc.get(), "ifc_path": tool.Ifc.get_path(),
            "status": scene.railway_bim.status,
            "false_origin_mode": tool.Project.get_project_props().false_origin_mode}


def _rejected(tool, message):
    before = _state(tool)
    try:
        result = bpy.ops.railway_bim.open_ifc()
    except RuntimeError as exc:
        assert message in str(exc).lower(), str(exc)
    else:
        assert result == {"CANCELLED"}, "IFC Open accepted " + message
    assert _state(tool) == before, "Rejected IFC Open changed the scene, objects or active IFC"


def exercise_import_reference(destination, output_dir, extension_package="bl_ext.user_default.railway_bim",
                              gis_package="bl_ext.user_default.swiss_gis_local"):
    import bonsai.tool as tool
    scan_package = "bl_ext.user_default.scan_viewer"
    addon_utils.enable(scan_package, default_set=False, persistent=True)
    addon_utils.enable(gis_package, default_set=False, persistent=True)
    scan = importlib.import_module(scan_package)
    gis = importlib.import_module(gis_package)
    folder = Path(output_dir) / "import-reference"
    folder.mkdir(parents=True, exist_ok=True)
    snapshot = folder / "empty-scene.blend"
    assert tool.Ifc.get() is None
    assert bpy.ops.wm.save_as_mainfile(filepath=str(snapshot)) == {"FINISHED"}
    model = ifcopenshell.open(str(destination))
    conversion = model.by_type("IfcMapConversion")[0]
    origin = np.array([conversion.Eastings, conversion.Northings, conversion.OrthogonalHeight])
    mast = next(element for element in model.by_type("IfcProduct")
                if ifcopenshell.util.element.get_psets(element).get("Pset_RailwayBIMAsset", {}).get("AssetId") == "Mast 01")
    expected_bounds = _bounds(mast)
    center = expected_bounds.mean(0)
    source = folder / "mast-centre.xyz"
    source.write_text(" ".join(str(value) for value in center + origin) + " 123 45 201\n")
    cache_paths = set()
    gis_folders = set()

    def reset():
        scan._shutdown()
        assert bpy.ops.wm.open_mainfile(filepath=str(snapshot)) == {"FINISHED"}
        assert tool.Ifc.get() is None, "The empty fixture retained an IFC project"
        bpy.context.scene.railway_bim.output_ifc = str(destination)

    def import_scan():
        assert bpy.ops.scan_viewer.import_cloud(filepath=str(source), max_points=1000) == {"FINISHED"}
        deadline = time.monotonic() + 30
        while scan._jobs or scan._pending:
            assert time.monotonic() < deadline, "The real Scan Viewer import timed out"
            scan._poll_jobs()
            time.sleep(.01)
        obj = next(obj for obj in bpy.context.scene.objects if obj.get("scan_source") == str(source))
        cache_paths.add(Path(obj["scan_cache"]))
        assert bpy.ops.scan_viewer.snap_sample(max_vertices=1000) == {"FINISHED"}
        snap = bpy.context.active_object
        assert len(snap.data.vertices) == 1
        return obj, snap

    def complete_gis():
        assert bpy.ops.swiss_gis.build_local() == {"FINISHED"}
        gis_folders.add(Path(gis._job["output"]))
        deadline = time.monotonic() + 30
        while gis._job is not None:
            assert time.monotonic() < deadline, "The project-reference GIS build did not finish"
            gis._poll_job()
            time.sleep(.01)
        return bpy.context.scene.swiss_gis.status

    try:
        reset()
        _, snap = import_scan()
        default_origin = list(bpy.context.scene["scan_scene_origin"])
        assert not np.allclose(default_origin, origin, rtol=0, atol=1e-6)
        _rejected(tool, "origin")

        for change in ("rotation", "scale", "map units", "missing project units", "context"):
            reset()
            changed = ifcopenshell.open(str(destination))
            mapping = changed.by_type("IfcMapConversion")[0]
            if change == "rotation":
                mapping.XAxisAbscissa, mapping.XAxisOrdinate = 0., 1.
            elif change == "scale":
                mapping.Scale = 2.
            elif change == "map units":
                mapping.TargetCRS.MapUnit.Prefix = "MILLI"
            elif change == "missing project units":
                assignment = changed.by_type("IfcProject")[0].UnitsInContext
                assignment.Units = [unit for unit in assignment.Units if unit.UnitType != "LENGTHUNIT"]
            else:
                mapping.SourceCRS.WorldCoordinateSystem.Location.Coordinates = (1., 0., 0.)
            altered = folder / (change.replace(" ", "-") + ".ifc")
            changed.write(str(altered))
            bpy.context.scene.railway_bim.output_ifc = str(altered)
            _rejected(tool, "engineering" if change == "context" else "units" if "units" in change else "frame")

        reset()
        bpy.context.scene["scan_scene_origin"] = origin.tolist()
        scan_obj, snap = import_scan()
        scan_point = np.array(snap.matrix_world @ snap.data.vertices[0].co)
        np.testing.assert_allclose(scan_point, center, atol=2e-5, rtol=0)
        assert bpy.ops.railway_bim.open_ifc() == {"FINISHED"}
        obj = tool.Ifc.get_object(tool.Ifc.get().by_guid(mast.GlobalId))
        np.testing.assert_allclose(_mesh_bounds(obj), expected_bounds, atol=1e-5, rtol=0)
        assert scan_obj.name in bpy.context.scene.objects and snap.name in bpy.context.scene.objects
        np.testing.assert_allclose(scan_point, _mesh_bounds(obj).mean(0), atol=2e-5, rtol=0)

        reset()
        assert bpy.ops.railway_bim.open_ifc() == {"FINISHED"}
        np.testing.assert_allclose(bpy.context.scene["scan_scene_origin"], origin, atol=1e-6, rtol=0)
        saved_contract = json.loads(bpy.context.scene["railway_project_contract_json"])
        project_id = saved_contract["project_id"]
        delivery = json.loads(destination.with_suffix(".railway-project.json").read_text())
        if "contract" in delivery:
            assert project_id == delivery["contract"]["project_id"]
        np.testing.assert_allclose(saved_contract["reference"]["local_origin_m"], origin, atol=1e-6, rtol=0)
        header = laspy.LasHeader(point_format=3, version="1.2")
        header.scales = [.001] * 3
        header.offsets = origin.tolist()
        observation = laspy.LasData(header)
        expected_scan = np.array([origin + [x * .1, y * .1, 1.] for x in range(3) for y in range(3)])
        observation.x, observation.y, observation.z = expected_scan.T
        observation.classification = np.full(9, 2, dtype=np.uint8)
        las = folder / "next-observation.laz"
        observation.write(las)
        settings = bpy.context.scene.swiss_gis
        settings.scan = str(las)
        settings.assume_lv95 = True
        settings.margin = 1
        settings.max_points = 1000
        assert bpy.ops.swiss_gis.build_local() == {"FINISHED"}
        deadline = time.monotonic() + 30
        while gis._job is not None:
            assert time.monotonic() < deadline, "GIS preparation did not complete"
            gis._poll_job()
            time.sleep(.01)
        assert settings.status.startswith("Loaded "), settings.status
        manifest = json.loads(bpy.context.scene["swiss_gis_manifest_json"])
        assert manifest["project_contract"]["project_id"] == project_id
        np.testing.assert_allclose(manifest["origin"], origin, atol=1e-6, rtol=0)
        gis_obj = next(obj for obj in bpy.context.scene.objects if obj.get("swiss_gis_source") == str(las))
        coordinates = np.array([tuple(gis_obj.matrix_world @ vertex.co) for vertex in gis_obj.data.vertices])
        np.testing.assert_allclose(coordinates + origin, expected_scan, atol=1e-6, rtol=0)
        shutil.rmtree(Path(bpy.context.scene["swiss_gis_bundle"]))

        reset()
        bpy.context.scene["scan_scene_origin"] = origin.tolist()
        settings = bpy.context.scene.swiss_gis
        settings.scan = str(las)
        settings.assume_lv95 = True
        settings.margin = 1
        settings.max_points = 1000
        status = complete_gis()
        assert status.startswith("Loaded "), status
        partial = json.loads(bpy.context.scene["railway_project_contract_json"])
        assert not partial["tracks"] and not partial["terrain_regions"] and not partial.get("sampling")
        partial["name"] = "Saved GIS project name"
        partial["options"] = {"retain_source_labels": True}
        partial["reference"]["horizontal"] = {"crs": "EPSG:2056", "status": "DECLARED",
                                                   "evidence": "Existing project declaration of EPSG:2056."}
        bpy.context.scene["railway_project_contract_json"] = json.dumps(partial)
        incoming = json.loads(json.dumps(saved_contract))
        incoming["project_id"] = partial["project_id"]
        incoming["name"] = "Incoming fitted IFC name"
        complete_ifc = folder / "gis-first.ifc"
        reconstruction = Path(delivery["reconstruction_dir"])
        if not reconstruction.is_absolute():
            reconstruction = destination.parent / reconstruction
        domain = importlib.import_module(extension_package + ".core")
        domain.build_ifc(reconstruction, complete_ifc, project_contract=incoming, overwrite=True)
        bpy.context.scene.railway_bim.output_ifc = str(complete_ifc)
        assert bpy.ops.railway_bim.open_ifc() == {"FINISHED"}
        merged = json.loads(bpy.context.scene["railway_project_contract_json"])
        for key in ("tracks", "terrain_regions", "sampling"):
            assert merged[key] == incoming[key], "GIS-first IFC Open discarded incoming project " + key
        assert merged["name"] == partial["name"] and merged["options"] == partial["options"]
        for key in ("crs", "status", "evidence"):
            assert merged["reference"]["horizontal"][key] == partial["reference"]["horizontal"][key]
        shutil.rmtree(Path(bpy.context.scene["swiss_gis_bundle"]))

        for conflict in ("tracks", "terrain_regions", "sampling"):
            reset()
            conflicting = json.loads(json.dumps(incoming))
            if conflict == "tracks":
                conflicting["tracks"][0]["rail_ids"] = ["Rail 01", "Rail 03"]
                conflicting["tracks"][1]["rail_ids"] = ["Rail 02", "Rail 04"]
            elif conflict == "terrain_regions":
                conflicting["terrain_regions"][0]["selector"]["margin_m"] += .1
            else:
                conflicting["sampling"]["alignment_sample_count"] += 1
            bpy.context.scene["railway_project_contract_json"] = json.dumps(conflicting)
            bpy.context.scene["scan_scene_origin"] = origin.tolist()
            bpy.context.scene.railway_bim.output_ifc = str(complete_ifc)
            _rejected(tool, conflict)

        reset()
        bare_v2 = folder / "bare-v2.ifc"
        shutil.copy2(complete_ifc, bare_v2)
        bpy.context.scene.railway_bim.output_ifc = str(bare_v2)
        assert bpy.ops.railway_bim.open_ifc() == {"FINISHED"}
        assert "railway_project_contract_json" not in bpy.context.scene, "Bare IFC Open invented a full project contract"
        assert bpy.context.scene.get("railway_ifc_project_id") == incoming["project_id"], "Bare IFC Open discarded its actual project UUID"
        assert bpy.context.scene.get("railway_ifc_horizontal_crs") == "EPSG:2056", "Bare IFC Open discarded its horizontal name"
        settings = bpy.context.scene.swiss_gis
        settings.scan = str(las)
        settings.assume_lv95 = True
        settings.margin = 1
        settings.max_points = 1000
        status = complete_gis()
        assert status.startswith("Loaded "), status
        assert json.loads(bpy.context.scene["railway_project_contract_json"])["project_id"] == incoming["project_id"], "GIS invented another project UUID beside bare IFC"
        assert bpy.context.scene["railway_ifc_project_id"] == incoming["project_id"]
        shutil.rmtree(Path(bpy.context.scene["swiss_gis_bundle"]))

        reset()
        bpy.context.scene.railway_bim.output_ifc = str(bare_v2)
        assert bpy.ops.railway_bim.open_ifc() == {"FINISHED"}
        assert bpy.ops.bim.convert_to_blender() == {"FINISHED"}
        assert tool.Ifc.get() is None and "railway_project_contract_json" not in bpy.context.scene
        other_project = json.loads(json.dumps(incoming))
        other_project["project_id"] = str(uuid.uuid4())
        other_ifc = folder / "another-project.ifc"
        domain.build_ifc(reconstruction, other_ifc, project_contract=other_project, overwrite=True)
        bpy.context.scene.railway_bim.output_ifc = str(other_ifc)
        _rejected(tool, "project")

        reset()
        web_contract = json.loads(json.dumps(incoming))
        web_contract["reference"]["horizontal"] = {
            "crs": "EPSG:3857", "status": "DECLARED",
            "evidence": "Declared horizontal name for the bare IFC compatibility fixture."}
        web_full = folder / "full-3857.ifc"
        domain.build_ifc(reconstruction, web_full, project_contract=web_contract, overwrite=True)
        web_bare = folder / "bare-3857.ifc"
        shutil.copy2(web_full, web_bare)
        bpy.context.scene.railway_bim.output_ifc = str(web_bare)
        assert bpy.ops.railway_bim.open_ifc() == {"FINISHED"}
        assert "railway_project_contract_json" not in bpy.context.scene
        assert bpy.context.scene.get("railway_ifc_horizontal_crs") == "EPSG:3857", "Bare IFC Open discarded its checked horizontal name"
        settings = bpy.context.scene.swiss_gis
        settings.scan = str(las)
        settings.assume_lv95 = True
        settings.margin = 1
        settings.max_points = 1000
        before = _state(tool)
        assert "horizontal" in complete_gis().lower(), "GIS accepted EPSG:2056 beside retained EPSG:3857 IFC"
        assert _state(tool) == before, "Rejected GIS publication changed the retained IFC reference or scene"
        assert bpy.ops.bim.convert_to_blender() == {"FINISHED"}
        bpy.context.scene.railway_bim.output_ifc = str(complete_ifc)
        _rejected(tool, "horizontal")
        unknown_contract = json.loads(json.dumps(incoming))
        unknown_contract["reference"]["horizontal"] = {"crs": "UNKNOWN", "status": "UNKNOWN"}
        unknown_full = folder / "full-unknown-crs.ifc"
        domain.build_ifc(reconstruction, unknown_full, project_contract=unknown_contract, overwrite=True)
        unknown_bare = folder / "bare-unknown-crs.ifc"
        shutil.copy2(unknown_full, unknown_bare)
        bpy.context.scene.railway_bim.output_ifc = str(unknown_bare)
        assert bpy.ops.railway_bim.open_ifc() == {"FINISHED"}
        assert bpy.context.scene["railway_ifc_horizontal_crs"] == "EPSG:3857", "UNKNOWN import erased a retained known horizontal name"

        datum_cases = (
            ("scene-conflict", "LHN95", "LHN95", "LN02", "LN02", False),
            ("ifc-sidecar-conflict", "LN02", "LHN95", "UNKNOWN", "UNKNOWN", False),
            ("bare-scene-conflict", "LHN95", None, None, "LN02", False),
            ("saved-scene-conflict", None, "UNKNOWN", "LN02", "LHN95", False),
            ("matching-known", "LN02", "LN02", "LN02", "LN02", True),
            ("unknown-ifc", None, "UNKNOWN", "LN02", "LN02", True),
            ("unknown-scene", "LHN95", "LHN95", "UNKNOWN", "UNKNOWN", True),
            ("bare-unknown-scene", "LHN95", None, "UNKNOWN", "UNKNOWN", True),
        )
        for name, ifc_datum, sidecar_datum, scene_datum, gis_datum, accepted in datum_cases:
            reset()
            altered = folder / (name + ".ifc")
            incoming_contract = json.loads(json.dumps(saved_contract))
            datum = sidecar_datum or ifc_datum or "UNKNOWN"
            incoming_contract["reference"]["vertical"] = {
                "datum": datum, "status": "UNKNOWN" if datum == "UNKNOWN" else "DECLARED",
                "evidence": "Height datum declared by the import regression fixture."}
            reconstruction = Path(delivery["reconstruction_dir"])
            if not reconstruction.is_absolute():
                reconstruction = destination.parent / reconstruction
            domain = importlib.import_module(extension_package + ".core")
            domain.build_ifc(reconstruction, altered, project_contract=incoming_contract, overwrite=True)
            changed = ifcopenshell.open(str(altered))
            datum_mast = next(element for element in changed.by_type("IfcProduct")
                              if ifcopenshell.util.element.get_psets(element).get("Pset_RailwayBIMAsset", {}).get("AssetId") == "Mast 01")
            datum_guid = datum_mast.GlobalId
            if changed.by_type("IfcMapConversion")[0].TargetCRS.VerticalDatum != ifc_datum:
                changed.by_type("IfcMapConversion")[0].TargetCRS.VerticalDatum = ifc_datum
                changed.write(str(altered))
            if sidecar_datum is None:
                altered.with_suffix(".railway-project.json").unlink()
            if scene_datum is not None:
                existing = json.loads(json.dumps(saved_contract))
                existing["reference"]["vertical"] = {
                    "datum": scene_datum, "status": "UNKNOWN" if scene_datum == "UNKNOWN" else "DECLARED",
                    "evidence": "Height datum declared by the scene regression fixture."}
                bpy.context.scene["railway_project_contract_json"] = json.dumps(existing)
            bpy.context.scene["scan_scene_origin"] = origin.tolist()
            bpy.context.scene["swiss_gis_vertical_datum"] = gis_datum
            bpy.context.scene.railway_bim.output_ifc = str(altered)
            if accepted:
                saved_scene_contract = bpy.context.scene.get("railway_project_contract_json")
                assert bpy.ops.railway_bim.open_ifc() == {"FINISHED"}, "Compatible height references failed: " + name
                obj = tool.Ifc.get_object(tool.Ifc.get().by_guid(datum_guid))
                np.testing.assert_allclose(_mesh_bounds(obj), expected_bounds, atol=1e-5, rtol=0)
                if name in {"unknown-scene", "bare-unknown-scene"}:
                    assert bpy.context.scene.get("railway_ifc_vertical_datum") == "LHN95", "Open discarded the known imported IFC height datum"
                    before_contract = json.loads(saved_scene_contract)
                    after_contract = json.loads(bpy.context.scene["railway_project_contract_json"])
                    for key in ("project_id", "name", "tracks", "terrain_regions", "sampling"):
                        assert after_contract.get(key) == before_contract.get(key), "Open replaced saved project " + key
                if name == "unknown-scene":
                    settings = bpy.context.scene.swiss_gis
                    settings.scan = str(las)
                    settings.assume_lv95 = True
                    settings.vertical_datum = "LN02"
                    settings.margin = 1
                    settings.max_points = 1000
                    before = _state(tool)
                    assert bpy.ops.swiss_gis.build_local() == {"FINISHED"}
                    job_folder = Path(gis._job["output"])
                    deadline = time.monotonic() + 30
                    while gis._job is not None:
                        assert time.monotonic() < deadline, "The conflicting GIS build did not finish"
                        gis._poll_job()
                        time.sleep(.01)
                    assert "height datum" in settings.status.lower(), "GIS accepted LN02 beside retained LHN95 IFC"
                    assert _state(tool) == before, "Rejected GIS publication changed the scene or active IFC"
                    shutil.rmtree(job_folder, ignore_errors=True)
                    settings.vertical_datum = "LHN95"
                    assert bpy.ops.swiss_gis.build_local() == {"FINISHED"}
                    deadline = time.monotonic() + 30
                    while gis._job is not None:
                        assert time.monotonic() < deadline, "The compatible GIS build did not finish"
                        gis._poll_job()
                        time.sleep(.01)
                    assert settings.status.startswith("Loaded "), settings.status
                    assert bpy.context.scene["railway_ifc_vertical_datum"] == "LHN95"
                    assert tool.Ifc.get() is before["ifc"]
                    np.testing.assert_allclose(_mesh_bounds(obj), expected_bounds, atol=1e-5, rtol=0)
                    shutil.rmtree(Path(bpy.context.scene["swiss_gis_bundle"]), ignore_errors=True)
            else:
                _rejected(tool, "height")

        import rasterio
        from rasterio.transform import from_origin

        terrain_folder = folder / "ln02-terrain"
        terrain_folder.mkdir(exist_ok=True)
        with rasterio.open(terrain_folder / "known-height.tif", "w", driver="GTiff", width=4, height=4,
                           count=1, dtype="float32", crs="EPSG:2056",
                           transform=from_origin(origin[0] - 2, origin[1] + 2, 1, 1)) as raster:
            raster.write(np.full((1, 4, 4), origin[2] + 1, dtype=np.float32))
            raster.update_tags(VERTICAL_DATUM="LN02")
        for terrain_storage in ("tagged", "legacy"):
            reset()
            bpy.context.scene["railway_project_contract_json"] = json.dumps(saved_contract)
            bpy.context.scene["scan_scene_origin"] = origin.tolist()
            settings = bpy.context.scene.swiss_gis
            settings.scan = str(las)
            settings.assume_lv95 = True
            settings.terrain_folder = str(terrain_folder)
            settings.terrain_step = 1
            settings.vertical_datum = "UNKNOWN"
            settings.margin = 1
            settings.max_points = 1000
            assert bpy.ops.swiss_gis.build_local() == {"FINISHED"}
            deadline = time.monotonic() + 30
            while gis._job is not None:
                assert time.monotonic() < deadline, "The retained terrain fixture did not finish"
                gis._poll_job()
                time.sleep(.01)
            assert settings.status.startswith("Loaded "), settings.status
            terrain = next(obj for obj in bpy.context.scene.objects if obj.get("swiss_gis_role") == "terrain")
            assert terrain["swiss_gis_terrain_vertical_datum"] == "LN02"
            assert bpy.context.scene["swiss_gis_vertical_datum"] == "UNKNOWN"
            if terrain_storage == "legacy":
                del terrain["swiss_gis_terrain_vertical_datum"]
            bpy.context.scene.railway_bim.output_ifc = str(altered)
            _rejected(tool, "height")
            shutil.rmtree(Path(bpy.context.scene["swiss_gis_bundle"]), ignore_errors=True)

        reset()
        bare = folder / "bare.ifc"
        bare_model = ifcopenshell.open(str(destination))
        bare_model.by_type("IfcMapConversion")[0].TargetCRS.MapUnit = None
        assignment = bare_model.by_type("IfcProject")[0].UnitsInContext
        metre = next(unit for unit in assignment.Units if unit.UnitType == "LENGTHUNIT")
        factor = bare_model.create_entity("IfcMeasureWithUnit", ValueComponent=bare_model.create_entity("IfcLengthMeasure", 1.),
                                          UnitComponent=metre)
        converted = bare_model.create_entity("IfcConversionBasedUnit",
                                             Dimensions=bare_model.create_entity("IfcDimensionalExponents", 1, 0, 0, 0, 0, 0, 0),
                                             UnitType="LENGTHUNIT", Name="metre", ConversionFactor=factor)
        assignment.Units = [converted if unit == metre else unit for unit in assignment.Units]
        bare_model.write(str(bare))
        bpy.context.scene.railway_bim.output_ifc = str(bare)
        assert bpy.ops.railway_bim.open_ifc() == {"FINISHED"}
        np.testing.assert_allclose(bpy.context.scene["scan_scene_origin"], origin, atol=1e-6, rtol=0)
        assert "railway_project_contract_json" not in bpy.context.scene, "A bare IFC invented a project contract"
        return {"mismatched_scan_origin_rejected_without_mutation": True,
                "unsupported_map_frames_rejected_without_mutation": True,
                "matching_scan_and_ifc_meshes_share_coordinates": True,
                "empty_import_records_delivery_reference": True,
                "subsequent_gis_preparation_reuses_origin_and_project_uuid": True,
                "bare_ifc_records_origin_without_inventing_contract": True,
                "conversion_based_metre_project_units_with_inherited_map_units": True,
                "conflicting_known_height_datums_rejected_without_mutation": True,
                "matching_known_and_unknown_height_references_import": True,
                "ifc_height_retained_without_replacing_existing_contract": True,
                "later_conflicting_gis_publication_rejected_without_mutation": True,
                "matching_gis_publication_preserves_active_ifc": True,
                "tagged_and_legacy_retained_terrain_datums_reject_conflicting_ifc": True,
                "gis_first_ifc_fills_missing_contract_fields_and_preserves_saved_evidence": True,
                "conflicting_nonempty_project_fields_rejected_before_load": True,
                "bare_ifc_project_uuid_reused_by_gis_without_inventing_full_contract": True,
                "retained_ifc_project_uuid_rejects_another_project_after_conversion": True,
                "bare_ifc_horizontal_name_blocks_conflicting_gis_and_ifc_imports": True,
                "unknown_horizontal_name_does_not_erase_retained_known_ifc_reference": True,
                "default_scan_origin": default_origin, "ifc_origin": origin.tolist()}
    finally:
        reset()
        for cache in cache_paths:
            shutil.rmtree(cache, ignore_errors=True)
        for project_folder in gis_folders:
            shutil.rmtree(project_folder, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ifc", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--extension-package", default="bl_ext.user_default.railway_bim")
    parser.add_argument("--extension-root", type=Path)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    if args.extension_root:
        sys.path.insert(0, str(args.extension_root.resolve()))
    addon_utils.enable("bl_ext.user_default.bonsai", default_set=False, persistent=True)
    if args.extension_package != "bl_ext.user_default.railway_bim":
        addon_utils.disable("bl_ext.user_default.railway_bim", default_set=False)
    addon_utils.enable(args.extension_package, default_set=False, persistent=True)
    report = exercise_import_reference(args.ifc.resolve(), args.output_dir.resolve(), args.extension_package)
    (args.output_dir / "import-reference-check.json").write_text(json.dumps(report, indent=2) + "\n")
    print("RAILWAY_IMPORT_REFERENCE_OK", json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
