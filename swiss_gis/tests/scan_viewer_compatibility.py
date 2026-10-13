"""Verify both add-ons place the same scan under the established scene origin."""
from pathlib import Path
import importlib
import json
import uuid

import bpy
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
gis = importlib.import_module("bl_ext.user_default.swiss_gis_local")
viewer = importlib.import_module("scan_viewer")
if not hasattr(bpy.types.Object, "scan_display_count"):
    viewer.register()
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
scene = bpy.context.scene
scene["LV95_origin_m"] = [2689160., 1254972., 452.]
scene["scan_scene_origin"] = [2689160., 1254972., 452.]
cloud = viewer._load_object(ROOT / "artifacts/scan_viewer_railway", frame=False)
anchor_before = list(cloud.location)
manifest = gis.build(bpy.context, ROOT / "data/railway_local")
assert list(cloud.location) == anchor_before
assert len([obj for obj in scene.objects if "scan_cache" in obj]) == 1
own = next(obj for obj in scene.objects if obj.get("swiss_gis_role") == "scan")
cache = np.fromfile(ROOT / "artifacts/scan_viewer_railway/positions.f32", dtype=np.float32).reshape(-1, 3)
cache_origin = np.array(cloud["scan_origin"])
viewer_global = cache[:1].astype(np.float64) + cache_origin
own_global = np.array(tuple(own.data.vertices[0].co)) + manifest["origin"]
np.testing.assert_allclose(viewer_global[0], own_global, atol=.0001, rtol=0)
for key in ("scan_scene_origin", "LV95_origin_m"):
    assert list(scene[key]) == manifest["origin"]
# Preparing more context must reuse this scene anchor, including its height.
project = gis.Project(manifest["source"]["path"], max_points=1000, margin=1, origin=gis.scene_origin(scene))
prepared = gis.prepare(project, ROOT / "artifacts" / ("compat-" + uuid.uuid4().hex))
assert prepared["origin"] == manifest["origin"]
result = {"existing_scan_preserved": True, "shared_origin": manifest["origin"],
          "first_source_point_alignment_error_m": float(np.max(np.abs(viewer_global[0] - own_global))),
          "subsequent_preparation_reuses_origin": True}
(ROOT / "artifacts/compatibility.json").write_text(json.dumps(result, indent=2) + "\n")
print("SCAN_VIEWER_COMPATIBILITY_OK", result)
