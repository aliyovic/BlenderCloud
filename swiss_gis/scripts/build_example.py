"""Build the railway example with the installed extension in Blender."""
from pathlib import Path
import importlib
import json
import sys

import bpy
from mathutils import Vector


ROOT = Path(__file__).resolve().parents[1]


def main():
    addon = importlib.import_module("bl_ext.user_default.swiss_gis_local")
    args = sys.argv[sys.argv.index("--")+1:] if "--" in sys.argv else []
    bundle = Path(args[0]).resolve() if args else ROOT / "data/railway_local"
    name = args[1] if len(args) > 1 else "railway_local"
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for key in ("swiss_gis_origin_json", "swiss_gis_manifest_json", "scan_scene_origin", "LV95_origin_m"):
        if key in bpy.context.scene:
            del bpy.context.scene[key]
    manifest = addon.build(bpy.context, bundle, .035)
    settings = bpy.context.scene.swiss_gis
    settings.scan = manifest["source"]["path"]
    if manifest["terrain_sources"]:
        settings.terrain_folder = str(Path(manifest["terrain_sources"][0]["path"]).parent)
    if manifest["imagery_sources"]:
        settings.imagery_folder = str(Path(manifest["imagery_sources"][0]["path"]).parent)
    settings.margin = 50
    settings.radius = .035
    settings.status = f"Local railway project, {manifest['preview_points']:,} preview points"
    scene = bpy.context.scene
    scene.name = "Railway, offline LV95 context"
    camera_data = bpy.data.cameras.new("Railway overview")
    camera = bpy.data.objects.new("Railway overview", camera_data)
    scene.collection.objects.link(camera)
    camera.location = (67, -87, 72)
    target = Vector((0, 0, -3))
    camera.rotation_euler = (target-camera.location).to_track_quat("-Z", "Y").to_euler()
    camera_data.type = "ORTHO"
    camera_data.ortho_scale = 195 if manifest["terrain_sources"] else 95
    scene.camera = camera
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 24
    scene.cycles.use_denoising = True
    scene.render.resolution_x = 1500
    scene.render.resolution_y = 1000
    scene.render.resolution_percentage = 100
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs[0].default_value = (.23, .27, .32, 1)
    scene.world.node_tree.nodes["Background"].inputs[1].default_value = .8
    light_data = bpy.data.lights.new("Soft daylight", "AREA")
    light_data.energy = 12000
    light_data.shape = "DISK"
    light_data.size = 80
    light = bpy.data.objects.new("Soft daylight", light_data)
    scene.collection.objects.link(light)
    light.location = (0, -20, 80)
    camera.hide_set(True)
    light.hide_set(True)
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    # A flat map keeps scan points visible while their height datum is unknown.
    if manifest["imagery_sources"]:
        for obj in scene.objects:
            if obj.get("swiss_gis_role") == "terrain":
                obj.hide_set(True)
            elif obj.get("swiss_gis_role") == "aerial_map":
                obj.hide_set(False)
    # Show the full context area when independent terrain is available.
    initial = dict(manifest)
    low, high = manifest["source"]["bounds"]
    if not manifest["terrain_sources"]:
        initial["area"] = [low[0], low[1], high[0], high[1]]
    addon.frame_area(bpy.context, initial, top=bool(manifest["imagery_sources"]))
    destination = ROOT / "examples" / (name + ".blend")
    bpy.ops.wm.save_as_mainfile(filepath=str(destination))
    addon.export_reference(scene, str(ROOT / "examples" / (name + "_reference.json")))
    scene.render.filepath = str(ROOT / "artifacts" / (name + "_preview.png"))
    bpy.ops.render.render(write_still=True)
    print("EXAMPLE_READY", destination)


if __name__ == "__main__":
    main()
