"""Check the point shader against Blender viewport clip planes on Metal."""

import traceback

import bpy
import gpu
import numpy as np
from mathutils import Matrix

bpy.ops.preferences.addon_enable(module="scan_viewer")
import scan_viewer as addon


def run():
    offscreen = None
    try:
        area = next(area for area in bpy.context.screen.areas if area.type == 'VIEW_3D')
        region = next(region for region in area.regions if region.type == 'WINDOW')
        rv = area.spaces.active.region_3d
        rv.use_clip_planes = True
        rv.clip_planes = ((1.0, 0.0, 0.0, 0.0),) + ((0.0, 0.0, 0.0, 0.0),) * 5
        obj = bpy.data.objects.new("clip probe", None)
        bpy.context.scene.collection.objects.link(obj)
        obj.scan_point_size = 10.0
        batch = addon._gpu_batch(np.array([[-0.5, 0, 0], [0.5, 0, 0]], dtype=np.float32),
                                 np.array([[255, 0, 0, 255]] * 2, dtype=np.uint8),
                                 np.ones(2, dtype=np.float32), np.zeros(2, dtype=np.float32))
        addon._shader = addon._point_shader()
        addon._section_buffer = gpu.types.GPUUniformBuf(gpu.types.Buffer('FLOAT', 56, [0.0] * 56))
        offscreen = gpu.types.GPUOffScreen(64, 64)
        with bpy.context.temp_override(area=area, region=region):
            with offscreen.bind():
                gpu.state.active_framebuffer_get().clear(color=(0, 0, 0, 1))
                gpu.matrix.push()
                gpu.matrix.push_projection()
                gpu.matrix.load_matrix(Matrix.Identity(4))
                gpu.matrix.load_projection_matrix(Matrix.Identity(4))
                gpu.state.program_point_size_set(True)
                addon._draw_batch(batch, obj, 2)
                pixel = lambda x: gpu.state.active_framebuffer_get().read_color(
                    x, 32, 1, 1, 4, 0, 'UBYTE').to_list()[0][0][0]
                left, right = pixel(16), pixel(48)
                assert left < 20 and right > 200, (left, right, list(rv.clip_planes))
                gpu.matrix.pop_projection()
                gpu.matrix.pop()
        print("GUI_CLIP_PROBE_OK", left, right, flush=True)
    except Exception:
        print(traceback.format_exc(), flush=True)
    finally:
        if offscreen:
            offscreen.free()
        addon._shutdown()
        bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(run, first_interval=1.0)
