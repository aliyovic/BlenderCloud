"""Check point shape and source RGB through the add-on's GPU draw path."""

import json
import os
from pathlib import Path
import traceback

import bpy
import gpu
import numpy as np
from mathutils import Matrix


def run():
    result = {}
    try:
        bpy.ops.preferences.addon_enable(module="scan_viewer")
        import scan_viewer as addon

        obj = bpy.data.objects.new("RGB probe", None)
        bpy.context.collection.objects.link(obj)
        assert obj.scan_point_size == 1.0, obj.scan_point_size
        addon._shader = addon._point_shader()
        addon._section_buffer = gpu.types.GPUUniformBuf(
            gpu.types.Buffer('FLOAT', 56, [0.0] * 56))
        rgb = [40, 100, 160]
        batch = addon._gpu_batch(np.array([[0, 0, 0]], dtype=np.float32),
                                np.array([rgb + [255]], dtype=np.uint8),
                                np.zeros(1, dtype=np.float32), np.zeros(1, dtype=np.float32))
        area = next(area for area in bpy.context.screen.areas if area.type == 'VIEW_3D')
        region = next(region for region in area.regions if region.type == 'WINDOW')
        gpu.state.program_point_size_set(True)
        gpu.state.depth_test_set('NONE')
        gpu.state.blend_set('NONE')
        with bpy.context.temp_override(area=area, region=region):
            with gpu.matrix.push_pop():
                gpu.matrix.load_matrix(Matrix.Identity(4))
                gpu.matrix.load_projection_matrix(Matrix.Identity(4))
                for format in ('RGBA8', 'SRGB8_A8'):
                    texture = gpu.types.GPUTexture((64, 64), format=format)
                    framebuffer = gpu.types.GPUFrameBuffer(color_slots=texture)
                    with framebuffer.bind():
                        gpu.state.viewport_set(0, 0, 64, 64)
                        framebuffer.clear(color=(0, 0, 0, 1))
                        obj.scan_point_size = 12.0
                        addon._draw_batch(batch, obj, 1)
                        pixel = lambda x, y: framebuffer.read_color(
                            x, y, 1, 1, 4, 0, 'UBYTE').to_list()[0][0]
                        center = pixel(32, 32)[:3]
                        result[format] = center
                        assert max(abs(a-b) for a, b in zip(center, rgb)) <= 1, (format, center, rgb)
                        assert pixel(37, 32)[0] > 20, pixel(37, 32)
                        assert pixel(37, 37)[0] == 0, pixel(37, 37)
        result['ok'] = True
        print('GPU_POINT_RGB_OK', result, flush=True)
    except Exception:
        result['ok'] = False
        result['error'] = traceback.format_exc()
        print(result['error'], flush=True)
    finally:
        gpu.state.program_point_size_set(False)
        (Path(__file__).parent / "gpu_probe_result.json").write_text(json.dumps(result, indent=2))
        if not result.get('ok'):
            os._exit(1)
        bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(run, first_interval=1.0)
