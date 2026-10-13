"""Save separate, geographically aligned views of the full railway scan."""
from pathlib import Path
import importlib
import json
import sys

import bpy
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "data/full_scan_context"
DESTINATION = ROOT / "examples/full_scan_local_layers.blend"
NAMES = {"scan": "01 SCAN - measured points", "aerial_map": "02 AERIAL - SWISSIMAGE 2025",
         "terrain": "03 TERRAIN - swissALTI3D 2026"}


def label(collection, title, subtitle, area, origin):
    mat = bpy.data.materials.get("Layer label white") or bpy.data.materials.new("Layer label white")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (1, 1, 1, 1)
    bsdf.inputs["Emission Color"].default_value = (1, 1, 1, 1)
    bsdf.inputs["Emission Strength"].default_value = 1
    for line, text, size in ((0, title, 13), (1, subtitle, 6.5)):
        curve = bpy.data.curves.new(text, "FONT")
        curve.body = text
        curve.size = size
        obj = bpy.data.objects.new(text, curve)
        obj.location = (area[0]-origin[0], area[3]-origin[1]+30-line*15, 500-origin[2])
        obj["swiss_gis_role"] = "annotation"
        collection.objects.link(obj)
        curve.materials.append(mat)


def frame(space, manifest, width=1000, height=1000):
    area, origin = manifest["area"], manifest["origin"]
    low, high = manifest["source"]["bounds"]
    space.clip_start = .05
    space.clip_end = 5000
    space.region_3d.view_rotation = (1, 0, 0, 0)
    space.region_3d.view_perspective = "ORTHO"
    space.region_3d.view_location = ((area[0]+area[2])/2-origin[0],
                                     (area[1]+area[3]+40)/2-origin[1],
                                     (low[2]+high[2])/2-origin[2])
    space.region_3d.view_distance = max(area[2]-area[0], area[3]-area[1]+60)*1.12*max(1,height/max(width,1))*.78
    space.shading.type = "MATERIAL"
    space.shading.studiolight_rotate_z = 0
    space.show_region_ui = False
    space.show_region_toolbar = False
    space.overlay.show_floor = False
    space.overlay.show_axis_x = False
    space.overlay.show_axis_y = False
    space.overlay.show_cursor = False
    space.overlay.show_text = False
    space.overlay.show_extras = False


