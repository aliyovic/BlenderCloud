from pathlib import Path

import bpy

root = Path(__file__).resolve().parents[1]
bpy.ops.preferences.addon_enable(module="scan_viewer")
import scan_viewer

for obj in tuple(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
cloud = scan_viewer._load_object(root / "cache/swiss_copc")
cloud.scan_color_mode = 'CLASS'
cloud.scan_point_size = 2.0
assert cloud["scan_origin"][0] == 2669000.0
def capture():
    target = root / "tests/gui_copc.png"
    bpy.ops.screen.screenshot(filepath=str(target))
    print("GUI_COPC_RETEST_OK", target)
    return None

bpy.app.timers.register(capture, first_interval=4.0)
