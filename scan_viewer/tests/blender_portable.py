"""Check installed import, snap sampling, and full-detail reads with generated points.

This check owns the installed Blender operator-to-worker boundary. Pure worker
tests cannot detect a missing bundled binary, failed operator registration, or
an octree result that never reaches the active scan object. It uses the real
operators and runtime, with no substitute worker or altered production globals.
"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time

import addon_utils
import bpy
import numpy as np


def wait_until(finished, tick, *, seconds=30):
    deadline = time.monotonic() + seconds
    while not finished():
        if time.monotonic() >= deadline:
            raise AssertionError("The installed point-cloud operation timed out")
        tick()
        time.sleep(0.01)


def ordered(points):
    return points[np.lexsort((points[:, 2], points[:, 1], points[:, 0]))]


def check_active_import_disable(addon, package, directory):
    """Disable during a real import, then verify the next import can still run."""
    source = directory / "active-import.xyz"
    with source.open("wb") as stream:
        block = b"2689000.125 1254000.875 400.125 123 45 201\n" * 10000
        for _ in range(200):
            stream.write(block)
    jobs = []
    try:
        assert bpy.ops.scan_viewer.import_cloud(filepath=str(source), max_points=1000) == {"FINISHED"}
        jobs = list(addon._jobs.values())
        assert jobs and jobs[0][0].poll() is None, "The import fixture did not leave an active child process"
        bpy.context.scene.scan_streaming = False
        assert all(job[0].poll() is None for job in jobs), "Toggling detail streaming stopped an overview import"
        addon_utils.disable(package, default_set=False)
        assert all(job[0].poll() is not None for job in jobs), "Disabling the extension left an import process alive"
        assert not addon._jobs and not addon._pending, "Disabling the extension retained queued import work"
        assert all(not job[1].exists() for job in jobs), "Disabling the extension retained a partial import cache"
        assert not bpy.app.timers.is_registered(addon._poll_jobs), "Disabling the extension left its import timer registered"
        assert not bpy.app.timers.is_registered(addon._poll_octree_jobs), "Disabling the extension left its octree timer registered"
        assert not hasattr(bpy.types.WindowManager, "scan_viewer_status")
    finally:
        addon._stop_jobs(addon._jobs)
        addon._pending.clear()
        for timer in (addon._poll_jobs, addon._poll_octree_jobs):
            if bpy.app.timers.is_registered(timer):
                bpy.app.timers.unregister(timer)
        for job in jobs:
            shutil.rmtree(job[1], ignore_errors=True)
            shutil.rmtree(job[2], ignore_errors=True)
        addon_utils.enable(package, default_set=False, persistent=True)
        bpy.context.scene.scan_streaming = True


def check_minimum_cpu_delivery(addon, transport, directory, package):
    """Deliver a paged node at the lowest cache setting and from legacy settings."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from native_fixture import large_native_fixture

    folder = directory / "cpu-cache-boundary"
    folder.mkdir()
    source, cache, _, count = large_native_fixture(folder)
    matrix = np.eye(4)
    matrix[:3, :3] *= 0.25
    matrix[:3, 3] = -0.5
    view = dict(source=str(source), cache=str(cache), object="Cache boundary fixture",
                origin=[0, 0, 0], matrix=matrix.tolist(), width=1200, height=900)
    for legacy in (False, True):
        if legacy:
            snapshot = directory / "legacy-64-MiB-cache.blend"
            addon_utils.disable(package, default_set=False)
            assert not hasattr(bpy.types.Scene, "scan_cpu_cache_mb")
            bpy.types.Scene.scan_cpu_cache_mb = bpy.props.IntProperty(default=512, min=64, max=4096)
            try:
                bpy.context.scene.scan_cpu_cache_mb = 64
                assert bpy.ops.wm.save_as_mainfile(filepath=str(snapshot)) == {"FINISHED"}
            finally:
                del bpy.types.Scene.scan_cpu_cache_mb
                addon_utils.enable(package, default_set=False, persistent=True)
            assert bpy.ops.wm.open_mainfile(filepath=str(snapshot)) == {"FINISHED"}
            assert bpy.context.scene.scan_cpu_cache_mb == 64, "Reloading the legacy fixture did not preserve its old setting"
        else:
            bpy.context.scene.scan_cpu_cache_mb = 0
        runtime = addon._ensure_runtime()
        try:
            addon._stream_tick()
            before = runtime.stats()["node_reads"]
            runtime.request_view([view], count, 1.0, count)
            wait_until(lambda: runtime.stats()["node_reads"] > before, addon._stream_tick)
            key = transport.node_key(runtime.plan[0])
            label = "saved 64 MiB settings" if legacy else "lowest supported CPU setting"
            assert key in runtime.cpu, "The complete node was evicted before upload with " + label
            positions, _, _, _ = runtime.attributes(key)
            assert len(positions) == count, "The CPU cache omitted source points with " + label
            np.testing.assert_array_equal(positions[[0, -1]], [[1, 2, 3], [1, 2, 3]])
        finally:
            addon._shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extension-package", default="bl_ext.user_default.scan_viewer")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    for package in ("scan_viewer", "bl_ext.user_default.scan_viewer"):
        if package != args.extension_package and package in bpy.context.preferences.addons:
            addon_utils.disable(package, default_set=False)
    addon_utils.enable(args.extension_package, default_set=False, persistent=True)
    addon = importlib.import_module(args.extension_package)
    from_module = importlib.import_module(args.extension_package + ".streaming")
    cache_paths = set()
    runtime = None
    try:
        with tempfile.TemporaryDirectory(prefix="scan-viewer-blender-") as directory:
            check_active_import_disable(addon, args.extension_package, Path(directory))
            check_minimum_cpu_delivery(addon, from_module, Path(directory), args.extension_package)
            source = (Path(directory) / "known.xyz").resolve()
            xyz = np.array([[2689000.125 + (i % 100) * 0.01,
                             1254000.875 + (i // 100) * 0.01,
                             400.0 + (i % 7) * 0.001] for i in range(2000)])
            source.write_text("".join(f"{x:.6f} {y:.6f} {z:.6f} 123 45 201\n" for x, y, z in xyz))
            bpy.ops.object.select_all(action="SELECT")
            bpy.ops.object.delete(use_global=False)
            assert bpy.ops.scan_viewer.import_cloud(filepath=str(source), max_points=1000) == {"FINISHED"}
            wait_until(lambda: not addon._jobs and not addon._pending, addon._poll_jobs)
            scans = [obj for obj in bpy.context.scene.objects if obj.get("scan_source") == str(source)]
            assert len(scans) == 1, bpy.context.window_manager.scan_viewer_status
            scan = scans[0]
            cache_paths.add(Path(scan["scan_cache"]))
            assert scan["scan_source_points"] == 2000 and scan["scan_retained_points"] == 1000
            assert bpy.ops.scan_viewer.snap_sample(max_vertices=1000) == {"FINISHED"}
            snap = bpy.context.active_object
            world = np.array([tuple(snap.matrix_world @ vertex.co) for vertex in snap.data.vertices])
            world += np.array(bpy.context.scene["scan_scene_origin"])
            np.testing.assert_allclose(ordered(world), ordered(xyz[::2]), rtol=0, atol=5e-5)

            snap.select_set(False)
            scan.select_set(True)
            bpy.context.view_layer.objects.active = scan
            assert bpy.ops.scan_viewer.build_octree() == {"FINISHED"}
            wait_until(lambda: not addon._octree_jobs, addon._poll_octree_jobs)
            assert "scan_octree" in scan, bpy.context.window_manager.scan_viewer_status
            octree = Path(scan["scan_octree"])
            cache_paths.add(octree)
            manifest = json.loads((octree / "manifest.json").read_text())
            assert manifest["retained_points"] == len(xyz)

            runtime = addon._ensure_runtime()
            runtime.request_view([{
                "source": str(source), "cache": str(octree), "object": scan.name,
                "origin": list(scan["scan_origin"]), "width": 1000, "height": 1000,
                "matrix": [[1, 0, 0, -0.6], [0, 4, 0, -3.88],
                           [0, 0, 0.01, -4], [0, 0, 0, 1]],
            }], budget=2000, pixel_error=0.25, gpu_budget=2000)
            def ready():
                return (runtime.plan and not runtime.inflight and
                        all(from_module.node_key(node) in runtime.cpu for node in runtime.plan))
            wait_until(ready, runtime.tick)
            assert not runtime.error, runtime.error
            restored = []
            for node in runtime.plan:
                positions, colors, _, _ = runtime.attributes(from_module.node_key(node))
                restored.append(positions.astype(np.float64) + np.array(node["origin"]))
                np.testing.assert_array_equal(colors[:, :3], np.tile([123, 45, 201], (len(positions), 1)))
            restored = np.concatenate(restored)
            assert len(restored) == len(xyz), "The full-detail plan omitted or repeated source points"
            np.testing.assert_allclose(ordered(restored), ordered(xyz), rtol=0, atol=5e-5)
            print("SCAN_VIEWER_PORTABLE_BLENDER_OK", json.dumps({
                "source_points": len(xyz), "snap_vertices": len(snap.data.vertices),
                "full_detail_points": len(restored), "package": args.extension_package,
                "active_import_disable_and_reenable": "passed",
                "large_node_at_minimum_and_saved_cpu_settings": "passed",
            }), flush=True)
    finally:
        if runtime is not None:
            runtime.close()
        addon_utils.disable(args.extension_package, default_set=False)
        for path in cache_paths:
            shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    main()
