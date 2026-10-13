"""Check the actual railway preview against original source coordinates."""
from pathlib import Path
import json

import bpy
import laspy
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
scene = bpy.context.scene
manifest = json.loads(scene["swiss_gis_manifest_json"])
obj = next(obj for obj in scene.objects if obj.get("swiss_gis_role") == "scan")
xyz = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
obj.data.vertices.foreach_get("co", xyz)
xyz = xyz.reshape(-1, 3).astype(np.float64) + np.array(manifest["origin"])
low = np.empty(len(xyz), dtype=np.int32)
high = np.empty(len(xyz), dtype=np.int32)
obj.data.attributes["source_id_low"].data.foreach_get("value", low)
obj.data.attributes["source_id_high"].data.foreach_get("value", high)
ids = low.astype(np.uint32).astype(np.uint64) | (high.astype(np.uint32).astype(np.uint64) << 32)
las = laspy.read(manifest["source"]["path"])
expected = np.column_stack((las.x[ids], las.y[ids], las.z[ids]))
error = float(np.max(np.abs(xyz - expected)))
assert len(xyz) == 687530
assert len(las) == 5500234
assert error < 2e-6, error
assert manifest["origin"] == [2689160., 1254972., 452.]
assert manifest["source"]["crs_assumed"] and manifest["vertical_datum"] == "UNKNOWN"
assert len(next(obj for obj in scene.objects if obj.get("swiss_gis_role") == "scan_ground").data.polygons) == 7418
assert all(image.packed_file for image in bpy.data.images if image.source == "FILE")
result = {"source_points": len(las), "preview_points": len(xyz), "max_coordinate_error_m": error,
          "source_indices_verified": len(ids), "origin": manifest["origin"],
          "height_datum": "UNKNOWN", "crs_assumed": True, "ground_faces": 7418,
          "selected_area_m": [manifest["area"][2]-manifest["area"][0], manifest["area"][3]-manifest["area"][1]],
          "network_policy": "OS network access denied for package import, scene build and validation"}
(ROOT / "artifacts/railway_check.json").write_text(json.dumps(result, indent=2) + "\n")
print("RAILWAY_COORDINATES_OK", result)
