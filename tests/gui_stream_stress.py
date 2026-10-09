"""Move through all real COPC tiles with small caches in the user's Blender profile."""

import json
from pathlib import Path
import time
import traceback

import bpy
from mathutils import Quaternion, Vector

root = Path(__file__).resolve().parents[1]
bpy.ops.preferences.addon_enable(module="scan_viewer")
import scan_viewer as addon
from scan_viewer.streaming import node_key

started = time.monotonic()
stage_started = started
area = None
index = 0
rapid = 0
scans = sorted((obj for obj in bpy.data.objects if addon._is_stream_source(obj)), key=lambda obj:obj.name)
records = []
state = "rapid"


def position(obj):
    center = Vector([(a+b)/2-o for a,b,o in zip(obj["scan_bounds_min"],obj["scan_bounds_max"],obj["scan_origin"])])
    view = area.spaces.active.region_3d
    view.view_location = obj.matrix_world @ center
    view.view_distance = 90
    view.view_rotation = Quaternion((1,0,0,0))
    view.view_perspective = 'ORTHO'
    area.spaces.active.clip_start = 0.05
    obj.scan_color_mode = ('CLASS','INTENSITY','RGB')[index % 3]
    addon._redraw()


def ready():
    runtime = addon._runtime
    return (runtime and runtime.plan and not runtime.inflight and not runtime.plan_future
        and runtime.planned_generation == runtime.generation
        and all(node_key(node) in runtime.gpu
                and time.monotonic() - runtime.gpu[node_key(node)][2] >= 0.15
                for node in runtime.plan))


def tick():
    global area, index, rapid, state, stage_started
    try:
        if time.monotonic()-started > 150:
            raise RuntimeError(f"Stress test timed out at {index}: {addon._runtime.stats()}")
        if area is None:
            area = max((a for a in bpy.context.screen.areas if a.type == 'VIEW_3D'), key=lambda a:a.width*a.height)
            bpy.context.scene.scan_cpu_cache_mb = 64
            bpy.context.scene.scan_gpu_cache_mb = 64
            bpy.context.scene.scan_point_budget = 6_000_000
        runtime = addon._runtime
        if runtime:
            assert not runtime.error, runtime.error
            assert runtime.cpu_bytes <= 64*1024*1024
            total_gpu = runtime.gpu_bytes + sum(saved[2]*24 for saved in addon._batches.values())
            assert total_gpu <= 64*1024*1024, total_gpu
            assert addon._draw_stats["points"] <= bpy.context.scene.scan_point_budget
        if state == "rapid":
            position(scans[rapid % len(scans)])
            rapid += 1
            if rapid >= 30:
                state = "travel"
                position(scans[0])
                stage_started = time.monotonic()
            return 0.03
        if state == "travel" and time.monotonic()-stage_started > 0.3 and ready():
            records.append(dict(runtime.stats(), tile=scans[index % len(scans)].name,
                                seconds=time.monotonic()-stage_started, mode=scans[index % len(scans)].scan_color_mode,
                                total_gpu_bytes=total_gpu, **addon._draw_stats))
            print("STRESS_TILE",index,records[-1]["seconds"],flush=True)
            index += 1
            if index < len(scans)+1:
                position(scans[index % len(scans)])
                stage_started = time.monotonic()
                return 0.05
            assert runtime.metrics["cpu_evictions"] > 0
            assert runtime.metrics["gpu_evictions"] > 0
            processes = [client.process for client in runtime.clients]
            result = {"tiles":len(scans),"visits":records,"rapid_camera_changes":rapid,
                      "enabled_addons":list(bpy.context.preferences.addons.keys()),
                      "seconds":time.monotonic()-started}
            addon._shutdown()
            assert all(process.poll() is not None for process in processes)
            result["worker_processes_stopped"] = True
            (root/"tests/gui_stream_stress.json").write_text(json.dumps(result,indent=2))
            print("GUI_STRESS_OK",len(records),flush=True)
            bpy.ops.wm.quit_blender()
            return None
        return 0.05
    except Exception:
        error = traceback.format_exc()
        (root/"tests/gui_stream_stress_error.txt").write_text(error)
        print(error,flush=True)
        addon._shutdown()
        bpy.ops.wm.quit_blender()
        return None


bpy.app.timers.register(tick,first_interval=1.0)
