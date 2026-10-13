"""Show the local full scan, aerial image and terrain in one Blender viewport."""
from pathlib import Path
import json

import bpy


ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "examples/full_scan_combined.blend"


def combine():
    scene = bpy.context.scene
    manifest = json.loads(scene["swiss_gis_manifest_json"])
    screen = bpy.context.window.screen
    if bpy.context.area.type == "CONSOLE":
        bpy.context.area.type = "VIEW_3D"
    views = sorted((a for a in screen.areas if a.type == "VIEW_3D"),key=lambda a:a.x)
    for panel in views:
        region = next(r for r in panel.regions if r.type == "WINDOW")
        with bpy.context.temp_override(area=panel,region=region):
            if panel.spaces.active.local_view:
                bpy.ops.view3d.localview(frame_selected=False)
    roles = {obj.get("swiss_gis_role"):obj for obj in scene.objects
             if obj.get("swiss_gis_role") in {"scan","aerial_map","terrain"}}
    if len(roles) != 3:
        raise RuntimeError("The combined view needs the scan, aerial map and terrain")
    for obj in scene.objects:
        if obj.get("swiss_gis_role") == "annotation":
            obj.hide_set(True)
            obj.hide_render = True
        else:
            obj.hide_set(False)
        obj.select_set(False)
    roles["terrain"].display_type = "WIRE"
    display_mesh = roles["terrain"].modifiers.get("Terrain overview wireframe")
    if display_mesh is None:
        display_mesh = roles["terrain"].modifiers.new("Terrain overview wireframe","DECIMATE")
    display_mesh.ratio = .05
    display_mesh.use_collapse_triangulate = True
    roles["terrain"].color = (.16,.45,.85,1)
    roles["terrain"].show_in_front = False
    roles["terrain"]["layer_description"] = "Original LN02 elevation mesh, displayed as wireframe over the local aerial map"
    detail = bpy.data.scenes["04 Full scan detail on zoom"]
    full_scan = next(obj for obj in detail.objects if obj.get("scan_cache"))
    parent = next(c for c in roles["scan"].users_collection if c in scene.collection.children[:])
    if full_scan.name not in parent.objects:
        parent.objects.link(full_scan)
    full_scan["swiss_gis_role"] = "scan_detail"
    full_scan.hide_set(False)
    full_scan.scan_display_count = min(full_scan["scan_retained_points"],5_000_000)
    scene.scan_point_budget = 5_000_000
    scene.scan_streaming = True
    roles["scan"].hide_set(True)
    # Join adjacent viewports. The geographic objects keep their coordinates.
    while len(views) > 1:
        left,right = views[:2]
        source = (left.x+left.width//2,left.y+left.height//2)
        target = (right.x+right.width//2,right.y+right.height//2)
        with bpy.context.temp_override(area=left):
            bpy.ops.screen.area_join(source_xy=source,target_xy=target)
        updated = sorted((a for a in screen.areas if a.type == "VIEW_3D"),key=lambda a:a.x)
        if len(updated) >= len(views):
            raise RuntimeError("Blender did not join the adjacent viewports")
        views = updated
    scene.name = "Full scan + aerial map + terrain"
    scene.swiss_gis.status = "Full scan, local aerial map and terrain in one view"
    space = views[0].spaces.active
    area,origin = manifest["area"],manifest["origin"]
    low,high = manifest["source"]["bounds"]
    space.region_3d.view_rotation = (1,0,0,0)
    space.region_3d.view_perspective = "ORTHO"
    space.region_3d.view_location = ((area[0]+area[2])/2-origin[0],
                                     (area[1]+area[3])/2-origin[1],
                                     (low[2]+high[2])/2-origin[2])
    space.region_3d.view_distance = max(area[2]-area[0],area[3]-area[1])*1.12
    space.clip_start = .05
    space.clip_end = 5000
    space.shading.type = "MATERIAL"
    space.shading.wireframe_color_type = "OBJECT"
    space.overlay.show_text = True
    space.overlay.show_floor = False
    space.overlay.show_axis_x = False
    space.overlay.show_axis_y = False
    space.overlay.show_cursor = False
    space.show_region_ui = False
    space.show_region_toolbar = False
    bpy.context.view_layer.objects.active = full_scan
    bpy.ops.wm.save_as_mainfile(filepath=str(DESTINATION),compress=True)
    print("COMBINED_FULL_SCAN_READY",DESTINATION,flush=True)


if __name__ == "__main__":
    combine()
