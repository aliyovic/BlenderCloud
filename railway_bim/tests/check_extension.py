"""Exercise the installed extension's edit operators inside Blender/Bonsai.

Run in a fresh Blender process with the installed extension, for example::

    Blender --background --python check_extension.py -- --ifc railway_crop.ifc

Authoring gate: this check owns the installed UI-to-IFC edit contract. Core
acceptance tests cannot catch an unregistered operator, an edit sent to the
wrong open file, or Bonsai keeping a stale mesh after the saved IFC changes.
It checks real operators and imported objects without production test hooks.
The lifecycle case protects job completion across file loads and extension
disable/re-enable. It fails when Blender removes a job's nonpersistent timer,
leaving the sidebar disabled, or when an old job writes into a newly loaded
scene. Core tests cannot exercise Blender's timer and registration lifecycle.
The shared import helper checks retained Scan Viewer coordinates and subsequent
Swiss GIS preparation against Bonsai's real imported meshes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import addon_utils
import bpy
import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element
import numpy as np


ROOT = Path(__file__).resolve().parents[1]


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ifc", type=Path, default=ROOT / "output/railway_crop.ifc")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/extension-smoke")
    parser.add_argument("--lifecycle-only", action="store_true", help="Run only the Blender job lifecycle regression.")
    parser.add_argument("--extension-package", default="bl_ext.user_default.railway_bim")
    parser.add_argument("--extension-root", type=Path, help="Optional package search folder for an isolated regression baseline.")
    return parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])


def hashes(paths):
    result = {}
    for path in paths:
        with path.open("rb") as stream:
            result[str(path)] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def asset(model, asset_id):
    matches = [item for item in model.by_type("IfcProduct")
               if ifcopenshell.util.element.get_psets(item)
               .get("Pset_RailwayBIMAsset", {}).get("AssetId") == asset_id]
    assert len(matches) == 1, f"Expected one {asset_id}, got {len(matches)}"
    return matches[0]


def mesh_bounds(obj):
    bpy.context.view_layer.update()
    points = np.asarray([tuple(obj.matrix_world @ vertex.co) for vertex in obj.data.vertices])
    assert len(points), "The selected IFC object has no Blender mesh."
    return np.asarray([points.min(axis=0), points.max(axis=0)])


def ifc_bounds(element):
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    shape = ifcopenshell.geom.create_shape(settings, element)
    points = np.asarray(shape.geometry.verts).reshape(-1, 3)
    return np.asarray([points.min(axis=0), points.max(axis=0)])


def check_job_lifecycle(extension, package, reconstruction, output_dir):
    """Use real builds and Blender file/extension operations, without a worker mock."""
    folder = Path(tempfile.mkdtemp(prefix="lifecycle-", dir=output_dir))
    snapshot = folder / "loaded-scene.blend"
    loaded_status = "Loaded scene keeps its own status."
    bpy.context.scene.railway_bim.status = loaded_status
    assert "FINISHED" in bpy.ops.wm.save_as_mainfile(filepath=str(snapshot)), "Blender could not save the lifecycle fixture."

    def dispatch(name):
        target = folder / f"{name}.ifc"
        settings = bpy.context.scene.railway_bim
        settings.reconstruction_dir = str(reconstruction)
        settings.output_ifc = str(target)
        settings.replace_existing = False
        assert "FINISHED" in bpy.ops.railway_bim.build_ifc(), "The real build operator did not start."
        assert bpy.app.timers.is_registered(extension._poll_job), "The build operator did not register its completion timer."
        assert not bpy.ops.railway_bim.open_ifc.poll(), "The UI did not lock while completion was pending."
        return target

    def complete(target, scene_status):
        # Background --python blocks Blender's event loop. Dispatch the real
        # registered callback until it returns the normal timer-stop value.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            interval = extension._poll_job()
            if interval is None:
                if bpy.app.timers.is_registered(extension._poll_job):
                    bpy.app.timers.unregister(extension._poll_job)
                break
            time.sleep(min(float(interval), 0.2))
        else:
            raise AssertionError("The real build did not complete within 60 seconds.")
        for suffix in (".ifc", ".railway-project.json", ".asset-register.json"):
            assert target.with_suffix(suffix).is_file(), "The real worker did not deliver its complete file bundle."
        ifcopenshell.open(str(target))
        assert bpy.ops.railway_bim.open_ifc.poll(), "The sidebar stayed disabled after job completion."
        assert bpy.context.scene.railway_bim.status == scene_status, "An old job overwrote the current scene's status."

    file_load_target = dispatch("file-load-job")
    assert "FINISHED" in bpy.ops.wm.open_mainfile(filepath=str(snapshot)), "Blender could not load the lifecycle fixture."
    assert bpy.app.timers.is_registered(extension._poll_job), "Loading a .blend removed the job completion timer and left the UI disabled."
    assert bpy.context.scene.railway_bim.status == loaded_status, "The loaded fixture lost its saved scene status."
    complete(file_load_target, loaded_status)

    reenable_target = dispatch("reenable-job")
    addon_utils.disable(package, default_set=False)
    assert not bpy.app.timers.is_registered(extension._poll_job), "Disabling the extension left its timer registered."
    assert not hasattr(bpy.types.Scene, "railway_bim"), "Disabling the extension left its scene registration active."
    enabled = addon_utils.enable(package, default_set=False, persistent=True)
    assert enabled is extension, "Re-enabling replaced the module and lost the existing job."
    assert bpy.app.timers.is_registered(extension._poll_job), "Re-enabling the extension did not restore the pending job's timer."
    assert not bpy.ops.railway_bim.open_ifc.poll(), "The pending job lost its UI lock during re-enable."
    reenabled_status = "Re-enabled scene keeps its own status."
    bpy.context.scene.railway_bim.status = reenabled_status
    complete(reenable_target, reenabled_status)
    return {
        "real_worker_completed_after_file_load": True,
        "completion_timer_survived_file_load": True,
        "completion_timer_restored_after_reenable": True,
        "controls_reenabled_after_completion": True,
        "old_job_did_not_overwrite_current_scene": True,
        "file_load_ifc": str(file_load_target),
        "reenable_ifc": str(reenable_target),
    }


def main():
    args = arguments()
    source = args.ifc.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / "railway-ui-smoke.ifc"
    suffixes = (".ifc", ".railway-project.json", ".asset-register.json")
    source_files = [source.with_suffix(suffix) for suffix in suffixes]
    working_files = [destination.with_suffix(suffix) for suffix in suffixes]
    assert not set(source_files) & set(working_files), "Smoke-test copies must differ from the primary delivery."
    primary_hashes = hashes(source_files)
    for original, copied in zip(source_files, working_files):
        shutil.copy2(original, copied)

    bonsai_package = "bl_ext.user_default.bonsai"
    extension_package = args.extension_package
    if args.extension_root:
        sys.path.insert(0, str(args.extension_root.resolve()))
    if not hasattr(bpy.types.Scene, "BIMProperties") or bonsai_package not in bpy.context.preferences.addons:
        addon_utils.enable(bonsai_package, default_set=True, persistent=True)
    if extension_package != "bl_ext.user_default.railway_bim" and hasattr(bpy.types.Scene, "railway_bim"):
        addon_utils.disable("bl_ext.user_default.railway_bim", default_set=False)
    addon_utils.enable(extension_package, default_set=False, persistent=True)
    extension = importlib.import_module(extension_package)
    assert hasattr(bpy.types.Scene, "railway_bim"), "The installed Railway BIM extension did not register."
    import bonsai.tool as tool

    reconstruction = Path(json.loads(source.with_suffix(".railway-project.json").read_text())["reconstruction_dir"])
    if not reconstruction.is_absolute():
        reconstruction = (source.parent / reconstruction).resolve()
    if args.lifecycle_only:
        report = {"ok": True, "package": extension_package,
                  "job_lifecycle": check_job_lifecycle(extension, extension_package, reconstruction, output_dir)}
        assert hashes(source_files) == primary_hashes, "The lifecycle check changed the primary delivery."
        report["primary_delivery_unchanged"] = True
        (output_dir / "lifecycle-check.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        return

    assert tool.Ifc.get() is None, "Run this check in a fresh process with no IFC project open."
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from check_import_reference import exercise_import_reference

    import_reference = exercise_import_reference(destination, output_dir, extension_package)
    assert hashes(source_files) == primary_hashes, "The import reference check changed the primary delivery."
    settings = bpy.context.scene.railway_bim
    settings.output_ifc = str(destination)
    assert "FINISHED" in bpy.ops.railway_bim.open_ifc(), "The extension did not import the copied IFC."
    model = tool.Ifc.get()
    mast = asset(model, "Mast 01")
    other = asset(model, "Rail 03")
    mast_guid, other_guid = mast.GlobalId, other.GlobalId
    mast_obj, other_obj = tool.Ifc.get_object(mast), tool.Ifc.get_object(other)
    assert mast_obj is not None and other_obj is not None, "Bonsai did not create authoring objects."
    other_mesh_before = mesh_bounds(other_obj)
    other_ifc_before = ifc_bounds(other)
    mast_before = mesh_bounds(mast_obj)
    bpy.ops.object.select_all(action="DESELECT")
    mast_obj.select_set(True)
    bpy.context.view_layer.objects.active = mast_obj
    settings = bpy.context.scene.railway_bim
    settings.parameter = "height_m"
    assert "FINISHED" in bpy.ops.railway_bim.read_dimension(), "The selected asset dimension could not be read."
    original_height = float(mast_before[1, 2] - mast_before[0, 2])
    assert abs(settings.value_m - original_height) < 1e-5, "The sidebar read a dimension for another object."

    saved_before = hashes(working_files)
    settings.value_m = 0.0
    try:
        result = bpy.ops.railway_bim.rebuild_asset()
        assert "CANCELLED" in result, "The UI accepted a zero-height mast."
        rejection = "Operator cancelled invalid height."
    except RuntimeError as exc:
        rejection = str(exc)
        assert "height_m" in rejection and "between" in rejection, "The UI rejected input for a different reason."
    assert hashes(working_files) == saved_before, "Invalid UI input changed the IFC or evidence files."
    assert np.array_equal(mesh_bounds(mast_obj), mast_before), "Invalid UI input changed the live mesh."

    target_height = 13.0
    settings.value_m = target_height
    assert "FINISHED" in bpy.ops.railway_bim.rebuild_asset(), "The extension's saved edit failed."
    reopened = ifcopenshell.open(str(destination))
    exported_mast = asset(reopened, "Mast 01")
    exported_other = asset(reopened, "Rail 03")
    assert exported_mast.GlobalId == mast_guid and exported_other.GlobalId == other_guid, "The UI edit changed persistent identities."
    saved_bounds, live_bounds = ifc_bounds(exported_mast), mesh_bounds(mast_obj)
    assert abs(saved_bounds[1, 2] - saved_bounds[0, 2] - target_height) < 1e-6, "The saved mast does not have the requested height."
    assert np.allclose(live_bounds, saved_bounds, atol=1e-5), "Bonsai kept stale geometry after the edit."
    assert np.array_equal(mesh_bounds(other_obj), other_mesh_before), "The UI edit changed another Blender asset."
    assert np.allclose(ifc_bounds(exported_other), other_ifc_before, atol=1e-8), "The saved edit changed another IFC asset."
    current = tool.Ifc.get_entity(mast_obj)
    assert current.GlobalId == mast_guid, "The edited object lost its Bonsai authoring link."
    assert np.allclose(ifc_bounds(current), saved_bounds, atol=1e-8), "Bonsai's active IFC file still has the previous representation."
    register = json.loads(destination.with_suffix(".asset-register.json").read_text())
    record = next(item for item in register["assets"] if item["asset_id"] == "Mast 01")
    assert record["revision"] == 1, "The sidebar is using an old asset register."
    assert abs(record["parameters"]["z_top"] - record["parameters"]["z_bottom"] - target_height) < 1e-6
    last_result = json.loads(settings.last_result)
    assert last_result["asset_id"] == "Mast 01" and last_result["revision"] == 1, "The UI did not report its saved edit."
    assert "FINISHED" in bpy.ops.railway_bim.read_dimension()
    assert abs(settings.value_m - target_height) < 1e-6, "The sidebar did not refresh its dimension."
    assert hashes(source_files) == primary_hashes, "The smoke test changed the primary delivery."

    report = {
        "ok": True,
        "package": extension_package,
        "working_ifc": str(destination),
        "asset_id": "Mast 01",
        "ifc_guid": mast_guid,
        "original_height_m": original_height,
        "saved_height_m": target_height,
        "live_mesh_matches_saved_ifc": True,
        "invalid_edit_rejection": rejection,
        "invalid_input_left_files_unchanged": True,
        "other_asset_unchanged": True,
        "primary_delivery_hashes": primary_hashes,
        "primary_delivery_unchanged": True,
        "ui_status": settings.status,
        "import_reference": import_reference,
    }
    report["job_lifecycle"] = check_job_lifecycle(extension, extension_package, reconstruction, output_dir)
    assert hashes(source_files) == primary_hashes, "The lifecycle check changed the primary delivery."
    report_path = output_dir / "extension-check.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
