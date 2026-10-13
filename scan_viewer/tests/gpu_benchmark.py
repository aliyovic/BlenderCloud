"""Measure completed Metal point draws, including a framebuffer readback."""

import gc
import json
from pathlib import Path
import statistics
import time

import bpy
import gpu
import numpy as np
from mathutils import Matrix

root = Path(__file__).resolve().parents[1]
bpy.ops.preferences.addon_enable(module="scan_viewer")
import scan_viewer as addon


def run():
    results = []
    offscreen = gpu.types.GPUOffScreen(1920, 1080)
    shader = addon._point_shader()
    ubo = gpu.types.GPUUniformBuf(gpu.types.Buffer('FLOAT', 16, np.eye(4, dtype=np.float32).T.ravel()))
    rng = np.random.default_rng(11)
    try:
        for count in (1, 250_000, 500_000, 750_000, 1_000_000, 2_000_000, 4_000_000, 8_000_000):
            positions = rng.random((count, 3), dtype=np.float32)
            positions *= 2
            positions -= 1
            colors = np.full((count, 4), 255, dtype=np.uint8)
            intensity = np.zeros(count, dtype=np.float32)
            classes = np.zeros(count, dtype=np.float32)
            batch = addon._gpu_batch(positions, colors, intensity, classes)
            del positions, colors, intensity, classes
            samples = []
            with offscreen.bind():
                gpu.state.program_point_size_set(True)
                shader.bind()
                shader.uniform_float('ModelViewProjectionMatrix', Matrix.Identity(4))
                shader.uniform_float('point_size', 2.0)
                shader.uniform_float('intensity_min', 0.0)
                shader.uniform_float('intensity_max', 1.0)
                shader.uniform_int('color_mode', 0)
                shader.uniform_int('class_filter', -1)
                shader.uniform_int('use_section', 0)
                shader.uniform_block('section_data', ubo)
                for iteration in range(8):
                    gpu.state.active_framebuffer_get().clear(color=(0, 0, 0, 1))
                    started = time.perf_counter()
                    batch.draw_range(shader, elem_start=0, elem_count=count)
                    gpu.state.active_framebuffer_get().read_color(0, 0, 1, 1, 4, 0, 'UBYTE')
                    if iteration:
                        samples.append((time.perf_counter() - started) * 1000)
                gpu.state.program_point_size_set(False)
            results.append({"points": count, "median_ms": statistics.median(samples),
                            "min_ms": min(samples), "max_ms": max(samples)})
            del batch
            gc.collect()
        output = {"backend": gpu.platform.backend_type_get(), "resolution": [1920, 1080],
                  "point_size": 2, "measurements": results}
        (root / "tests/gpu_benchmark.json").write_text(json.dumps(output, indent=2))
        print('GPU_BENCHMARK_OK', json.dumps(output), flush=True)
    finally:
        offscreen.free()
        bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(run, first_interval=1.0)
