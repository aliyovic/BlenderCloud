"""Build and draw a native octree cache in a real Blender viewport."""

from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import time
import traceback

import bpy

root = Path(__file__).resolve().parents[1]
bpy.ops.preferences.addon_enable(module="scan_viewer")
import scan_viewer as addon
from scan_viewer.streaming import node_key

for obj in tuple(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
fixture = tempfile.TemporaryDirectory(prefix="scan-viewer-native-")
source = Path(fixture.name) / "scan.ply"
shutil.copy2(root / "tests/cras_binary_10000.ply", source)
assert bpy.ops.scan_viewer.import_cloud(filepath=str(source), max_points=1000) == {'FINISHED'}
obj = None
started = time.monotonic()
state = "wait_import"
checking_full_overview = False


def check_build_cleanup():
    for name, stop in (("shutdown", addon._shutdown),
                       ("load", lambda: bpy.ops.wm.read_factory_settings(use_empty=True))):
        interrupted_source = Path(fixture.name) / (name + ".xyz")
        interrupted_source.write_text("0 0 0 100 120 140\n" * 250_000)
        preview = Path(fixture.name) / (name + "-overview")
        subprocess.run([str(addon._binary()), str(interrupted_source), str(preview),
                        "--max-points", "1000"], check=True, capture_output=True)
        addon._load_object(preview, frame=False)
        assert bpy.ops.scan_viewer.build_octree() == {'FINISHED'}
        job = next(iter(addon._octree_jobs.values()))
        proc, temp = job[:2]
        deadline = time.monotonic() + 2
        while not temp.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.002)
        assert temp.is_dir(), "Builder did not create its temporary cache"
        stop()
        assert proc.poll() is not None, "Builder survived shutdown"
        assert not temp.exists(), "Interrupted cache survived shutdown"
        assert not addon._octree_jobs
    print("GUI_BUILD_CLEANUP_OK", flush=True)


def tick():
    global state, obj, checking_full_overview
    try:
        if time.monotonic() - started > 30:
            raise RuntimeError(f"Native octree timed out in {state}")
        if state == "wait_import":
            scans = [item for item in bpy.context.scene.objects if "scan_cache" in item]
            if scans:
                obj = scans[0]
                assert obj["scan_source_points"] == 10_000
                assert obj["scan_retained_points"] == (10_000 if checking_full_overview else 1000)
                if checking_full_overview:
                    obj.scan_display_count = 1000
                state = "wait_build"
        if state == "wait_build" and addon._is_stream_source(obj):
            manifest = json.loads((Path(obj["scan_octree"]) / "manifest.json").read_text())
            assert manifest["retained_points"] == 10_000
            if checking_full_overview:
                print("GUI_FULL_OVERVIEW_ZOOM_OK", flush=True)
                check_build_cleanup()
                fixture.cleanup()
                def finish():
                    bpy.ops.wm.quit_blender()
                    return None
                bpy.app.timers.register(finish, first_interval=1.0)
                return None
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
            checking_full_overview = True
            full_source = Path(fixture.name) / "full.ply"
            shutil.copy2(source, full_source)
            bpy.data.objects.remove(crop, do_unlink=True)
            bpy.data.objects.remove(obj, do_unlink=True)
            assert bpy.ops.scan_viewer.import_cloud(filepath=str(full_source), max_points=10_000) == {'FINISHED'}
            state = "wait_import"
            return 0.1
        return 0.1
    except Exception:
        print(traceback.format_exc(), flush=True)
        addon._before_load(None)
        fixture.cleanup()
        os._exit(1)


bpy.app.timers.register(tick, first_interval=1.0)
