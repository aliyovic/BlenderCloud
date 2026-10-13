"""Protect persistent project identity through the real Build and Open operators.

Two observations in one scene must keep its project UUID, while a new scene
gets its own UUID. Invalid saved identity must reject preparation before any
scene or bundle changes. Pure bundle and IFC tests do not reach this operator.
Opening a prepared bundle from another project must reject before scene changes;
another observation prepared with the same UUID must still load.
An IFC project must also retain its topology, sampling and recorded reference
facts when GIS supplies another observation. The previous UUID checks missed
that loss. The optional pilot scenario imports the real delivery in Bonsai.
Retained GeoTIFF terrain must reject later conflicting known height datums,
even when both surveys remain UNKNOWN and the active manifest has no terrain.
The prior datum control compared only incoming terrain with the project.
Prepared Open must compare an independently checked IFC UUID before the first
GIS contract exists. A missing sidecar must not turn that UUID into a new project.
The independently checked IFC horizontal CRS must also agree before publication;
the previous bare-IFC controls covered UUIDs and heights but missed that name.
The optional source path selects an add-on registration for repair controls;
it does not change preparation behavior or add a production hook.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import uuid

import bpy
import laspy
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from fixtures import fixture_scan, fixture_raster


def _build_scan(addon, scan, bundles):
    settings = bpy.context.scene.swiss_gis
    settings.scan = str(scan)
    settings.margin = 1
    settings.max_points = 1000
    assert bpy.ops.swiss_gis.build_local() == {"FINISHED"}
    deadline = time.monotonic() + 30
    while addon._job is not None and time.monotonic() < deadline:
        addon._poll_job()
        time.sleep(.01)
    assert addon._job is None, "The identity fixture build did not complete"
    if bpy.app.timers.is_registered(addon._poll_job):
        bpy.app.timers.unregister(addon._poll_job)
    assert settings.status.startswith("Loaded "), settings.status
    manifest = json.loads(bpy.context.scene["swiss_gis_manifest_json"])
    bundles.append(Path(bpy.context.scene["swiss_gis_bundle"]))
    return manifest


def _reject_bundle(folder, message):
    scene = bpy.context.scene
    before = {key: scene.get(key) for key in ("railway_project_contract_json", "swiss_gis_manifest_json", "swiss_gis_bundle",
                                            "swiss_gis_vertical_datum", "swiss_gis_origin_json", "railway_ifc_vertical_datum",
                                            "railway_ifc_project_id", "railway_ifc_horizontal_crs")}
    objects = {obj.as_pointer(): (obj.name, obj.get("swiss_gis_terrain_vertical_datum")) for obj in scene.objects}
    collections = {item.as_pointer(): item.name for item in scene.collection.children}
    input_bytes = (folder / "project.json").read_bytes()
    try:
        result = bpy.ops.swiss_gis.open_bundle(filepath=str(folder / "project.json"))
    except RuntimeError as exc:
        assert message in str(exc).lower(), str(exc)
    else:
        assert result == {"CANCELLED"}, f"Open accepted conflicting project {message}"
    assert {key: scene.get(key) for key in before} == before
    assert {obj.as_pointer(): (obj.name, obj.get("swiss_gis_terrain_vertical_datum")) for obj in scene.objects} == objects
    assert {item.as_pointer(): item.name for item in scene.collection.children} == collections
    assert (folder / "project.json").read_bytes() == input_bytes


def exercise_project_identity(addon, directory):
    directory = Path(directory).resolve()
    original_scene = bpy.context.window.scene
    scenes, bundles = [], []
    started = time.monotonic()

    def new_scene(name):
        scene = bpy.data.scenes.new(name)
        scenes.append(scene)
        bpy.context.window.scene = scene
        return scene

    def build_scan(scan):
        return _build_scan(addon, scan, bundles)

    try:
        first_source = directory / "first.laz"
        second_source = directory / "resurvey.laz"
        first_expected = fixture_scan(first_source)
        expected = fixture_scan(second_source)
        changed = laspy.read(second_source)
        changed.z = np.asarray(changed.z) + .001
        changed.write(second_source)
        expected[:, 2] += .001

        scene = new_scene("Persistent Swiss GIS identity")
        first = build_scan(first_source)
        first_id = first["project_contract"]["project_id"]
        uuid.UUID(first_id)
        second = build_scan(second_source)
        assert second["source"]["path"] != first["source"]["path"]
        assert second["project_contract"]["project_id"] == first_id, "A new observation replaced the scene's persistent project UUID"
        assert json.loads(scene["railway_project_contract_json"])["project_id"] == first_id
        assert second["origin"] == first["origin"]
        scan = next(obj for obj in scene.objects if obj.get("swiss_gis_source") == str(second_source))
        coordinates = np.array([tuple(vertex.co) for vertex in scan.data.vertices])
        np.testing.assert_allclose(coordinates + np.array(second["origin"]), expected, atol=2e-6, rtol=0)

        conflict = directory / "another-project"
        incoming = addon.prepare(addon.Project(scan=str(second_source), origin=tuple(second["origin"]),
                                               margin=1, max_points=1000), conflict)
        assert incoming["origin"] == second["origin"]
        assert incoming["project_contract"]["project_id"] != first_id
        saved_contract = scene["railway_project_contract_json"]
        saved_manifest = scene["swiss_gis_manifest_json"]
        saved_bundle = scene["swiss_gis_bundle"]
        saved_objects = {obj.as_pointer(): obj.name for obj in scene.objects}
        saved_collections = {collection.as_pointer(): collection.name for collection in scene.collection.children}
        saved_input = (conflict / "project.json").read_bytes()
        try:
            result = bpy.ops.swiss_gis.open_bundle(filepath=str(conflict / "project.json"))
        except RuntimeError as exc:
            assert "project" in str(exc).lower(), str(exc)
        else:
            assert result == {"CANCELLED"}, "Open accepted another project's UUID at the same scene origin"
        assert scene["railway_project_contract_json"] == saved_contract
        assert scene["swiss_gis_manifest_json"] == saved_manifest
        assert scene["swiss_gis_bundle"] == saved_bundle
        assert {obj.as_pointer(): obj.name for obj in scene.objects} == saved_objects
        assert {collection.as_pointer(): collection.name for collection in scene.collection.children} == saved_collections
        assert (conflict / "project.json").read_bytes() == saved_input

        matching = directory / "same-project-observation"
        addon.prepare(addon.Project(scan=str(first_source), origin=tuple(second["origin"]),
                                    margin=1, max_points=1000, project_id=first_id), matching)
        assert bpy.ops.swiss_gis.open_bundle(filepath=str(matching / "project.json")) == {"FINISHED"}
        assert json.loads(scene["railway_project_contract_json"])["project_id"] == first_id
        assert Path(scene["swiss_gis_bundle"]) == matching
        collection = next(collection for collection in scene.collection.children
                          if collection.get("swiss_gis_project") == str(matching))
        loaded = next(obj for obj in collection.objects if obj.get("swiss_gis_role") == "scan")
        coordinates = np.array([tuple(vertex.co) for vertex in loaded.data.vertices])
        np.testing.assert_allclose(coordinates + np.array(second["origin"]), first_expected, atol=2e-6, rtol=0)

        checked = new_scene("Checked IFC identity without a full contract")
        checked["scan_scene_origin"] = second["origin"]
        checked["railway_ifc_project_id"] = "{" + first_id.upper() + "}"
        _reject_bundle(conflict, "identity")
        assert "railway_project_contract_json" not in checked
        for malformed_id in ("not-a-uuid", "", "UNKNOWN"):
            checked["railway_ifc_project_id"] = malformed_id
            _reject_bundle(matching, "identity")
        checked["railway_ifc_project_id"] = first_id
        assert bpy.ops.swiss_gis.open_bundle(filepath=str(matching / "project.json")) == {"FINISHED"}
        assert json.loads(checked["railway_project_contract_json"])["project_id"] == first_id
        checked_scan = next(obj for obj in checked.objects if obj.get("swiss_gis_role") == "scan")
        checked_coordinates = np.array([tuple(vertex.co) for vertex in checked_scan.data.vertices])
        np.testing.assert_allclose(checked_coordinates + np.array(second["origin"]), first_expected, atol=2e-6, rtol=0)
        checked["railway_ifc_project_id"] = incoming["project_contract"]["project_id"]
        _reject_bundle(matching, "identity")
        checked["railway_ifc_project_id"] = first_id
        bpy.context.window.scene = scene

        for crs, accepted in (("EPSG:3857", False), ("UNKNOWN", True), ("EPSG:2056", True)):
            framed = new_scene("Checked IFC horizontal reference " + crs)
            framed["scan_scene_origin"] = second["origin"]
            framed["railway_ifc_project_id"] = first_id
            framed["railway_ifc_horizontal_crs"] = crs
            if not accepted:
                _reject_bundle(matching, "crs")
            else:
                assert bpy.ops.swiss_gis.open_bundle(filepath=str(matching / "project.json")) == {"FINISHED"}
                framed_scan = next(obj for obj in framed.objects if obj.get("swiss_gis_role") == "scan")
                framed_coordinates = np.array([tuple(vertex.co) for vertex in framed_scan.data.vertices])
                np.testing.assert_allclose(framed_coordinates + np.array(second["origin"]), first_expected, atol=2e-6, rtol=0)
                assert framed["railway_ifc_horizontal_crs"] == crs
                assert json.loads(framed["railway_project_contract_json"])["project_id"] == first_id
        bpy.context.window.scene = scene

        saved_contract = scene["railway_project_contract_json"]
        saved_manifest = scene["swiss_gis_manifest_json"]
        saved_bundle = scene["swiss_gis_bundle"]
        saved_objects = {obj.name for obj in scene.objects}
        malformed = json.loads(saved_contract)
        malformed["project_id"] = "not-a-uuid"
        for raw in (json.dumps(malformed), json.dumps([]), "{"):
            scene["railway_project_contract_json"] = raw
            try:
                result = bpy.ops.swiss_gis.build_local()
            except RuntimeError as exc:
                assert "project" in str(exc).lower(), str(exc)
            else:
                assert result == {"CANCELLED"}, "The Build operator accepted malformed saved project identity"
            assert addon._job is None, "Invalid identity started a preparation worker"
            assert scene["swiss_gis_manifest_json"] == saved_manifest
            assert scene["swiss_gis_bundle"] == saved_bundle
            assert {obj.name for obj in scene.objects} == saved_objects
        scene["railway_project_contract_json"] = saved_contract

        for legacy in (False, True):
            retained = new_scene("Retained terrain reference" + (" from an older workspace" if legacy else ""))
            terrain_folder = directory / ("retained-terrain-legacy" if legacy else "retained-terrain")
            terrain_folder.mkdir()
            fixture_raster(terrain_folder / "ground.tif", np.full((8, 8), 450, dtype=np.float32), datum="LN02")
            first_terrain = terrain_folder / "first-bundle"
            addon.prepare(addon.Project(scan=str(first_source), terrain_folder=str(terrain_folder),
                                        origin=tuple(second["origin"]), project_id=first_id,
                                        margin=1, max_points=1000), first_terrain)
            assert bpy.ops.swiss_gis.open_bundle(filepath=str(first_terrain / "project.json")) == {"FINISHED"}
            terrain_obj = next(obj for obj in retained.objects if obj.get("swiss_gis_role") == "terrain")
            if legacy and "swiss_gis_terrain_vertical_datum" in terrain_obj:
                del terrain_obj["swiss_gis_terrain_vertical_datum"]
            held = terrain_folder.with_name(terrain_folder.name + ".held")
            terrain_folder.rename(held)
            try:
                assert bpy.ops.swiss_gis.open_bundle(filepath=str(matching / "project.json")) == {"FINISHED"}
                assert not json.loads(retained["swiss_gis_manifest_json"])["terrain_sources"]
                assert retained["swiss_gis_vertical_datum"] == "UNKNOWN"
                assert json.loads(retained["railway_project_contract_json"])["reference"]["vertical"]["datum"] == "UNKNOWN"
                assert terrain_obj in list(retained.objects)
                other_terrain = terrain_folder.with_name(terrain_folder.name + "-lhn95")
                other_terrain.mkdir()
                fixture_raster(other_terrain / "ground.tif", np.full((8, 8), 450, dtype=np.float32), datum="LHN95")
                conflicting = other_terrain / "conflicting-bundle"
                addon.prepare(addon.Project(scan=str(first_source), terrain_folder=str(other_terrain),
                                            origin=tuple(second["origin"]), project_id=first_id,
                                            margin=1, max_points=1000), conflicting)
                _reject_bundle(conflicting, "datum")
                assert bpy.ops.swiss_gis.open_bundle(filepath=str(held / "first-bundle/project.json")) == {"FINISHED"}
                assert retained["swiss_gis_vertical_datum"] == "UNKNOWN"
                loaded = [obj for obj in retained.objects if obj.get("swiss_gis_role") == "terrain"]
                assert len(loaded) == 2, "A matching known terrain reference did not load beside the retained layer"
                for obj in loaded:
                    heights = np.array([vertex.co.z for vertex in obj.data.vertices]) + second["origin"][2]
                    np.testing.assert_allclose(heights, 450, atol=2e-6, rtol=0)
            finally:
                held.rename(terrain_folder)

        fresh = new_scene("Independent Swiss GIS identity")
        independent = build_scan(second_source)
        independent_id = independent["project_contract"]["project_id"]
        uuid.UUID(independent_id)
        assert independent_id != first_id, "A new scene reused another project's UUID"
        assert json.loads(fresh["railway_project_contract_json"])["project_id"] == independent_id
        return {"observation_rebuild_keeps_uuid": True, "new_scene_gets_fresh_uuid": True,
                "other_project_bundle_rejected_without_mutation": True,
                "same_project_observation_bundle_loads": True,
                "checked_ifc_identity_controls_prepared_open": True,
                "invalid_checked_identity_rejected_without_mutation": True,
                "checked_identity_and_contract_conflict_rejected": True,
                "checked_ifc_crs_controls_prepared_open": True,
                "matching_and_unknown_ifc_crs_preserve_coordinates": True,
                "malformed_identity_rejected_before_dispatch": True,
                "retained_terrain_datum_conflict_rejected_without_mutation": True,
                "legacy_terrain_reference_retained_after_manifest_update": True,
                "matching_terrain_datum_loads_without_promoting_survey": True,
                "resurvey_coordinates_preserved": True,
                "elapsed_seconds": round(time.monotonic() - started, 3)}
    finally:
        bpy.context.window.scene = original_scene
        for scene in scenes:
            bpy.data.scenes.remove(scene)
        for bundle in bundles:
            shutil.rmtree(bundle)


def exercise_project_contract(addon, directory, pilot_ifc):
    """Retain an imported IFC contract through GIS Build and prepared Open."""
    import bonsai.tool as tool
    from bonsai.bim.ifc import IfcStore

    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    original_scene = bpy.context.window.scene
    scene = bpy.data.scenes.new("IFC project with another GIS observation")
    bpy.context.window.scene = scene
    bundles = []
    started = time.monotonic()
    assert tool.Ifc.get() is None, "The project contract fixture requires an empty IFC session"

    def contract():
        return json.loads(scene["railway_project_contract_json"])

    def assert_structure(expected):
        actual = contract()
        for key in ("project_id", "name", "tracks", "terrain_regions", "sampling"):
            assert actual.get(key) == expected[key], f"GIS replaced the saved project {key}"

    def assert_loaded(folder, scan, expected):
        manifest = json.loads(scene["swiss_gis_manifest_json"])
        assert Path(scene["swiss_gis_bundle"]) == folder
        assert manifest["source"]["path"] == str(scan)
        collection = next(item for item in scene.collection.children
                          if item.get("swiss_gis_project") == str(folder))
        obj = next(item for item in collection.objects if item.get("swiss_gis_role") == "scan")
        coordinates = np.array([tuple(vertex.co) for vertex in obj.data.vertices])
        np.testing.assert_allclose(coordinates + np.array(manifest["origin"]), expected, atol=2e-6, rtol=0)

    try:
        scene.railway_bim.output_ifc = str(Path(pilot_ifc).resolve())
        assert bpy.ops.railway_bim.open_ifc() == {"FINISHED"}
        imported = contract()
        assert imported["tracks"] and imported["terrain_regions"] and imported["sampling"]
        first_source = directory / "unknown-reference-observation.laz"
        first_expected = fixture_scan(first_source)
        known_height = json.loads(json.dumps(imported))
        known_height["reference"]["vertical"] = {"datum": "LN02", "status": "DECLARED",
                                                 "evidence": "Height datum declared for the GeoTIFF regression fixture."}
        scene["railway_project_contract_json"] = json.dumps(known_height)
        terrain = directory / "terrain-other-datum"
        terrain.mkdir()
        fixture_raster(terrain / "ground.tif", np.full((8, 8), 450, dtype=np.float32), datum="LHN95")
        terrain_bundle = directory / "terrain-observation"
        addon.prepare(addon.Project(scan=str(first_source), terrain_folder=str(terrain),
                                    origin=tuple(imported["reference"]["local_origin_m"]),
                                    project_id=imported["project_id"], margin=1, max_points=1000), terrain_bundle)
        _reject_bundle(terrain_bundle, "datum")
        scene["railway_project_contract_json"] = json.dumps(imported)
        first = _build_scan(addon, first_source, bundles)
        assert_structure(imported)
        assert contract()["reference"] == imported["reference"], "Unknown/default survey facts erased the imported reference evidence"
        assert first["source"]["path"] == str(first_source)
        assert_loaded(Path(scene["swiss_gis_bundle"]), first_source, first_expected)

        verified_source = directory / "verified-reference-observation.laz"
        verified_expected = fixture_scan(verified_source, crs="EPSG:2056")
        folder = directory / "verified-observation"
        addon.prepare(addon.Project(scan=str(verified_source), origin=tuple(first["origin"]),
                                    project_id=imported["project_id"], margin=1, max_points=1000,
                                    vertical_datum="LN02"), folder)
        assert bpy.ops.swiss_gis.open_bundle(filepath=str(folder / "project.json")) == {"FINISHED"}
        assert_structure(imported)
        updated = contract()
        assert updated["reference"]["horizontal"]["status"] == "VERIFIED"
        assert imported["reference"]["horizontal"]["evidence"] in updated["reference"]["horizontal"]["evidence"]
        for key in ("description", "geodetic_datum", "map_projection"):
            assert updated["reference"]["horizontal"][key] == imported["reference"]["horizontal"][key]
        assert updated["reference"]["vertical"]["datum"] == "LN02"
        assert updated["reference"]["vertical"]["status"] == "DECLARED"
        assert_loaded(folder, verified_source, verified_expected)

        unknown = directory / "unknown-observation"
        addon.prepare(addon.Project(scan=str(first_source), origin=tuple(first["origin"]),
                                    project_id=imported["project_id"], margin=1, max_points=1000), unknown)
        assert bpy.ops.swiss_gis.open_bundle(filepath=str(unknown / "project.json")) == {"FINISHED"}
        assert contract() == updated, "A later unknown observation downgraded the saved project facts"
        assert_loaded(unknown, first_source, first_expected)

        conflict = directory / "other-height-datum"
        addon.prepare(addon.Project(scan=str(verified_source), origin=tuple(first["origin"]),
                                    project_id=imported["project_id"], margin=1, max_points=1000,
                                    vertical_datum="LHN95"), conflict)
        _reject_bundle(conflict, "datum")
        for key in ("tracks", "terrain_regions", "sampling"):
            manifest = json.loads((folder / "project.json").read_text())
            manifest["project_contract"][key] = json.loads(json.dumps(imported[key]))
            if key == "sampling":
                manifest["project_contract"][key]["rail_sample_count"] += 1
            else:
                manifest["project_contract"][key][0]["id"] += "-conflicting"
            altered = directory / ("conflicting-" + key)
            shutil.copytree(folder, altered)
            (altered / "project.json").write_text(json.dumps(manifest))
            _reject_bundle(altered, key)

        saved = scene["railway_project_contract_json"]
        other_crs = contract()
        other_crs["reference"]["horizontal"]["crs"] = "EPSG:21781"
        scene["railway_project_contract_json"] = json.dumps(other_crs)
        _reject_bundle(folder, "crs")
        scene["railway_project_contract_json"] = saved
        return {"ifc_project_structure_retained": True, "recorded_reference_facts_retained": True,
                "compatible_reference_facts_upgraded": True, "unknown_facts_do_not_downgrade": True,
                "known_terrain_datum_conflict_rejected_without_mutation": True,
                "conflicting_topology_and_reference_rejected_without_mutation": True,
                "active_observation_coordinates_updated": True,
                "elapsed_seconds": round(time.monotonic() - started, 3)}
    finally:
        IfcStore.purge()
        bpy.context.window.scene = original_scene
        bpy.data.scenes.remove(scene)
        for bundle in bundles:
            shutil.rmtree(bundle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--addon-source", type=Path)
    parser.add_argument("--pilot-ifc", type=Path)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    installed = importlib.import_module("bl_ext.user_default.swiss_gis_local")
    addon = installed
    if args.addon_source:
        installed.unregister()
        path = args.addon_source.resolve() / "__init__.py"
        spec = importlib.util.spec_from_file_location("swiss_gis_identity_candidate", path,
                                                     submodule_search_locations=[str(path.parent)])
        addon = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = addon
        spec.loader.exec_module(addon)
        addon.register()
    try:
        with tempfile.TemporaryDirectory(prefix="swiss-gis-identity-") as directory:
            report = exercise_project_identity(addon, Path(directory))
            if args.pilot_ifc:
                report["project_contract"] = exercise_project_contract(addon, Path(directory) / "contract", args.pilot_ifc)
        print("SWISS_GIS_IDENTITY_OK", json.dumps(report), flush=True)
    finally:
        if addon is not installed:
            addon.unregister()
            installed.register()


if __name__ == "__main__":
    main()
