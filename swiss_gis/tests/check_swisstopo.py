"""Verify the real cropped tiles and their saved Blender placement offline."""
from pathlib import Path
import hashlib
import json

import bpy
import numpy as np
import rasterio


ROOT = Path(__file__).resolve().parents[1]
scene = bpy.context.scene
manifest = json.loads(scene["swiss_gis_manifest_json"])
receipt = json.loads((ROOT / "data/swisstopo_area/sources.json").read_text())
assert len(manifest["terrain_sources"]) == len(manifest["imagery_sources"]) == 2
assert receipt["local_bytes_total"] < 6_000_000
for source in receipt["sources"]:
    path = Path(source["local_file"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == source["local_sha256"]
    with rasterio.open(path) as raster:
        assert raster.crs.to_epsg() == 2056
        assert raster.tags()["SOURCE_REFERENCE"] == "Federal Office of Topography swisstopo"
        assert raster.bounds.left >= 2689076 and raster.bounds.right <= 2689246
        assert raster.bounds.bottom >= 1254894 and raster.bounds.top <= 1255048
        if source["product"] == "swissalti3d":
            assert raster.tags()["VERTICAL_DATUM"] == "LN02"
terrain = next(obj for obj in scene.objects if obj.get("swiss_gis_role") == "terrain")
xyz = np.empty(len(terrain.data.vertices)*3, dtype=np.float32)
terrain.data.vertices.foreach_get("co", xyz)
xyz = xyz.reshape(-1, 3).astype(np.float64) + manifest["origin"]
assert len(terrain.data.polygons) == 6300
assert np.isfinite(xyz).all()
assert xyz[:, 1].min() < 1255000 < xyz[:, 1].max()
# All 85 by 76 grid vertices are present, including the source tile seam.
assert len(xyz) == 85*76
assert manifest["vertical_datum"] == "UNKNOWN"
assert all(source["vertical_datum"] == "LN02" for source in manifest["terrain_sources"])
indices = np.empty(len(terrain.data.loops), dtype=np.int32)
terrain.data.loops.foreach_get("vertex_index", indices)
uv = np.empty(len(indices)*2, dtype=np.float32)
terrain.data.uv_layers.active.data.foreach_get("uv", uv)
uv = uv.reshape(-1, 2)
area = np.array(manifest["aerial_area"])
expected = (xyz[indices, :2]-area[:2])/(area[2:]-area[:2])
np.testing.assert_allclose(uv, expected, atol=1e-6, rtol=0)
image = terrain.data.materials[0].node_tree.nodes.get("Image Texture").image
assert image.packed_file is not None
assert json.loads(scene["swiss_gis_source_references_json"]) == ["Federal Office of Topography swisstopo"]
result = {"local_crop_hashes":"passed", "local_crops_inside_selected_area":"passed",
          "two_terrain_and_two_imagery_sources":"passed", "terrain_complete_across_tile_seam":"passed",
          "terrain_vertex_count":len(xyz), "terrain_quad_count":len(terrain.data.polygons),
          "aerial_uv_coordinate_error_max":float(np.max(np.abs(uv-expected))),
          "packed_aerial_image":"passed", "source_attribution":"passed",
          "local_raster_bytes":receipt["local_bytes_total"], "height_datum_confirmation":"still required"}
(ROOT / "artifacts/swisstopo_check.json").write_text(json.dumps(result, indent=2) + "\n")
print("SWISSTOPO_CONTEXT_OK", result)
