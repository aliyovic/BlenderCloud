"""Check programmable point size and round points on Blender's active GPU backend."""

import bpy
import gpu
import numpy as np
from mathutils import Matrix


def run():
    try:
        info = gpu.types.GPUShaderCreateInfo()
        info.vertex_in(0, 'VEC3', 'pos')
        info.push_constant('MAT4', 'ModelViewProjectionMatrix')
        info.push_constant('FLOAT', 'point_size')
        info.fragment_out(0, 'VEC4', 'FragColor')
        info.vertex_source('void main() { gl_Position = ModelViewProjectionMatrix * vec4(pos, 1.0); gl_PointSize = point_size; }')
        info.fragment_source('void main() { if (length(gl_PointCoord - vec2(0.5)) > 0.5) discard; FragColor = vec4(1.0); }')
        shader = gpu.shader.create_from_info(info)
        fmt = gpu.types.GPUVertFormat()
        fmt.attr_add(id='pos', comp_type='F32', len=3, fetch_mode='FLOAT')
        vbo = gpu.types.GPUVertBuf(len=1, format=fmt)
        vbo.attr_fill(id='pos', data=np.array([[0, 0, 0]], dtype=np.float32))
        batch = gpu.types.GPUBatch(type='POINTS', buf=vbo)
        offscreen = gpu.types.GPUOffScreen(64, 64)
        with offscreen.bind():
            gpu.state.active_framebuffer_get().clear(color=(0, 0, 0, 1))
            gpu.state.program_point_size_set(True)
            shader.bind()
            shader.uniform_float('ModelViewProjectionMatrix', Matrix.Identity(4))
            shader.uniform_float('point_size', 20.0)
            batch.draw(shader)
            pixel = lambda x, y: gpu.state.active_framebuffer_get().read_color(x, y, 1, 1, 4, 0, 'UBYTE').to_list()[0][0]
            assert pixel(32, 32)[0] > 200, pixel(32, 32)
            assert pixel(41, 32)[0] > 200, pixel(41, 32)
            assert pixel(41, 41)[0] < 20, pixel(41, 41)
        gpu.state.program_point_size_set(False)
        offscreen.free()
        print('GPU_POINT_SIZE_OK', flush=True)
    except Exception as exc:
        print('GPU_POINT_SIZE_ERROR', repr(exc), flush=True)
        raise
    finally:
        bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(run, first_interval=1.0)