def build():
    addon = importlib.import_module("bl_ext.user_default.swiss_gis_local")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    scene.name = "01 Separate top views"
    manifest = addon.build(bpy.context, BUNDLE, .06)
    scene.swiss_gis.scan = manifest["source"]["path"]
    scene.swiss_gis.terrain_folder = str(ROOT / "data/swisstopo_full_scan/terrain")
    scene.swiss_gis.imagery_folder = str(ROOT / "data/swisstopo_full_scan/imagery")
    scene.swiss_gis.margin = 50
    scene.swiss_gis.radius = .06
    scene.swiss_gis.status = "Full scan extent, three separate local layers"
    origin, area = manifest["origin"], manifest["area"]
    collections = {}
    layers = {}
    for obj in list(scene.objects):
        role = obj.get("swiss_gis_role")
        if role not in NAMES:
            bpy.data.objects.remove(obj, do_unlink=True)
            continue
        collection = bpy.data.collections.new(NAMES[role])
        collection.color_tag = {"scan":"COLOR_02", "aerial_map":"COLOR_04", "terrain":"COLOR_05"}[role]
        scene.collection.children.link(collection)
        for parent in list(obj.users_collection):
            parent.objects.unlink(obj)
        collection.objects.link(obj)
        obj.name = NAMES[role]
        obj.hide_set(False)
        obj.hide_render = False
        obj["layer_description"] = {"scan":"Measured RGB points from the complete source extent",
                                    "aerial_map":"Independent orthophotograph on a flat display plane",
                                    "terrain":"Independent elevation mesh, LN02 heights, height colours"}[role]
        collections[role], layers[role] = collection, obj
    for collection in list(scene.collection.children):
        if not collection.objects and not collection.children:
            bpy.data.collections.remove(collection)
    terrain = layers["terrain"]
    z = np.array([vertex.co.z+origin[2] for vertex in terrain.data.vertices])
    lo, hi = float(z.min()), float(z.max())
    t = (z-lo)/max(hi-lo, 1e-6)
    anchors = np.array([[.15,.32,.65], [.10,.65,.64], [.61,.77,.40], [.80,.68,.42], [.94,.89,.73]])
    rgb = np.column_stack([np.interp(t, np.linspace(0,1,len(anchors)), anchors[:,i]) for i in range(3)])
    addon.scene.set_colors(terrain, rgb)
    terrain.data.materials.clear()
    terrain.data.materials.append(addon.scene.vertex_material("Terrain height colours, LN02"))
    terrain["height_range_ln02_m"] = [lo, hi]
    label(collections["scan"], "1. FULL SCAN", "21,862,802 source points | RGB overview", area, origin)
    label(collections["aerial_map"], "2. AERIAL MAP", "SWISSIMAGE 2025 | photograph", area, origin)
    label(collections["terrain"], "3. TERRAIN", f"swissALTI3D 2026 | LN02 {lo:.1f} to {hi:.1f} m", area, origin)
    for role in NAMES:
        separate = bpy.data.scenes.new(NAMES[role])
        separate.collection.children.link(collections[role])
        separate.unit_settings.system = "METRIC"
        separate.unit_settings.scale_length = 1
        for key in ("swiss_gis_origin_json", "swiss_gis_epsg", "swiss_gis_vertical_datum",
                    "swiss_gis_manifest_json", "swiss_gis_bundle", "swiss_gis_source_references_json", "scan_scene_origin"):
            separate[key] = scene[key]
    # Keep the complete cached scan available for interactive detail on zoom.
    sys.path.insert(0, "/Applications/Open Point Cloud in Blender.app/Contents/Resources/addons")
    if "scan_viewer" not in bpy.context.preferences.addons:
        bpy.ops.preferences.addon_enable(module="scan_viewer")
    scan_viewer = importlib.import_module("scan_viewer")
    detail = bpy.data.scenes.new("04 Full scan detail on zoom")
    detail["scan_scene_origin"] = origin
    for key in ("swiss_gis_origin_json", "swiss_gis_epsg", "swiss_gis_vertical_datum", "swiss_gis_manifest_json"):
        detail[key] = scene[key]
    bpy.context.window.scene = detail
    source = Path(manifest["source"]["path"])
    cache = scan_viewer._cache_path(source, 5_000_000)
    obj = scan_viewer._load_object(cache, frame=False)
    octree = scan_viewer._octree_path(source)
    if (octree/"manifest.json").is_file():
        obj["scan_octree"] = str(octree)
    obj.name = "Full scan - 21,862,802 measured source points"
    detail.scan_point_budget = 5_000_000
    bpy.context.window.scene = scene
    for screen in bpy.data.screens:
        for panel in screen.areas:
            if panel.type == "VIEW_3D":
                frame(panel.spaces.active, manifest, panel.width, panel.height)
    scene["full_scan_layer_collections_json"] = json.dumps({role:c.name for role,c in collections.items()})
    bpy.ops.wm.save_as_mainfile(filepath=str(DESTINATION), compress=True)
    addon.export_reference(scene, str(ROOT / "examples/full_scan_local_layers_reference.json"))
    print("FULL_SCAN_LAYERS_READY", DESTINATION, flush=True)


def split_views():
    scene = bpy.context.scene
    manifest = json.loads(scene["swiss_gis_manifest_json"])
    screen = bpy.context.window.screen
    panel = bpy.context.area
    if panel.type == "CONSOLE":
        panel.type = "VIEW_3D"
    viewports = [a for a in screen.areas if a.type == "VIEW_3D"]
    if len(viewports) == 1:
        with bpy.context.temp_override(area=panel):
            bpy.ops.screen.area_split(direction="VERTICAL", factor=1/3)
        right = max((a for a in screen.areas if a.type == "VIEW_3D"), key=lambda a:a.width)
        with bpy.context.temp_override(area=right):
            bpy.ops.screen.area_split(direction="VERTICAL", factor=.5)
    viewports = sorted((a for a in screen.areas if a.type == "VIEW_3D"), key=lambda a:a.x)
    if len(viewports) != 3:
        raise RuntimeError("The comparison needs three 3D viewports")
    names = json.loads(scene["full_scan_layer_collections_json"])
    for panel, role in zip(viewports, NAMES):
        space = panel.spaces.active
        region = next(r for r in panel.regions if r.type == "WINDOW")
        with bpy.context.temp_override(area=panel, region=region):
            if space.local_view:
                bpy.ops.view3d.localview(frame_selected=False)
            bpy.ops.object.select_all(action="DESELECT")
            objects = list(bpy.data.collections[names[role]].objects)
            for obj in objects:
                obj.select_set(True)
            bpy.context.view_layer.objects.active = next(o for o in objects if o.get("swiss_gis_role") == role)
            bpy.ops.view3d.localview(frame_selected=False)
        frame(space, manifest, panel.width, panel.height)
        panel.tag_redraw()
    for obj in scene.objects:
        obj.select_set(False)
    bpy.ops.wm.save_as_mainfile(filepath=str(DESTINATION), compress=True)
    print("SEPARATE_TOP_VIEWS_SAVED", flush=True)


if __name__ == "__main__":
    build()
