"""Check class filtering through the Blender plan, worker, and GPU caches."""

import json
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
target = next(obj for obj in bpy.data.objects if obj.get("scan_source") == reference["source"])
area = None
region = None
started = time.monotonic()
state = "overview"
result = {}


def ready(expected):
    runtime = addon._runtime
    return (runtime and runtime.plan and not runtime.inflight and not runtime.plan_future
            and runtime.planned_generation == runtime.generation
            and all(node["class_filter"] == expected and node_key(node) in runtime.gpu
                    for node in runtime.plan))


def tick():
    global state, area, region
    try:
        if time.monotonic() - started > 20:
            runtime = addon._runtime
            raise RuntimeError(f"Class filter timed out in {state}: "
                f"{runtime.stats() if runtime else 'no runtime'}, "
                f"hidden={target.hide_get()}, classes={[node['class_filter'] for node in runtime.plan[:3]] if runtime else []}")
        if area is None:
            area = max((area for area in bpy.context.screen.areas if area.type == 'VIEW_3D'),
                       key=lambda area: area.width * area.height)
            region = area.spaces.active.region_3d
            addon._frame_all(bpy.context)
            addon._redraw()
            return 0.1
        if state == "overview" and ready(-1):
            for obj in bpy.data.objects:
                if obj != target and obj.get("scan_source"):
                    obj.hide_set(True)
            target.hide_set(False)
            region.view_location = target.matrix_world @ Vector(np.asarray(reference["center"]) - target["scan_origin"])
            region.view_distance = 38.0
            region.view_rotation = Quaternion((1, 0, 0, 0))
            region.view_perspective = 'ORTHO'
            area.spaces.active.clip_start = 0.05
            addon._redraw()
            state = "all"
            return 0.1
        if state == "all" and ready(-1):
            result["all_nodes"] = len(addon._runtime.plan)
            target.scan_class_filter = 6
            addon._redraw()
            state = "filtered"
            return 0.1
        if state == "filtered" and ready(6):
            runtime = addon._runtime
            classes = []
            for node in runtime.plan:
                key = node_key(node)
                if key in runtime.cpu:
                    classes.append(runtime.attributes(key)[3])
            assert classes and all(np.all(values == 6) for values in classes)
            result.update(filtered_nodes=len(runtime.plan), checked_buffers=len(classes),
                          seconds=time.monotonic() - started)
            assert result["filtered_nodes"] > 0
            assert not runtime.error, runtime.error
            (root / "tests/gui_class_filter.json").write_text(json.dumps(result, indent=2))
            print("GUI_CLASS_FILTER_OK", result, flush=True)
            addon._shutdown()
            bpy.ops.wm.quit_blender()
            return None
        return 0.1
    except Exception:
        print(traceback.format_exc(), flush=True)
        addon._shutdown()
        bpy.ops.wm.quit_blender()
        return None


bpy.app.timers.register(tick, first_interval=1.0)
