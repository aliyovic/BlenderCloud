import importlib.util
import json
from pathlib import Path
import sys
import time

import bpy

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("scan_viewer", root / "addon/__init__.py")
addon = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = addon
spec.loader.exec_module(addon)
addon.register()

for obj in tuple(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)

start = time.monotonic()
result = bpy.ops.scan_viewer.import_folder(directory="/Users/ali/Desktop/pointclouds")
assert result == {'FINISHED'}, result
while addon._jobs or addon._pending:
    addon._poll_jobs()
    time.sleep(0.1)
assert len([obj for obj in bpy.data.objects if "scan_cache" in obj]) == 15

scans = [obj for obj in bpy.data.objects if "scan_cache" in obj]
assert sum(obj["scan_retained_points"] for obj in scans) <= bpy.context.scene.scan_point_budget
assert sum(obj["scan_source_points"] for obj in scans if obj["scan_source"].endswith((".copc.laz", ".las"))) == 694431174
assert len({tuple(round(v) for v in obj.location[:2]) for obj in scans if obj["scan_source"].endswith(".copc.laz")}) == 13
assert len({str(obj["scan_source"]) for obj in scans}) == 15
assert list(bpy.context.scene["scan_scene_origin"]) == [2669000.0, 1250000.0, 0.0]

scene_path = root / "examples/all_desktop_scans.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(scene_path))
print("FOLDER_SMOKE_OK", json.dumps({
    "seconds": round(time.monotonic() - start, 3),
    "scans": len(scans),
    "source_points_las_copc": 694431174,
    "cached_points": sum(obj["scan_retained_points"] for obj in scans),
    "scene_origin": list(bpy.context.scene["scan_scene_origin"]),
    "scene": str(scene_path),
}))
