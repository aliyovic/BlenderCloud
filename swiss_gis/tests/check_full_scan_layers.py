"""Verify the delivered full-scan views against the LAZ and local raster crops."""
from pathlib import Path
import hashlib
import json

import bpy
import laspy
import numpy as np
import rasterio


ROOT = Path(__file__).resolve().parents[1]
scene = bpy.data.scenes["01 Separate top views"]
manifest = json.loads(scene["swiss_gis_manifest_json"])
origin = np.array(manifest["origin"])
layers = {obj.get("swiss_gis_role"):obj for obj in scene.objects if obj.get("swiss_gis_role") != "annotation"}
scan = layers["scan"]
count = len(scan.data.vertices)
xyz = np.empty(count*3,dtype=np.float32)
scan.data.vertices.foreach_get("co",xyz)
xyz = xyz.reshape(-1,3).astype(np.float64)+origin
low = np.empty(count,dtype=np.int32);high = low.copy()
scan.data.attributes["source_id_low"].data.foreach_get("value",low)
scan.data.attributes["source_id_high"].data.foreach_get("value",high)
ids = low.astype(np.uint32).astype(np.uint64) | (high.astype(np.uint32).astype(np.uint64)<<32)
max_error = 0.0
verified = offset = 0
with laspy.open(manifest["source"]["path"]) as source:
    source_count = source.header.point_count
    assert source_count == 21_862_802
    assert source.header.parse_crs() is None
    for chunk in source.chunk_iterator(500_000):
        left,right = np.searchsorted(ids,[offset,offset+len(chunk)])
        local_ids = ids[left:right]-offset
        expected = np.column_stack((chunk.x[local_ids],chunk.y[local_ids],chunk.z[local_ids]))
        max_error = max(max_error,float(np.abs(xyz[left:right]-expected).max(initial=0)))
        verified += right-left
        offset += len(chunk)
assert verified == count and max_error < .0001
assert manifest["source"]["crs_assumed"] and manifest["vertical_datum"] == "UNKNOWN"
for obj in layers.values():
    assert np.allclose(np.array(obj.matrix_world),np.eye(4),atol=1e-12)
    assert json.loads(obj["swiss_gis_origin_json"]) == origin.tolist()
views = sorted((a for a in bpy.context.screen.areas if a.type == "VIEW_3D"),key=lambda a:a.x)
assert len(views) == 3
for panel,role in zip(views,("scan","aerial_map","terrain")):
    space = panel.spaces.active
    assert space.local_view is not None
    assert space.region_3d.view_perspective == "ORTHO"
    assert np.allclose(tuple(space.region_3d.view_rotation),(1,0,0,0))
    assert layers[role].local_view_get(space)
    assert not any(obj.local_view_get(space) for other,obj in layers.items() if other != role)
area = manifest["area"]
rows,cols = int(np.ceil((area[3]-area[1])/2)),int(np.ceil((area[2]-area[0])/2))
terrain = layers["terrain"]
assert len(terrain.data.vertices) == rows*cols
assert len(terrain.data.polygons) == (rows-1)*(cols-1)
assert len(manifest["terrain_sources"]) == len(manifest["imagery_sources"]) == 4
receipt = json.loads((ROOT/"data/swisstopo_full_scan/sources.json").read_text())
for item in receipt["sources"]:
    path = Path(item["local_file"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == item["local_sha256"]
    with rasterio.open(path) as raster:
        assert raster.crs.to_epsg() == 2056
        assert raster.tags()["SOURCE_REFERENCE"] == "Federal Office of Topography swisstopo"
        if item["product"] == "swissalti3d":
            assert raster.tags()["VERTICAL_DATUM"] == "LN02"
assert all(image.packed_file for image in bpy.data.images if image.source == "FILE")
detail = bpy.data.scenes["04 Full scan detail on zoom"]
full = next(obj for obj in detail.objects if obj.get("scan_cache"))
octree = json.loads((Path(full["scan_octree"])/"manifest.json").read_text())
assert octree["retained_points"] == source_count
assert list(full["scan_origin"]) == origin.tolist()
result = {"source_points":int(source_count),"overview_points":count,"source_ids_verified":int(verified),
          "max_coordinate_error_m":max_error,"shared_origin":origin.tolist(),
          "saved_separate_top_views":3,"local_raster_files":8,"local_raster_bytes":receipt["local_bytes_total"],
          "terrain_vertices":rows*cols,"terrain_quads":len(terrain.data.polygons),
          "full_detail_cache_points":octree["retained_points"],"crs_assumed":True,"scan_height_datum":"UNKNOWN",
          "network_policy":"OS denied network access during preparation, scene build and saved-file validation"}
(ROOT/"artifacts/full_scan_check.json").write_text(json.dumps(result,indent=2)+"\n")
print("FULL_SCAN_LAYERS_OK",result,flush=True)
