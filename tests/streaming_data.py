"""Compare a real COPC modeling crop with an independent laspy query."""

import importlib.util
import json
from pathlib import Path
import tempfile
import time

import laspy
from laspy.copc import Bounds
import numpy as np

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("scan_stream_protocol", root / "addon/streaming.py")
streaming = importlib.util.module_from_spec(spec)
spec.loader.exec_module(streaming)
source = Path("/Users/ali/Desktop/pointclouds/ZH/swisssurface3d_2024_2670-1253_2056_5728.copc.laz")
origin = np.array([2670000.0, 1253000.0, 0.0])
client = streaming.RustClient(root / "target/release/scan-cache")


def view(center, halfwidth):
    center = np.asarray(center) - origin
    return {"source": str(source), "object": "test", "origin": origin.tolist(), "width": 1200, "height": 900,
            "matrix": [[1/halfwidth,0,0,-center[0]/halfwidth],
                       [0,1/halfwidth,0,-center[1]/halfwidth],
                       [0,0,-1/1000,center[2]/1000],[0,0,0,1]]}


try:
    far, _ = client.request({"op":"plan", "views":[view([2670500,1253500,410],750)],
                             "budget":1_500_000,"pixel_error":1.5,"cap":65536})
    root_node = next(n for n in far["nodes"] if n["key"][0] == 0)
    packet, data = client.request(dict(op="node", source=str(source), stamp=root_node["stamp"],
                                      node=root_node["node"], origin=origin.tolist(), cap=65536))
    count = packet["count"]
    positions = np.frombuffer(data, '<f4', count=count*3).reshape(-1,3).astype(np.float64) + origin
    classes = np.frombuffer(data, 'u1', count=count, offset=count*17)
    buildings = positions[classes == 6]
    assert len(buildings) > 0
    center = buildings[np.argmin(np.linalg.norm(buildings[:,:2] - [2670500,1253500], axis=1))]
    near, _ = client.request({"op":"plan", "views":[view(center,15)],
                              "budget":1_500_000,"pixel_error":1.5,"cap":65536})
    assert max(n["key"][0] for n in near["nodes"]) > max(n["key"][0] for n in far["nodes"])
    assert near["points"] <= 1_500_000
    bounds = np.array([center-[10,10,15], center+[10,10,15]])
    with tempfile.TemporaryDirectory(prefix="scan-crop-test-") as directory:
        destination = Path(directory)/"crop"
        started = time.monotonic()
        crop, _ = client.request(dict(op="crop", source=str(source), origin=origin.tolist(),
            output=str(destination), bounds=bounds.tolist(), point_limit=1_000_000))
        crop_seconds = time.monotonic()-started
        manifest = crop["manifest"]
        actual = np.fromfile(destination/"positions.f32", '<f4').reshape(-1,3).astype(np.float64)+origin
        ids = np.fromfile(destination/"source_ids.u64", '<u8')
        assert len(ids) == len(np.unique(ids)) == len(actual)
        with laspy.CopcReader.open(source) as independent:
            reference = independent.query(bounds=Bounds(bounds[0],bounds[1]))
            expected = np.column_stack((reference.x,reference.y,reference.z))
            expected = expected[np.all((expected >= bounds[0]) & (expected <= bounds[1]), axis=1)]
            root_entry = next(entry for key, entry in independent.root_page.entries.items() if key.level == 0)
            root_points = independent._fetch_and_decompress_points_of_nodes([root_entry])
            direct = np.column_stack((root_points.x,root_points.y,root_points.z,
                                      root_points.intensity,root_points.classification)).astype(np.float64)
            rust = np.column_stack((positions, np.frombuffer(data, '<u2', count=count, offset=count*15), classes))
            direct[:,:3] = np.rint(direct[:,:3] * 100)
            rust[:,:3] = np.rint(rust[:,:3] * 100)
            order = lambda rows: rows[np.lexsort(tuple(rows[:,i] for i in range(4,-1,-1)))]
            np.testing.assert_array_equal(order(rust), order(direct))
        def canonical(points):
            values = np.rint(points*100).astype(np.int64)
            return values[np.lexsort((values[:,2],values[:,1],values[:,0]))]
        np.testing.assert_array_equal(canonical(actual),canonical(expected))
        assert manifest["full_resolution_crop"] and manifest["stride"] == 1
        angle = np.deg2rad(27)
        rotation = np.array([[np.cos(angle),np.sin(angle),0],[-np.sin(angle),np.cos(angle),0],[0,0,1]])
        linear = np.diag([1/7,1/5,1/15]) @ rotation
        clip = np.eye(4)
        clip[:3,:3] = linear
        clip[:3,3] = -linear @ (center-origin)
        oriented, _ = client.request(dict(op="crop", source=str(source), origin=origin.tolist(),
            output=str(Path(directory)/"rotated"),bounds=bounds.tolist(),
            clip_matrix=clip.tolist(),point_limit=1_000_000))
        rotated = np.fromfile(Path(directory)/"rotated/positions.f32", '<f4').reshape(-1,3).astype(np.float64)+origin
        local = np.einsum('ni,ji->nj', expected-center, linear)
        assert np.isfinite(local).all()
        expected_rotated = expected[np.all(np.abs(local) <= 1+1e-9, axis=1)]
        np.testing.assert_array_equal(canonical(rotated),canonical(expected_rotated))
        try:
            client.request(dict(op="crop", source=str(source), origin=origin.tolist(),
                output=str(Path(directory)/"too_large"),bounds=bounds.tolist(),point_limit=10))
            raise AssertionError("Crop limit did not reject excess points")
        except RuntimeError as exc:
            assert "exceeds" in str(exc)
            assert not (Path(directory)/"too_large").exists()
        seam_sources = [Path("/Users/ali/Desktop/pointclouds/ZH/swisssurface3d_2024_2670-1259_2056_5728.copc.laz"),
                        Path("/Users/ali/Desktop/pointclouds/ZH/swisssurface3d_2024_2669-1259_2056_5728.copc.laz")]
        seam_bounds = np.array([[2669990,1259490,500],[2670010,1259510,900]], dtype=np.float64)
        seam_origin = [2670000,1259000,0]
        left_to_right = np.eye(4)
        left_to_right[0,3] = -1000
        seam_specs = [dict(source=str(seam_sources[0]),origin=seam_origin,bounds=seam_bounds.tolist(),
                           to_output=np.eye(4).tolist()),
                      dict(source=str(seam_sources[1]),origin=[2669000,1259000,0],bounds=seam_bounds.tolist(),
                           to_output=left_to_right.tolist())]
        seam_path = Path(directory)/"seam"
        seam, _ = client.request(dict(op="crop", source=str(seam_sources[0]), origin=seam_origin,
            bounds=seam_bounds.tolist(), source_specs=seam_specs, output=str(seam_path), point_limit=1_000_000))
        seam_points = np.fromfile(seam_path/"positions.f32", '<f4').reshape(-1,3) + seam_origin
        seam_ids = np.fromfile(seam_path/"source_ids.u64", '<u8')
        assert set(np.unique(seam_ids >> 56)) == {0,1}
        seam_reference = []
        for seam_source in seam_sources:
            with laspy.CopcReader.open(seam_source) as independent:
                points = independent.query(bounds=Bounds(seam_bounds[0],seam_bounds[1]))
                xyz = np.column_stack((points.x,points.y,points.z))
                seam_reference.append(xyz[np.all((xyz >= seam_bounds[0]) & (xyz <= seam_bounds[1]), axis=1)])
        np.testing.assert_array_equal(canonical(seam_points), canonical(np.concatenate(seam_reference)))
        picked, _ = client.request(dict(op="pick", pick_sources=[dict(source=str(source),
            ray_origin=[center[0],center[1],center[2]+100],ray_direction=[0,0,-1],max_distance=200)],
            pick_radius=0.05,plane_radius=1.0))
        assert picked["found"] and picked["ray_distance"] < 0.01 and picked["plane_points"] >= 3
        class_view = dict(view([2670500,1253500,410],750), class_filter=6,
                          class_index=str(Path(directory)/"class_index"))
        class_plan, _ = client.request(dict(op="plan", views=[class_view], budget=200_000,
                                            gpu_budget=2_000_000, pixel_error=1.5))
        assert class_plan["nodes"] and (Path(directory)/"class_index/copc_class_counts.json").is_file()
        filtered, filtered_data = client.request(dict(op="node", source=str(source),
            stamp=root_node["stamp"], node=root_node["node"], origin=origin.tolist(), class_filter=6))
        filtered_count = filtered["count"]
        filtered_xyz = np.frombuffer(filtered_data, '<f4', count=filtered_count*3).reshape(-1,3) + origin
        filtered_intensity = np.frombuffer(filtered_data, '<u2', count=filtered_count,
                                           offset=filtered_count*15)
        filtered_classes = np.frombuffer(filtered_data, 'u1', count=filtered_count,
                                         offset=filtered_count*17)
        assert np.all(filtered_classes == 6)
        filtered_rows = np.column_stack((np.rint(filtered_xyz*100), filtered_intensity,
                                         filtered_classes)).astype(np.float64)
        np.testing.assert_array_equal(order(filtered_rows), order(direct[direct[:,4] == 6]))
        result = {"source":str(source),"center":center.tolist(),"bounds":bounds.tolist(),
                  "far_nodes":len(far["nodes"]),"near_nodes":len(near["nodes"]),
                  "far_level":max(n["key"][0] for n in far["nodes"]),
                  "near_level":max(n["key"][0] for n in near["nodes"]),
                  "plan_ms":near["elapsed_ms"],"crop_points":len(actual),"crop_seconds":crop_seconds,
                  "independent_laspy_match":True,"unique_source_ids":True,
                  "rotated_crop_points":len(rotated),"rotated_crop_match":True,
                  "selective_decode_match":True,"cross_tile_points":len(seam_points),
                  "cross_tile_match":True,"pick_plane_points":picked["plane_points"],
                  "class_filter_points":filtered_count,"class_filter_match":True}
        (root/"tests/streaming_data.json").write_text(json.dumps(result,indent=2))
        print("STREAMING_DATA_OK",json.dumps(result))
finally:
    client.close()
