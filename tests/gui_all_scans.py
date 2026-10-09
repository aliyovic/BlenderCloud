import json
from pathlib import Path

import bpy

root = Path(__file__).resolve().parents[1]
bpy.ops.preferences.addon_enable(module="scan_viewer")
import scan_viewer


def capture():
    target = root / "tests/gui_all_scans.png"
    bpy.ops.screen.screenshot(filepath=str(target))
    status = {
        "scan_objects": len([obj for obj in bpy.data.objects if "scan_cache" in obj]),
        "gpu_batches": len(scan_viewer._batches),
        "handler_registered": scan_viewer._handler is not None,
        "point_budget": bpy.context.scene.scan_point_budget,
        "screenshot": str(target),
    }
    (root / "tests/gui_all_scans.json").write_text(json.dumps(status, indent=2))
    print("GUI_ALL_SCANS_OK", json.dumps(status))
    bpy.ops.wm.save_as_mainfile(filepath=str(root / "examples/all_desktop_scans.blend"))
    tile = next(obj for obj in bpy.data.objects if "2669-1250" in obj.name)
    tile.scan_point_size = 3.0
    scan_viewer._frame_scan(tile)
    scan_viewer._redraw()
    bpy.app.timers.register(capture_tile, first_interval=3.0)
    return None


def capture_tile():
    target = root / "tests/gui_one_tile.png"
    bpy.ops.screen.screenshot(filepath=str(target))
    print("GUI_TILE_OK", target)
    return None


def frame():
    scan_viewer._frame_all(bpy.context)
    scan_viewer._redraw()
    bpy.app.timers.register(capture, first_interval=3.0)
    return None


bpy.app.timers.register(frame, first_interval=2.0)
