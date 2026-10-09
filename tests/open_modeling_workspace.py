"""Open the tested crop in a perspective view with vertex snapping ready."""

import math
from pathlib import Path

import bpy
from mathutils import Euler

root = Path(__file__).resolve().parents[1]
bpy.ops.preferences.addon_enable(module="scan_viewer")
import scan_viewer as addon


def prepare():
    crop = next(obj for obj in bpy.context.scene.objects if obj.get("scan_full_resolution_crop"))
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    for obj in bpy.context.scene.objects:
        if "scan_cache" in obj and obj != crop:
            obj.hide_set(True)
        if obj.type == 'MESH' and obj.data.attributes.get("scan_node_id"):
            obj.display_type = 'WIRE'
    crop.select_set(True)
    crop.scan_point_size = 3
    bpy.context.view_layer.objects.active = crop
    area = max((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), key=lambda a:a.width*a.height)
    space = area.spaces.active
    space.show_region_ui = True
    space.overlay.show_overlays = False
    view = space.region_3d
    addon._frame_scan(crop)
    view.view_rotation = Euler((math.radians(55),0,math.radians(25)), 'XYZ').to_quaternion()
    view.view_perspective = 'PERSP'
    view.view_distance = 35
    bpy.context.scene.tool_settings.use_snap = True
    bpy.context.scene.tool_settings.snap_elements = {'VERTEX'}
    addon._redraw()
    bpy.app.timers.register(save,first_interval=1.5)
    return None


def save():
    bpy.ops.wm.save_as_mainfile(filepath=str(root/"examples/copc_modeling_crop.blend"))
    bpy.ops.screen.screenshot(filepath=str(root/"examples/copc_modeling_workspace.png"))
    print("MODELING_WORKSPACE_READY",flush=True)
    return None


bpy.app.timers.register(prepare,first_interval=1.0)
