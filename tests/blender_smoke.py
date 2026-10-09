import importlib.util
from pathlib import Path
import sys

import bpy

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("scan_viewer", root / "addon/__init__.py")
addon = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = addon
spec.loader.exec_module(addon)
addon.register()

sample = addon._load_object(root / "cache/cras_sample")
assert sample["scan_retained_points"] == 584702
assert len(bpy.data.objects) >= 2
sample.scan_color_mode = 'CLASS'
sample.scan_display_count = 100000
result = bpy.ops.scan_viewer.snap_sample(max_vertices=10000)
assert result == {'FINISHED'}
mesh = bpy.context.active_object
assert len(mesh.data.vertices) == 10000

synthetic = addon._load_object(root / "cache/synthetic")
assert synthetic["scan_source_points"] == 3427151
assert synthetic["scan_retained_points"] == 856788

las = addon._load_object(root / "cache/swiss_las")
assert las["scan_origin"][0] == 2608000
assert las["scan_origin"][1] == 1228000

for obj in (sample, mesh, las):
    obj.hide_set(True)
for obj in bpy.data.objects:
    obj.select_set(False)
synthetic.select_set(True)
bpy.context.view_layer.objects.active = synthetic

destination = root / "tests/scan_viewer_smoke.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(destination))
print("BLENDER_SMOKE_OK", destination)
