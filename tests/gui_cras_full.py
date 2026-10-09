"""Benchmark full CRAS terrestrial scan streaming in Blender."""

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

for obj in tuple(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
source = (root / "../CRAS_Lab/craslabannotated.zip").resolve()
obj = addon._load_object(root / "cache/cras_full_preview")
obj["scan_source"] = str(source)
obj["scan_octree"] = str(addon._octree_path(source))
assert (Path(obj["scan_octree"]) / "manifest.json").is_file()
bpy.context.scene.scan_point_budget = 2_000_000
started = time.monotonic()
stage_started = started
stage = "overview"
result = {"source": str(source), "octree": obj["scan_octree"]}


def ready():
    runtime = addon._runtime
    return (runtime and runtime.plan and not runtime.inflight and not runtime.plan_future
            and runtime.planned_generation == runtime.generation
            and all(node_key(node) in runtime.gpu
                    and time.monotonic() - runtime.gpu[node_key(node)][2] >= 0.15
                    for node in runtime.plan))


def tick():
    global stage, stage_started
    try:
        if time.monotonic() - started > 120:
            raise RuntimeError(f"CRAS full benchmark timed out at {stage}: {addon._runtime.stats()}")
        area = max((area for area in bpy.context.screen.areas if area.type == 'VIEW_3D'),
                   key=lambda area: area.width * area.height)
        region = area.spaces.active.region_3d
        if stage == "overview" and ready():
            result["overview"] = dict(addon._runtime.stats(), **addon._draw_stats)
            result["overview_seconds"] = time.monotonic() - stage_started
            center = Vector((6.5185, 15.7385, 1.5075))
            region.view_location = obj.matrix_world @ center
            region.view_distance = 5.0
            region.view_rotation = Quaternion((1, 0, 0, 0))
            region.view_perspective = 'ORTHO'
            area.spaces.active.clip_start = 0.01
            area.spaces.active.clip_end = 1000
            addon._redraw()
            stage = "near"
            stage_started = time.monotonic()
            return 0.05
        if stage == "near" and time.monotonic() - stage_started > 0.3 and ready():
            result["near"] = dict(addon._runtime.stats(), **addon._draw_stats)
            result["near_seconds"] = time.monotonic() - stage_started
            assert result["near"]["stream_points"] > 0
            bpy.ops.screen.screenshot(filepath=str(root / "tests/gui_cras_full.png"))
            (root / "tests/gui_cras_full.json").write_text(json.dumps(result, indent=2))
            print("GUI_CRAS_FULL_OK", json.dumps(result), flush=True)
            addon._shutdown()
            bpy.ops.wm.quit_blender()
            return None
        return 0.05
    except Exception:
        print(traceback.format_exc(), flush=True)
        addon._shutdown()
        bpy.ops.wm.quit_blender()
        return None


bpy.app.timers.register(tick, first_interval=1.0)
