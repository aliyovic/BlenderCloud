"""Build and draw a native octree cache in a real Blender viewport."""

from pathlib import Path
import time
import traceback

import bpy

root = Path(__file__).resolve().parents[1]
bpy.ops.preferences.addon_enable(module="scan_viewer")
import scan_viewer as addon
from scan_viewer.streaming import node_key

for obj in tuple(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
obj = addon._load_object(root / "cache/cras_binary")
source = Path(obj["scan_source"]).resolve()
obj["scan_source"] = str(source)
started = time.monotonic()
state = "build"


def tick():
    global state
    try:
        if time.monotonic() - started > 30:
            raise RuntimeError(f"Native octree timed out in {state}")
        if state == "build":
            assert bpy.ops.scan_viewer.build_octree() == {'FINISHED'}
            state = "wait_build"
            return 0.1
        if state == "wait_build" and not addon._octree_jobs:
            assert addon._is_stream_source(obj)
            addon._redraw()
            state = "wait_draw"
            return 0.1
        if state == "wait_draw":
            runtime = addon._runtime
            if runtime and runtime.plan and not runtime.inflight and all(
                    node_key(node) in runtime.gpu for node in runtime.plan):
                assert sum(node["source_count"] for node in runtime.plan) == 10_000
                area = next(area for area in bpy.context.screen.areas if area.type == 'VIEW_3D')
                region = next(region for region in area.regions if region.type == 'WINDOW')
                with bpy.context.temp_override(area=area, region=region):
                    assert bpy.ops.scan_viewer.modeling_box() == {'FINISHED'}
                    assert bpy.ops.scan_viewer.modeling_crop() == {'FINISHED'}
                state = "wait_crop"
                return 0.1
        if state == "wait_crop" and not addon._modeling_jobs:
            crop = bpy.context.active_object
            assert crop.get("scan_full_resolution_crop") and crop.get("scan_cache") == "embedded"
            assert len(crop.data.vertices) > 0
            print("GUI_NATIVE_OCTREE_OK", addon._runtime.stats(), len(crop.data.vertices), flush=True)
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
