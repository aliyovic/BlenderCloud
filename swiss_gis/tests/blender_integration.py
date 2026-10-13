"""Verify installed extension imports, persistent geometry and reference export."""
from pathlib import Path
import argparse
import importlib
import json
import sys
import time

import bpy
import laspy
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from fixtures import fixture_scan, fixture_raster
from check_project_identity import exercise_project_identity, exercise_project_contract
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-ifc", type=Path)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    addon = importlib.import_module("bl_ext.user_default.swiss_gis_local")
    assert "swiss_gis" in bpy.types.Scene.__dict__
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    working = ROOT / "artifacts/integration"
    working.mkdir(parents=True, exist_ok=True)
    scan_path = working / "fixture.laz"
    xyz = fixture_scan(scan_path)
    terrain_folder = working / "terrain"
    imagery_folder = working / "imagery"
    terrain_folder.mkdir(exist_ok=True)
    imagery_folder.mkdir(exist_ok=True)
    fixture_raster(terrain_folder / "ground.tif", np.full((8, 8), 450, dtype=np.float32))
    pixels = np.zeros((4, 8, 8), dtype=np.uint8)
    pixels[0, :4] = 255
    pixels[2, 4:] = 255
    pixels[3, :4] = 255
    fixture_raster(imagery_folder / "map.tif", pixels)
    settings = scene.swiss_gis
    settings.scan = str(scan_path)
    settings.terrain_folder = str(terrain_folder)
    settings.imagery_folder = str(imagery_folder)
    settings.max_points = 1000
    settings.margin = 1
    settings.terrain_step = 1
    assert bpy.ops.swiss_gis.inspect_scan() == {"FINISHED"}
    assert bpy.ops.swiss_gis.build_local() == {"FINISHED"}
    deadline = time.monotonic() + 60
    while addon._job is not None and time.monotonic() < deadline:
        addon._poll_job()
        time.sleep(.05)
    assert addon._job is None, "The real build operator did not complete"
    assert "Loaded" in settings.status, settings.status
    manifest = json.loads(scene["swiss_gis_manifest_json"])
    scan = next(obj for obj in scene.objects if obj.get("swiss_gis_role") == "scan")
    coords = np.array([tuple(vertex.co) for vertex in scan.data.vertices])
    np.testing.assert_allclose(coords + np.array(manifest["origin"]), xyz, atol=2e-6, rtol=0)
    terrain = next(obj for obj in scene.objects if obj.get("swiss_gis_role") == "terrain")
    coords = np.array([tuple(vertex.co) for vertex in terrain.data.vertices])
    np.testing.assert_allclose(coords[:, 2] + manifest["origin"][2], 450, atol=1e-5)
    assert terrain.data.uv_layers.active
    image = terrain.data.materials[0].node_tree.nodes.get("Image Texture").image
    assert image.packed_file is not None
    assert bpy.ops.swiss_gis.toggle_layer(role="terrain") == {"FINISHED"}
    assert terrain.hide_get() and terrain.hide_render
    assert bpy.ops.swiss_gis.toggle_layer(role="terrain") == {"FINISHED"}
    assert not terrain.hide_get() and not terrain.hide_render
    # The texture uses geographic UVs, with north at V=1.
    vertex = terrain.data.vertices[terrain.data.loops[0].vertex_index].co
    uv = terrain.data.uv_layers.active.data[0].uv
    expected = ((vertex.y + manifest["origin"][1] - manifest["aerial_area"][1]) /
                (manifest["aerial_area"][3] - manifest["aerial_area"][1]))
    assert abs(uv.y - expected) < 1e-6
    # A missing aerial pixel must leave the measured terrain visible.
    bpy.ops.swiss_gis.toggle_layer(role="scan")
    camera_data = bpy.data.cameras.new("Coverage check")
    camera = bpy.data.objects.new("Coverage check", camera_data)
    scene.collection.objects.link(camera)
    area = manifest["area"]
    camera.location = ((area[0]+area[2])/2-manifest["origin"][0],
                       (area[1]+area[3])/2-manifest["origin"][1], 20)
    camera.rotation_euler = (0, 0, 0)
    camera_data.type = "ORTHO"
    camera_data.ortho_scale = 4
    scene.camera = camera
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 4
    scene.render.resolution_x = scene.render.resolution_y = 64
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = True
    scene.world.node_tree.nodes["Background"].inputs[0].default_value = (1, 1, 1, 1)
    scene.world.node_tree.nodes["Background"].inputs[1].default_value = 1
    scene.render.filepath = str(working / "coverage.png")
    bpy.ops.render.render(write_still=True)
    rendered = bpy.data.images.load(str(working / "coverage.png"))
    pixels = np.array(rendered.pixels[:]).reshape(64, 64, 4)
    south = pixels[16, 32]
    assert south[3] > .99 and south[1] > south[0] and south[1] > south[2], south
    bpy.data.images.remove(rendered)
    bpy.ops.swiss_gis.toggle_layer(role="scan")
    destination = working / "persistent.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(destination))
    bundle = Path(scene["swiss_gis_bundle"])
    held = bundle.with_name(bundle.name + ".held")
    bundle.rename(held)
    try:
        bpy.ops.wm.open_mainfile(filepath=str(destination))
        reloaded = bpy.context.scene
        assert json.loads(reloaded["swiss_gis_origin_json"]) == manifest["origin"]
        assert len(next(obj for obj in reloaded.objects if obj.get("swiss_gis_role") == "scan").data.vertices) == 9
        assert all(image.packed_file is not None for image in bpy.data.images if image.source == "FILE")
        reference_path = working / "reference.json"
        assert bpy.ops.swiss_gis.export_reference(filepath=str(reference_path)) == {"FINISHED"}
        reference = json.loads(reference_path.read_text())
        assert reference["origin"] == manifest["origin"]
        assert reference["crs_assumed"] and reference["vertical_datum"] == "UNKNOWN"
        # A conflicting anchor must leave the existing scene untouched.
        before = len(reloaded.objects)
        reloaded["scan_scene_origin"] = [2689000., 1254000., 400.]
        try:
            addon.build(bpy.context, held)
            raise AssertionError("A conflicting scene origin was accepted")
        except addon.DataError:
            pass
        assert len(reloaded.objects) == before
        reloaded["scan_scene_origin"] = manifest["origin"]
        addon.unregister()
        addon.register()
        assert bpy.ops.swiss_gis.frame() == {"FINISHED"}
    finally:
        held.rename(bundle)
    result = {"package_import": "passed", "operator_build": "passed", "mesh_coordinate_roundtrip": "passed",
              "terrain_uv_alignment": "passed", "packed_image_reload_without_bundle": "passed",
              "reference_export": "passed", "layer_visibility": "passed", "terrain_visible_without_aerial_pixels": "passed",
              "conflicting_origin_rejected": "passed", "register_cycle": "passed"}
    with tempfile.TemporaryDirectory(prefix="swiss-gis-identity-") as directory:
        result["project_identity"] = exercise_project_identity(addon, Path(directory))
        if args.pilot_ifc:
            result["project_contract"] = exercise_project_contract(addon, Path(directory) / "contract", args.pilot_ifc)
    (ROOT / "artifacts/blender_integration.json").write_text(json.dumps(result, indent=2) + "\n")
    print("BLENDER_INTEGRATION_OK", result)


if __name__ == "__main__":
    main()
