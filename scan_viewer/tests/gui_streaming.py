"""Exercise COPC refinement, camera motion, and source crops in a real Blender window."""

import json
import math
from pathlib import Path
import time
import traceback

import bpy
import numpy as np
from mathutils import Quaternion, Vector

root = Path(__file__).resolve().parents[1]
bpy.ops.preferences.addon_enable(module="scan_viewer")
import scan_viewer as addon
from scan_viewer.streaming import node_key

reference = json.loads((root / "tests/streaming_data.json").read_text())
result = {"enabled_addons":list(bpy.context.preferences.addons.keys())}
state = "overview"
started = time.monotonic()
stage_started = started
frames = []
draw_samples = []
benchmark = False
motion_step = 0
area = None
view = None


def mark_frame():
    if benchmark and bpy.context.area == area:
        frames.append(time.monotonic())
        draw_samples.append(dict(addon._draw_stats))


handler = bpy.types.SpaceView3D.draw_handler_add(mark_frame, (), 'WINDOW', 'POST_PIXEL')


def ready():
    runtime = addon._runtime
    if runtime is None or not runtime.plan or runtime.inflight or runtime.plan_future:
        return False
    if runtime.planned_generation != runtime.generation:
        return False
    return all(node_key(node) in runtime.gpu
               and time.monotonic() - runtime.gpu[node_key(node)][2] >= 0.15
               for node in runtime.plan)


def tick():
    global state, stage_started, benchmark, motion_step, area, view
    try:
        if time.monotonic() - started > 90:
            raise RuntimeError(f"Streaming GUI timed out in {state}: {addon._runtime.stats() if addon._runtime else 'no runtime'}")
        if area is None:
            area = max((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), key=lambda a:a.width*a.height)
            view = area.spaces.active.region_3d
            addon._frame_all(bpy.context)
            addon._redraw()
            return 0.1
        if state == "overview" and ready():
            result["overview"] = dict(addon._runtime.stats(), **addon._draw_stats)
            assert addon._draw_stats["points"] <= bpy.context.scene.scan_point_budget
            bpy.ops.screen.screenshot(filepath=str(root / "tests/gui_stream_overview.png"))
            target = next(o for o in bpy.data.objects if o.get("scan_source") == reference["source"])
            for selected in bpy.context.selected_objects:
                selected.select_set(False)
            target.select_set(True)
            bpy.context.view_layer.objects.active = target
            center = target.matrix_world @ Vector(np.asarray(reference["center"]) - target["scan_origin"])
            view.view_location = center
            view.view_distance = 38.0
            view.view_rotation = Quaternion((1.0,0.0,0.0,0.0))
            view.view_perspective = 'ORTHO'
            area.spaces.active.clip_start = 0.05
            addon._redraw()
            state = "refine"
            stage_started = time.monotonic()
            return 0.1
        if state == "refine" and time.monotonic()-stage_started > 0.3 and ready():
            result["near"] = dict(addon._runtime.stats(), **addon._draw_stats)
            result["refinement_seconds"] = time.monotonic()-stage_started
            assert result["near"]["max_level"] > result["overview"]["max_level"]
            assert result["near"]["points"] <= bpy.context.scene.scan_point_budget
            bpy.ops.screen.screenshot(filepath=str(root / "tests/gui_stream_building.png"))
            benchmark = True
            state = "motion"
            return 0.016
        if state == "motion":
            view.view_rotation = Quaternion((0,0,1), math.sin(motion_step/40)*0.6)
            area.tag_redraw()
            motion_step += 1
            if motion_step >= 120:
                benchmark = False
                intervals = np.diff(frames)*1000
                result["camera_motion"] = {"frames":len(frames),
                    "median_frame_ms":float(np.median(intervals)),
                    "p95_frame_ms":float(np.quantile(intervals,.95)),
                    "median_draw_submission_ms":float(np.median([x["draw_ms"] for x in draw_samples])),
                    "max_drawn_points":max(x["points"] for x in draw_samples)}
                assert result["camera_motion"]["max_drawn_points"] <= bpy.context.scene.scan_point_budget
                view.view_rotation = Quaternion((1,0,0,0))
                state = "crop"
            return 0.016
        if state == "crop":
            bpy.context.scene.scan_crop_size = (20,20,30)
            region = next(r for r in area.regions if r.type == 'WINDOW')
            with bpy.context.temp_override(area=area, region=region):
                assert bpy.ops.scan_viewer.modeling_box() == {'FINISHED'}
                assert bpy.ops.scan_viewer.modeling_crop() == {'FINISHED'}
            state = "crop_wait"
            stage_started = time.monotonic()
            return 0.1
        if state == "crop_wait" and not addon._modeling_jobs:
            crop = bpy.context.active_object
            assert crop.get("scan_full_resolution_crop"), bpy.context.window_manager.scan_viewer_status
            result["crop"] = {"points":crop["scan_retained_points"],"seconds":time.monotonic()-stage_started,
                              "cache":crop["scan_cache"]}
            assert result["crop"]["points"] > 8000
            assert bpy.ops.scan_viewer.snap_sample(max_vertices=50000) == {'FINISHED'}
            mesh = bpy.context.active_object
            assert len(mesh.data.vertices) == crop["scan_retained_points"]
            assert mesh.data.attributes.get("scan_node_id") and mesh.data.attributes.get("scan_point_index")
            result["crop"]["snap_vertices"] = len(mesh.data.vertices)
            result["crop"]["source_ids_preserved"] = True
            addon._frame_scan(crop)
            addon._redraw()
            state = "finish"
            stage_started = time.monotonic()
            return 0.1
        if state == "finish" and time.monotonic()-stage_started > 1:
            bpy.ops.screen.screenshot(filepath=str(root / "tests/gui_modeling_crop.png"))
            bpy.ops.wm.save_as_mainfile(filepath=str(root / "examples/copc_modeling_crop.blend"))
            result["final_memory"] = addon._runtime.stats()
            assert result["final_memory"]["cpu_bytes"] <= addon._runtime.cpu_limit
            assert result["final_memory"]["gpu_bytes"] <= addon._runtime.gpu_limit
            (root / "tests/gui_streaming.json").write_text(json.dumps(result,indent=2))
            print("GUI_STREAMING_OK",json.dumps(result),flush=True)
            bpy.types.SpaceView3D.draw_handler_remove(handler,'WINDOW')
            addon._shutdown()
            bpy.ops.wm.quit_blender()
            return None
        return 0.1
    except Exception:
        error = traceback.format_exc()
        (root / "tests/gui_streaming_error.txt").write_text(error)
        print(error,flush=True)
        addon._shutdown()
        bpy.ops.wm.quit_blender()
        return None


bpy.app.timers.register(tick, first_interval=1.0)
