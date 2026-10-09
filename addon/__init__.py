bl_info = {
    "name": "Scan Viewer",
    "author": "Codex",
    "version": (0, 3, 0),
    "blender": (4, 3, 0),
    "location": "3D View > Sidebar > Scan",
    "description": "Stream COPC detail and crop source points for modeling",
    "category": "3D View",
}

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
import zipfile
import atexit
from collections import OrderedDict, deque

import bpy
import gpu
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, PointerProperty, StringProperty
from bpy.app.handlers import persistent
from bpy_extras.io_utils import ImportHelper
from bpy_extras import view3d_utils
from mathutils import Matrix, Vector

from .streaming import StreamRuntime, node_key


_handler = None
_shader = None
_section_buffer = None
_batches = OrderedDict()
_jobs = {}
_octree_jobs = {}
_pending = []
_bulk_frame = False
_error_once = set()
_manifests = {}
_runtime = None
_modeling_jobs = {}
_pick_jobs = {}
_repair_attempted = set()
_draw_stats = {"points": 0, "stream_points": 0, "upload_ms": 0.0, "draw_ms": 0.0}
_motion_intervals = deque(maxlen=30)
_last_draw_at = None


def _binary():
    name = "scan-cache.exe" if sys.platform == "win32" else "scan-cache"
    development = Path(__file__).resolve().parent.parent / "target/release" / name
    if development.is_file() and os.access(development, os.X_OK):
        return development
    bundled = Path(__file__).resolve().parent / "bin" / name
    if bundled.is_file():
        digest = hashlib.sha256(bundled.read_bytes()).hexdigest()[:16]
        executable = _cache_root() / "bin" / f"scan-cache-{digest}{'.exe' if sys.platform == 'win32' else ''}"
        executable.parent.mkdir(parents=True, exist_ok=True)
        if not executable.is_file():
            shutil.copy2(bundled, executable)
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        return executable
    found = shutil.which(name)
    if found:
        return Path(found)
    raise RuntimeError("scan-cache is missing. Build it with cargo build --release in scan_viewer.")


def _cache_root():
    if sys.platform == "darwin":
        base = Path.home() / "Library/Caches"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    path = base / "scan_viewer"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(source, limit):
    source = Path(source).resolve()
    stat = source.stat()
    key = f"{source}:{stat.st_size}:{stat.st_mtime_ns}:{limit}:v3".encode()
    return _cache_root() / hashlib.sha256(key).hexdigest()[:24]


def _octree_path(source):
    source = Path(source).resolve()
    info = source.stat()
    key = f"{source}:{info.st_size}:{info.st_mtime_ns}:octree-v1".encode()
    return _cache_root() / ("octree-" + hashlib.sha256(key).hexdigest()[:24])


def _manifest(path):
    path = str(Path(path).resolve())
    if path in _manifests:
        return _manifests[path]
    with (Path(path) / "manifest.json").open() as stream:
        m = json.load(stream)
    if m.get("version") != 1:
        raise ValueError("Unsupported scan cache version")
    count = m["retained_points"]
    for name, width in (("positions.f32", 12), ("colors.u8", 3),
                        ("intensity.f32", 4), ("classification.u8", 1)):
        if (Path(path) / name).stat().st_size != count * width:
            raise ValueError(f"Incomplete scan cache: {name}")
    _manifests[path] = m
    return m


def _load_object(path, context=None, frame=True):
    m = _manifest(path)
    scene = (context or bpy.context).scene
    obj = bpy.data.objects.new(Path(m["source"]).stem, None)
    obj.empty_display_type = 'PLAIN_AXES'
    obj.empty_display_size = 0.5
    obj["scan_cache"] = str(Path(path).resolve())
    obj["scan_source"] = m["source"]
    obj["scan_source_points"] = m["source_points"]
    obj["scan_retained_points"] = m["retained_points"]
    obj["scan_origin"] = m["origin"]
    obj["scan_bounds_min"] = m["bounds_min"]
    obj["scan_bounds_max"] = m["bounds_max"]
    obj["scan_full_resolution_crop"] = bool(m.get("full_resolution_crop"))
    collection = (context or bpy.context).collection or bpy.context.scene.collection
    collection.objects.link(obj)
    origin = m["origin"]
    if any(abs(value) > 100_000 for value in origin[:2]):
        if "scan_scene_origin" not in scene:
            scene["scan_scene_origin"] = origin
        obj.location = [value - anchor for value, anchor in zip(origin, scene["scan_scene_origin"])]
    (context or bpy.context).view_layer.update()
    obj.scan_display_count = min(m["retained_points"], 1_000_000)
    if not m["has_rgb"]:
        obj.scan_color_mode = 'CLASS' if m["has_classification"] else 'INTENSITY'
    for selected in (context or bpy.context).selected_objects:
        selected.select_set(False)
    obj.select_set(True)
    (context or bpy.context).view_layer.objects.active = obj
    if frame:
        _frame_scan(obj)
    _redraw()
    return obj


def _embed_crop(obj, path):
    path = Path(path)
    manifest = _manifest(path)
    count = manifest["retained_points"]
    mesh = bpy.data.meshes.new(obj.name + " points")
    mesh.vertices.add(count)
    positions = np.memmap(path / "positions.f32", dtype='<f4', mode='r', shape=(count, 3))
    mesh.vertices.foreach_set("co", positions.ravel())
    rgb = np.memmap(path / "colors.u8", dtype=np.uint8, mode='r', shape=(count, 3))
    rgba = np.ones((count, 4), dtype=np.float32)
    rgba[:, :3] = rgb / 255.0
    mesh.attributes.new("scan_rgb", 'BYTE_COLOR', 'POINT').data.foreach_set("color", rgba.ravel())
    values = np.memmap(path / "intensity.f32", dtype='<f4', mode='r', shape=count)
    mesh.attributes.new("scan_intensity", 'FLOAT', 'POINT').data.foreach_set("value", values)
    classes = np.memmap(path / "classification.u8", dtype=np.uint8, mode='r', shape=count)
    mesh.attributes.new("scan_class", 'INT', 'POINT').data.foreach_set("value", classes.astype(np.int32))
    ids = np.memmap(path / "source_ids.u64", dtype='<u8', mode='r', shape=count)
    for name, part in (("scan_source_index", (ids >> 56).astype(np.int32)),
                       ("scan_node_id", ((ids >> 32) & 0x00ffffff).astype(np.int32)),
                       ("scan_point_index", (ids & 0xffffffff).astype(np.int32))):
        mesh.attributes.new(name, 'INT', 'POINT').data.foreach_set("value", part)
    mesh.update()
    embedded = bpy.data.objects.new(obj.name + " embedded", mesh)
    for collection in obj.users_collection:
        collection.objects.link(embedded)
    for name in obj.keys():
        embedded[name] = obj[name]
    embedded.matrix_world = obj.matrix_world.copy()
    embedded.scan_display_count = obj.scan_display_count
    embedded.scan_point_size = obj.scan_point_size
    embedded.scan_color_mode = obj.scan_color_mode
    embedded.scan_class_filter = obj.scan_class_filter
    embedded["scan_source_stamp"] = manifest["source_stamp"]
    embedded["scan_source_nodes"] = json.dumps(manifest["source_nodes"])
    embedded["scan_source_sources"] = json.dumps(manifest["source_sources"])
    embedded["scan_cache"] = "embedded"
    _batches.pop(obj.as_pointer(), None)
    name = obj.name
    obj.select_set(False)
    bpy.data.objects.remove(obj, do_unlink=True)
    embedded.name = name
    embedded.select_set(True)
    bpy.context.view_layer.objects.active = embedded
    _manifests.pop(str(path.resolve()), None)
    del positions, rgb, rgba, values, classes, ids
    shutil.rmtree(path)
    return embedded


def _frame_scan(obj):
    origin = obj["scan_origin"]
    low = obj["scan_bounds_min"]
    high = obj["scan_bounds_max"]
    center = Vector([(a + b) * 0.5 - o for a, b, o in zip(low, high, origin)])
    center = obj.matrix_world @ center
    extent = max(a - b for a, b in zip(high, low))
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                area.spaces.active.clip_end = max(area.spaces.active.clip_end, extent * 20.0)
                area.spaces.active.clip_start = max(area.spaces.active.clip_start, min(1.0, extent / 10000.0))
                region = area.spaces.active.region_3d
                region.view_location = center
                region.view_distance = max(2.0, extent * 1.7)


def _redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _on_redraw(self, context):
    _redraw()


def _on_setting(self, context):
    _batches.pop(self.as_pointer(), None)
    _redraw()


def _on_budget(self, context):
    _batches.clear()
    _redraw()


def _point_shader():
    info = gpu.types.GPUShaderCreateInfo()
    info.vertex_in(0, 'VEC3', 'pos')
    info.vertex_in(1, 'VEC4', 'color')
    info.vertex_in(2, 'FLOAT', 'intensity')
    info.vertex_in(3, 'FLOAT', 'classification')
    interface = gpu.types.GPUStageInterfaceInfo('scan_point_interface')
    interface.smooth('VEC4', 'point_color')
    info.vertex_out(interface)
    info.push_constant('MAT4', 'ModelViewProjectionMatrix')
    info.typedef_source('struct SectionData { mat4 matrix; mat4 world; vec4 clip[6]; };')
    info.uniform_buf(0, 'SectionData', 'section_data')
    info.push_constant('FLOAT', 'point_size')
    info.push_constant('FLOAT', 'intensity_min')
    info.push_constant('FLOAT', 'intensity_max')
    info.push_constant('INT', 'color_mode')
    info.push_constant('INT', 'class_filter')
    info.push_constant('INT', 'use_section')
    info.push_constant('INT', 'use_view_clip')
    info.fragment_out(0, 'VEC4', 'FragColor')
    info.vertex_source('''
void main() {
    vec4 position = vec4(pos, 1.0);
    gl_Position = ModelViewProjectionMatrix * position;
    if (use_section != 0) {
        vec3 box_pos = (section_data.matrix * position).xyz;
        if (any(greaterThan(abs(box_pos), vec3(1.0))))
            gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
    }
    if (use_view_clip != 0) {
        vec4 world_pos = section_data.world * position;
        for (int i = 0; i < 6; i++) {
            if (dot(section_data.clip[i], world_pos) < 0.0)
                gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
        }
    }
    int cls = int(classification + 0.5);
    if (class_filter >= 0 && cls != class_filter)
        gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
    gl_PointSize = point_size;
    if (color_mode == 1) {
        float gray = clamp((intensity - intensity_min) / max(intensity_max - intensity_min, 0.00001), 0.0, 1.0);
        point_color = vec4(gray, gray, gray, 1.0);
    } else if (color_mode == 2) {
        point_color = vec4(float((cls * 67 + 71) % 211 + 35),
                           float((cls * 113 + 29) % 211 + 35),
                           float((cls * 157 + 101) % 211 + 35), 255.0) / 255.0;
    } else {
        point_color = color;
    }
}''')
    info.fragment_source('''
void main() {
    if (length(gl_PointCoord - vec2(0.5)) > 0.5) discard;
    FragColor = point_color;
}''')
    return gpu.shader.create_from_info(info)


def _is_stream_source(obj):
    cache = obj.get("scan_octree")
    return (_is_copc_source(obj) or bool(cache and (Path(cache) / "manifest.json").is_file())) and not obj.get("scan_full_resolution_crop", False)


def _is_copc_source(obj):
    return str(obj.get("scan_source", "")).lower().endswith(".copc.laz")


def _preview_capacity(obj):
    scene = bpy.context.scene
    scans = max(1, sum("scan_cache" in item for item in scene.objects))
    pool = scene.scan_gpu_cache_mb * 1024 * 1024 // (2 if scene.scan_streaming else 1)
    count = min(obj.scan_display_count, obj["scan_retained_points"])
    count = min(count, scene.scan_point_budget, max(1, pool // scans // 24))
    return count


def _gpu_batch(pos, colors, intensity, classes):
    fmt = gpu.types.GPUVertFormat()
    fmt.attr_add(id="pos", comp_type='F32', len=3, fetch_mode='FLOAT')
    fmt.attr_add(id="color", comp_type='U8', len=4, fetch_mode='INT_TO_FLOAT_UNIT')
    fmt.attr_add(id="intensity", comp_type='F32', len=1, fetch_mode='FLOAT')
    fmt.attr_add(id="classification", comp_type='F32', len=1, fetch_mode='FLOAT')
    vbo = gpu.types.GPUVertBuf(len=len(pos), format=fmt)
    vbo.attr_fill(id="pos", data=pos)
    vbo.attr_fill(id="color", data=colors)
    vbo.attr_fill(id="intensity", data=intensity)
    vbo.attr_fill(id="classification", data=classes)
    return gpu.types.GPUBatch(type='POINTS', buf=vbo)


def _batch(obj):
    key = obj.as_pointer()
    if obj.get("scan_cache") == "embedded":
        count = _preview_capacity(obj)
        state = ("embedded", obj.data.as_pointer(), count)
        saved = _batches.get(key)
        if saved and saved[0] == state:
            _batches.move_to_end(key)
            return saved[1], saved[2]
        mesh = obj.data
        total = len(mesh.vertices)
        index = np.linspace(0, total - 1, count, dtype=np.int64)
        np.random.default_rng(0).shuffle(index)
        all_positions = np.empty((total, 3), dtype=np.float32)
        mesh.vertices.foreach_get("co", all_positions.ravel())
        colors = np.empty((total, 4), dtype=np.float32)
        mesh.attributes["scan_rgb"].data.foreach_get("color", colors.ravel())
        intensity = np.empty(total, dtype=np.float32)
        mesh.attributes["scan_intensity"].data.foreach_get("value", intensity)
        classes = np.empty(total, dtype=np.float32)
        mesh.attributes["scan_class"].data.foreach_get("value", classes)
        batch = _gpu_batch(all_positions[index], (colors[index] * 255).astype(np.uint8),
                           intensity[index], classes[index])
        _batches[key] = (state, batch, count)
        return batch, count
    path = Path(obj["scan_cache"])
    m = _manifest(path)
    count = _preview_capacity(obj)
    state = (str(path), count)
    saved = _batches.get(key)
    if saved and saved[0] == state:
        _batches.move_to_end(key)
        return saved[1], saved[2]
    total = m["retained_points"]
    index = np.linspace(0, total - 1, count, dtype=np.int64)
    np.random.default_rng(0).shuffle(index)
    pos = np.memmap(path / "positions.f32", dtype='<f4', mode='r', shape=(total, 3))[index]
    rgb = np.memmap(path / "colors.u8", dtype=np.uint8, mode='r', shape=(total, 3))[index]
    colors = np.empty((count, 4), dtype=np.uint8)
    colors[:, :3] = rgb
    colors[:, 3] = 255
    intensity = np.memmap(path / "intensity.f32", dtype='<f4', mode='r', shape=total)[index]
    classes = np.memmap(path / "classification.u8", dtype=np.uint8, mode='r', shape=total)[index].astype(np.float32)
    batch = _gpu_batch(pos, colors, intensity, classes)
    _batches[key] = (state, batch, count)
    return batch, count


def _in_view(obj, matrix):
    low, high, origin = obj["scan_bounds_min"], obj["scan_bounds_max"], obj["scan_origin"]
    clip = []
    transform = matrix @ obj.matrix_world
    for x in (low[0] - origin[0], high[0] - origin[0]):
        for y in (low[1] - origin[1], high[1] - origin[1]):
            for z in (low[2] - origin[2], high[2] - origin[2]):
                clip.append(transform @ Vector((x, y, z, 1.0)))
    return not (all(p.x < -p.w for p in clip) or all(p.x > p.w for p in clip)
                or all(p.y < -p.w for p in clip) or all(p.y > p.w for p in clip)
                or all(p.z < -p.w for p in clip) or all(p.z > p.w for p in clip))


def _draw():
    global _shader, _section_buffer, _last_draw_at
    started = time.perf_counter()
    now = time.monotonic()
    interval = now - _last_draw_at if _last_draw_at is not None else 0.0
    _last_draw_at = now
    drawn = stream_drawn = 0
    upload_ms = 0.0
    if _shader is None:
        _shader = _point_shader()
        _section_buffer = gpu.types.GPUUniformBuf(gpu.types.Buffer('FLOAT', 56, [0.0] * 56))
    gpu.state.depth_test_set('LESS_EQUAL')
    gpu.state.depth_mask_set(True)
    gpu.state.program_point_size_set(True)
    try:
        region = bpy.context.region_data
        matrix = region.perspective_matrix if region else None
        objects = [obj for obj in bpy.context.view_layer.objects
                   if "scan_cache" in obj and not obj.hide_get() and not obj.hide_viewport
                   and (matrix is None or _in_view(obj, matrix))]
        scene = bpy.context.scene
        stream_objects = [obj for obj in objects if _is_stream_source(obj)] if scene.scan_streaming else []
        view_areas = [area for area in bpy.context.screen.areas if area.type == 'VIEW_3D']
        main_area = max(view_areas, key=lambda area: area.width * area.height, default=None)
        can_stream = bool(stream_objects and matrix and bpy.context.area == main_area)
        runtime = _ensure_runtime() if can_stream else None
        roots = {node["object"] for node in runtime.plan
                 if node["key"][0] == 0 and node_key(node) in runtime.gpu
                 and now - runtime.gpu[node_key(node)][2] >= 0.15} if runtime else set()
        for obj in objects:
            if obj.name in roots:
                _batches.pop(obj.as_pointer(), None)
        requests = [0 if obj.name in roots else _preview_capacity(obj) for obj in objects]
        requested = sum(requests)
        budget = scene.scan_point_budget
        base_budget = budget // 4 if can_stream else budget
        scale = min(1.0, base_budget / requested) if requested else 1.0
        for obj, requested_count in zip(objects, requests):
            try:
                count = int(requested_count * scale)
                if count <= 0:
                    continue
                batch, available = _batch(obj)
                count = min(count, available)
                _draw_batch(batch, obj, count)
                drawn += count
            except Exception as exc:
                key = (obj.name, str(exc))
                if key not in _error_once:
                    print(f"Scan Viewer draw error for {obj.name}: {exc}")
                    _error_once.add(key)
        if can_stream:
            width, height = bpy.context.region.width, bpy.context.region.height
            views = []
            for obj in stream_objects:
                view = {"source": obj["scan_source"], "object": obj.name,
                        "origin": list(obj["scan_origin"]), "width": width, "height": height,
                        "matrix": [[round(value, 9) for value in row] for row in matrix @ obj.matrix_world]}
                view["class_filter"] = obj.scan_class_filter
                if obj.get("scan_octree"):
                    view["cache"] = obj["scan_octree"]
                else:
                    view["class_index"] = obj.get("scan_cache", "")
                section = scene.scan_section_box
                if section is not None and section.get("scan_crop_source") == obj.name:
                    clip = section.matrix_world.inverted() @ obj.matrix_world
                    view["clip_matrix"] = [list(row) for row in clip]
                views.append(view)
            preview_bytes = sum(saved[2] * 24 for saved in _batches.values())
            capacity = max(0, (scene.scan_gpu_cache_mb * 1024 * 1024 - preview_bytes) // 24)
            runtime.request_view(views, max(0, budget - drawn), scene.scan_pixel_error, capacity)
            lookup = {obj.name: obj for obj in stream_objects}
            upload_start = time.perf_counter()
            uploads = 0
            moving = time.monotonic() - runtime.changed_at < 0.12
            if moving and 0.002 < interval < 0.05:
                _motion_intervals.append(interval)
            elif not moving:
                _motion_intervals.clear()
            motion_scale = 1.0
            if moving and len(_motion_intervals) >= 8:
                baseline = sorted(_motion_intervals)[max(0, len(_motion_intervals) // 10)]
                if interval > baseline * 1.3:
                    motion_scale = max(0.2, baseline / interval)
            for node in runtime.plan:
                obj = lookup.get(node["object"])
                if obj is None:
                    continue
                key = node_key(node)
                gpu_key = key
                saved = runtime.gpu.get(gpu_key)
                if saved is None and key in runtime.cpu and not moving:
                    if uploads and (time.perf_counter() - upload_start) > 0.004:
                        continue
                    before = time.perf_counter()
                    pos, colors, intensity, classes = runtime.attributes(key)
                    batch = _gpu_batch(pos, colors, intensity, classes)
                    size = len(pos) * 24
                    runtime.trim_gpu(scene.scan_gpu_cache_mb * 1024 * 1024 - preview_bytes - size)
                    runtime.gpu[gpu_key] = (batch, size, time.monotonic())
                    runtime.gpu_bytes += size
                    saved = runtime.gpu[gpu_key]
                    uploads += 1
                    upload_ms += (time.perf_counter() - before) * 1000
                    if node["key"][0] == 0:
                        _redraw()
                if saved is None:
                    continue
                runtime.gpu.move_to_end(gpu_key)
                count = min(node["count"], saved[1] // 24, budget - drawn)
                count = int(count * min(1.0, (time.monotonic() - saved[2]) / 0.15))
                if moving:
                    count = int(count * motion_scale)
                if count <= 0:
                    continue
                _draw_batch(saved[0], obj, count, node["error"])
                drawn += count
                stream_drawn += count
            preview_bytes = sum(saved[2] * 24 for saved in _batches.values())
            runtime.trim_gpu(scene.scan_gpu_cache_mb * 1024 * 1024 - preview_bytes)
        preview_limit = scene.scan_gpu_cache_mb * 1024 * 1024 // (2 if can_stream else 1)
        while sum(saved[2] * 24 for saved in _batches.values()) > preview_limit and len(_batches) > 1:
            _batches.popitem(last=False)
    finally:
        gpu.state.depth_mask_set(False)
        gpu.state.depth_test_set('NONE')
        gpu.state.program_point_size_set(False)
        _draw_stats.update(points=drawn, stream_points=stream_drawn, upload_ms=upload_ms,
                           draw_ms=(time.perf_counter()-started)*1000)


def _draw_batch(batch, obj, count, spacing=0.0):
    gpu.matrix.push()
    try:
        gpu.matrix.multiply_matrix(obj.matrix_world)
        _shader.bind()
        _shader.uniform_float('ModelViewProjectionMatrix', gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix())
        _shader.uniform_float('point_size', max(obj.scan_point_size, min(8.0, spacing)))
        _shader.uniform_float('intensity_min', bpy.context.scene.scan_intensity_min)
        _shader.uniform_float('intensity_max', bpy.context.scene.scan_intensity_max)
        _shader.uniform_int('color_mode', {'RGB': 0, 'INTENSITY': 1, 'CLASS': 2}[obj.scan_color_mode])
        _shader.uniform_int('class_filter', obj.scan_class_filter)
        section = bpy.context.scene.scan_section_box
        active = section is not None and section.get('scan_crop_source') == obj.name
        _shader.uniform_int('use_section', int(active))
        region_view = bpy.context.region_data
        use_view_clip = bool(region_view and region_view.use_clip_planes)
        _shader.uniform_int('use_view_clip', int(use_view_clip))
        section_matrix = section.matrix_world.inverted() @ obj.matrix_world if active else Matrix.Identity(4)
        planes = np.asarray(region_view.clip_planes, dtype=np.float32).reshape(6, 4) if use_view_clip else np.zeros((6, 4), dtype=np.float32)
        uniforms = np.concatenate((np.asarray(section_matrix, dtype=np.float32).T.ravel(),
                                   np.asarray(obj.matrix_world, dtype=np.float32).T.ravel(),
                                   planes.ravel()))
        _section_buffer.update(gpu.types.Buffer('FLOAT', 56, uniforms))
        _shader.uniform_block('section_data', _section_buffer)
        batch.draw_range(_shader, elem_start=0, elem_count=count)
    finally:
        gpu.matrix.pop()


def _ensure_runtime():
    global _runtime
    if _runtime is None:
        _runtime = StreamRuntime(_binary())
    return _runtime


def _stream_tick():
    if _runtime is not None:
        _runtime.cpu_limit = bpy.context.scene.scan_cpu_cache_mb * 1024 * 1024
        _runtime.gpu_limit = bpy.context.scene.scan_gpu_cache_mb * 1024 * 1024
        changed = _runtime.tick()
        _runtime.trim_gpu(_runtime.gpu_limit - sum(saved[2] * 24 for saved in _batches.values()))
        waiting_upload = False
        for node in _runtime.plan:
            obj = bpy.data.objects.get(node["object"])
            key = node_key(node)
            if obj and not obj.hide_get() and key in _runtime.cpu and key not in _runtime.gpu:
                waiting_upload = True
                break
        fading = any(node_key(node) in _runtime.gpu
                     and time.monotonic() - _runtime.gpu[node_key(node)][2] < 0.15
                     for node in _runtime.plan)
        if changed or waiting_upload or fading or _runtime.inflight or _runtime.plan_future or _runtime.planned_generation != _runtime.generation:
            _redraw()
    for future, job in list(_modeling_jobs.items()):
        if not future.done():
            continue
        del _modeling_jobs[future]
        source_name, box_name, matrix, output = job
        try:
            response, _ = future.result()
            obj = _load_object(output, frame=False)
            obj.name = source_name + " modeling crop"
            obj.matrix_world = matrix
            obj["scan_crop_seconds"] = response["elapsed_ms"] / 1000
            obj = _embed_crop(obj, output)
            for name in (source_name, box_name):
                original = bpy.data.objects.get(name)
                if original:
                    original.hide_set(True)
            _frame_scan(obj)
            bpy.context.window_manager.scan_viewer_status = f"Full detail: {obj['scan_retained_points']:,} points"
        except Exception as exc:
            bpy.context.window_manager.scan_viewer_status = f"Crop failed: {exc}"
            print(f"Scan Viewer crop failed: {exc}")
        _redraw()
    for future, objects in list(_pick_jobs.items()):
        if not future.done():
            continue
        del _pick_jobs[future]
        try:
            result, _ = future.result()
            if not result["found"]:
                bpy.context.window_manager.scan_viewer_status = "No scan point found at the click"
                continue
            obj = objects[result["source_index"]]
            local = Vector([value - origin for value, origin in zip(result["xyz"], obj["scan_origin"])])
            world = obj.matrix_world @ local
            cursor = bpy.context.scene.cursor
            cursor.location = world
            if result["normal"] is not None:
                normal = obj.matrix_world.to_3x3().inverted().transposed() @ Vector(result["normal"])
                normal.normalize()
                cursor.rotation_mode = 'QUATERNION'
                cursor.rotation_quaternion = Vector((0, 0, 1)).rotation_difference(normal)
            bpy.context.scene["scan_last_pick_source"] = result["source"]
            bpy.context.scene["scan_last_pick_source_id"] = str(result["source_id"])
            bpy.context.scene["scan_last_pick_xyz"] = result["xyz"]
            bpy.context.window_manager.scan_viewer_status = (
                f"Picked {Path(result['source']).name}, {result['plane_points']} plane points")
        except Exception as exc:
            bpy.context.window_manager.scan_viewer_status = f"Pick failed: {exc}"
            print(f"Scan Viewer pick failed: {exc}")
        _redraw()
    for obj in bpy.context.scene.objects:
        cache = obj.get("scan_cache")
        source = obj.get("scan_source")
        if not cache or cache == "embedded" or not source:
            continue
        if (Path(cache) / "manifest.json").is_file():
            continue
        if source not in _repair_attempted and Path(source).is_file():
            _repair_attempted.add(source)
            _pending.append((Path(source), int(obj["scan_retained_points"]), False))
            _start_pending()
    return 0.04 if _runtime is not None or _modeling_jobs or _pick_jobs else 0.2


def _shutdown():
    global _runtime
    if _runtime is not None:
        _runtime.close()
        _runtime = None
    for _source, _box, _matrix, output in _modeling_jobs.values():
        if not (output / "manifest.json").is_file():
            shutil.rmtree(output, ignore_errors=True)
    _modeling_jobs.clear()
    _pick_jobs.clear()


@persistent
def _before_load(_dummy):
    _shutdown()
    _batches.clear()
    _manifests.clear()
    _pending.clear()
    _repair_attempted.clear()
    for proc, temp, _final, _source, _started, _frame in _jobs.values():
        proc.terminate()
    _jobs.clear()
    for proc, _temp, _final, _name, _started in _octree_jobs.values():
        proc.terminate()
    _octree_jobs.clear()


def _on_stream_toggle(self, context):
    if not self.scan_streaming:
        _shutdown()
    _batches.clear()
    _redraw()


def _start_pending():
    while _pending and len(_jobs) < 4:
        source, limit, frame = _pending.pop(0)
        final = _cache_path(source, limit)
        if (final / "manifest.json").is_file():
            try:
                if not _repair_missing_cache(final, source):
                    _load_object(final, frame=frame)
            except Exception as exc:
                print(f"Scan Viewer cache failed for {source}: {exc}")
            continue
        temp = final.with_name(final.name + f".tmp-{os.getpid()}-{time.time_ns()}")
        args = [str(_binary()), str(source), str(temp), "--max-points", str(limit)]
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _jobs[id(proc)] = (proc, temp, final, str(source), time.time(), frame)
    if _jobs and not bpy.app.timers.is_registered(_poll_jobs):
        bpy.app.timers.register(_poll_jobs, first_interval=0.1)


def _repair_missing_cache(path, source):
    targets = [obj for obj in bpy.context.scene.objects
               if obj.get("scan_source") == str(source)
               and obj.get("scan_cache") not in (None, "embedded")
               and not (Path(obj["scan_cache"]) / "manifest.json").is_file()]
    if not targets:
        return False
    manifest = _manifest(path)
    for obj in targets:
        obj["scan_cache"] = str(path)
        obj["scan_retained_points"] = manifest["retained_points"]
        obj["scan_origin"] = manifest["origin"]
        obj["scan_bounds_min"] = manifest["bounds_min"]
        obj["scan_bounds_max"] = manifest["bounds_max"]
        _batches.pop(obj.as_pointer(), None)
    _repair_attempted.discard(str(source))
    bpy.context.window_manager.scan_viewer_status = f"Rebuilt {Path(source).name} overview"
    _redraw()
    return True


def _queue_import(source, limit, frame=True):
    global _bulk_frame
    if not frame:
        _bulk_frame = True
    _pending.append((Path(source).resolve(), limit, frame))
    _start_pending()


def _folder_sources(directory):
    extensions = {".ply", ".las", ".laz", ".e57", ".xyz", ".pts", ".asc", ".txt", ".csv", ".zip"}
    paths = sorted(Path(directory).rglob("*"), key=lambda path: (not path.name.lower().endswith(".copc.laz"), str(path)))
    for path in paths:
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        if path.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(path) as archive:
                    members = [item for item in archive.infolist() if not item.is_dir()]
                    if len(members) != 1 or Path(members[0].filename).suffix.lower() not in {".xyz", ".pts", ".asc", ".txt", ".csv"}:
                        continue
                    if (path.parent / members[0].filename).is_file():
                        continue
            except (OSError, zipfile.BadZipFile):
                continue
        yield path


def _frame_all(context):
    objects = [obj for obj in context.view_layer.objects if "scan_cache" in obj]
    if not objects:
        return
    context.view_layer.update()
    lows = [float("inf")] * 3
    highs = [float("-inf")] * 3
    for obj in objects:
        low, high, origin = obj["scan_bounds_min"], obj["scan_bounds_max"], obj["scan_origin"]
        for x in (low[0] - origin[0], high[0] - origin[0]):
            for y in (low[1] - origin[1], high[1] - origin[1]):
                for z in (low[2] - origin[2], high[2] - origin[2]):
                    point = obj.matrix_world @ Vector((x, y, z))
                    for axis in range(3):
                        lows[axis] = min(lows[axis], point[axis])
                        highs[axis] = max(highs[axis], point[axis])
    midpoint = Vector([(a + b) * 0.5 for a, b in zip(lows, highs)])
    extent = max(a - b for a, b in zip(highs, lows))
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                area.spaces.active.clip_end = max(area.spaces.active.clip_end, extent * 20)
                area.spaces.active.region_3d.view_location = midpoint
                area.spaces.active.region_3d.view_distance = max(2.0, extent * 1.2)


def _poll_jobs():
    global _bulk_frame
    for job_id, job in list(_jobs.items()):
        proc, temp, final, source, started, frame = job
        if proc.poll() is None:
            continue
        stdout, stderr = proc.communicate()
        try:
            if proc.returncode:
                raise RuntimeError(stderr.strip() or stdout.strip() or f"Importer exited {proc.returncode}")
            _manifest(temp)
            if final.exists():
                shutil.rmtree(final)
            temp.rename(final)
            if _repair_missing_cache(final, source):
                print(f"Scan Viewer rebuilt {source}")
            else:
                obj = _load_object(final, frame=frame)
                print(f"Scan Viewer imported {source}: {obj['scan_retained_points']:,} points")
                bpy.context.window_manager.scan_viewer_status = f"Loaded {Path(source).name}"
        except Exception as exc:
            print(f"Scan Viewer import failed for {source}: {exc}")
            bpy.context.window_manager.scan_viewer_status = f"Import failed: {str(exc)[:100]}"
            shutil.rmtree(temp, ignore_errors=True)
        del _jobs[job_id]
    _start_pending()
    if not _jobs and not _pending and _bulk_frame:
        _frame_all(bpy.context)
        _bulk_frame = False
    _redraw()
    return 0.25 if _jobs or _pending else None


def _poll_octree_jobs():
    for job_id, job in list(_octree_jobs.items()):
        proc, temp, final, name, started = job
        if proc.poll() is None:
            continue
        stdout, stderr = proc.communicate()
        try:
            if proc.returncode:
                raise RuntimeError(stderr.strip() or stdout.strip() or f"Octree builder exited {proc.returncode}")
            if not (temp / "manifest.json").is_file():
                raise RuntimeError("Octree manifest was not written")
            temp.rename(final)
            obj = bpy.data.objects.get(name)
            if obj is not None:
                obj["scan_octree"] = str(final)
            bpy.context.window_manager.scan_viewer_status = f"Full detail ready for {name}"
        except Exception as exc:
            shutil.rmtree(temp, ignore_errors=True)
            bpy.context.window_manager.scan_viewer_status = f"Octree build failed: {str(exc)[:100]}"
            print(f"Scan Viewer octree build failed: {exc}")
        del _octree_jobs[job_id]
        _redraw()
    return 0.25 if _octree_jobs else None


class SCAN_OT_import(bpy.types.Operator, ImportHelper):
    bl_idname = "scan_viewer.import_cloud"
    bl_label = "Import point cloud"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".ply"
    filter_glob: StringProperty(default="*.ply;*.las;*.laz;*.e57;*.xyz;*.pts;*.asc;*.txt;*.csv;*.zip", options={'HIDDEN'})
    max_points: IntProperty(name="Cache point limit", default=2_000_000, min=1_000, max=50_000_000,
                            description="Maximum retained points; increase for a denser view")

    def execute(self, context):
        source = Path(self.filepath).resolve()
        try:
            _queue_import(source, self.max_points)
            context.window_manager.scan_viewer_status = f"Importing {source.name}"
            self.report({'INFO'}, f"Import started: {source.name}")
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class SCAN_OT_import_folder(bpy.types.Operator):
    bl_idname = "scan_viewer.import_folder"
    bl_label = "Import scan folder"
    bl_options = {'REGISTER', 'UNDO'}

    directory: StringProperty(name="Folder", subtype='DIR_PATH')

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        global _bulk_frame
        sources = list(_folder_sources(self.directory))
        if not sources:
            self.report({'ERROR'}, "No supported point clouds found in folder")
            return {'CANCELLED'}
        limit = max(1_000, context.scene.scan_point_budget // len(sources))
        try:
            if "scan_scene_origin" not in context.scene:
                survey = next((path for path in sources if path.suffix.lower() in {".las", ".laz"}), None)
                if survey is not None:
                    result = subprocess.run([str(_binary()), "inspect-origin", str(survey)],
                                            check=True, capture_output=True, text=True)
                    context.scene["scan_scene_origin"] = json.loads(result.stdout)
            for source in sources:
                _queue_import(source, limit, frame=False)
            if not _jobs and not _pending:
                _frame_all(context)
                _bulk_frame = False
            context.window_manager.scan_viewer_status = f"Queued {len(sources)} scans"
            self.report({'INFO'}, f"Queued {len(sources)} scans, {limit:,} cached points each")
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class SCAN_OT_cancel(bpy.types.Operator):
    bl_idname = "scan_viewer.cancel_import"
    bl_label = "Cancel import"

    @classmethod
    def poll(cls, context):
        return bool(_jobs or _pending)

    def execute(self, context):
        global _bulk_frame
        _pending.clear()
        _bulk_frame = False
        for job_id, (proc, temp, _final, _source, _started, _frame) in list(_jobs.items()):
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            shutil.rmtree(temp, ignore_errors=True)
            del _jobs[job_id]
        context.window_manager.scan_viewer_status = "Import cancelled"
        _redraw()
        return {'FINISHED'}


class SCAN_OT_build_octree(bpy.types.Operator):
    bl_idname = "scan_viewer.build_octree"
    bl_label = "Build full detail cache"
    bl_description = "Index every point in this scan for streaming; large sources may take several minutes"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and "scan_cache" in obj and not _is_copc_source(obj) and not obj.get("scan_full_resolution_crop")

    def execute(self, context):
        obj = context.active_object
        source = Path(obj["scan_source"])
        preview = Path(obj["scan_cache"])
        if not source.is_file() or not (preview / "manifest.json").is_file():
            self.report({'ERROR'}, "The scan source or overview cache is missing")
            return {'CANCELLED'}
        final = _octree_path(source)
        if (final / "manifest.json").is_file():
            obj["scan_octree"] = str(final)
            _redraw()
            return {'FINISHED'}
        if any(job[3] == obj.name for job in _octree_jobs.values()):
            return {'FINISHED'}
        temp = final.with_name(final.name + f".tmp-{os.getpid()}-{time.time_ns()}")
        proc = subprocess.Popen([str(_binary()), "build-octree", str(source), str(preview), str(temp)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _octree_jobs[id(proc)] = (proc, temp, final, obj.name, time.time())
        if not bpy.app.timers.is_registered(_poll_octree_jobs):
            bpy.app.timers.register(_poll_octree_jobs, first_interval=0.25)
        context.window_manager.scan_viewer_status = f"Building full detail for {obj.name}"
        return {'FINISHED'}


class SCAN_OT_cancel_octree(bpy.types.Operator):
    bl_idname = "scan_viewer.cancel_octree"
    bl_label = "Cancel full detail build"

    @classmethod
    def poll(cls, context):
        return bool(_octree_jobs)

    def execute(self, context):
        for job_id, (proc, temp, _final, _name, _started) in list(_octree_jobs.items()):
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            shutil.rmtree(temp, ignore_errors=True)
            del _octree_jobs[job_id]
        context.window_manager.scan_viewer_status = "Full detail build cancelled"
        return {'FINISHED'}


class SCAN_OT_snap_sample(bpy.types.Operator):
    bl_idname = "scan_viewer.snap_sample"
    bl_label = "Create modeling snap sample"
    bl_description = "Create a sparse Blender mesh for vertex snapping"
    bl_options = {'REGISTER', 'UNDO'}

    max_vertices: IntProperty(name="Vertices", default=50_000, min=100, max=500_000)

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and "scan_cache" in context.active_object

    def execute(self, context):
        source = context.active_object
        embedded = source.get("scan_cache") == "embedded"
        path = None if embedded else Path(source["scan_cache"])
        m = None if embedded else _manifest(path)
        total = len(source.data.vertices) if embedded else m["retained_points"]
        count = min(total, self.max_vertices)
        index = np.linspace(0, total - 1, count, dtype=np.int64)
        if embedded:
            all_positions = np.empty((total, 3), dtype=np.float32)
            source.data.vertices.foreach_get("co", all_positions.ravel())
            positions = all_positions[index]
        else:
            positions = np.memmap(path / "positions.f32", dtype='<f4', mode='r', shape=(total, 3))[index]
        mesh = bpy.data.meshes.new(source.name + " snap points")
        mesh.vertices.add(count)
        mesh.vertices.foreach_set("co", positions.ravel())
        if embedded:
            for name in ("scan_source_index", "scan_node_id", "scan_point_index"):
                values = np.empty(total, dtype=np.int32)
                source.data.attributes[name].data.foreach_get("value", values)
                mesh.attributes.new(name, 'INT', 'POINT').data.foreach_set("value", values[index])
        elif (path / "source_ids.u64").is_file():
            ids = np.memmap(path / "source_ids.u64", dtype='<u8', mode='r', shape=total)[index]
            for name, values in (("scan_source_index", (ids >> 56).astype(np.int32)),
                                 ("scan_node_id", ((ids >> 32) & 0x00ffffff).astype(np.int32)),
                                 ("scan_point_index", (ids & 0xffffffff).astype(np.int32))):
                attribute = mesh.attributes.new(name, 'INT', 'POINT')
                attribute.data.foreach_set("value", values)
        mesh.update()
        obj = bpy.data.objects.new(mesh.name, mesh)
        context.collection.objects.link(obj)
        obj.matrix_world = source.matrix_world.copy()
        obj.display_type = 'WIRE'
        obj["scan_source"] = source["scan_source"]
        if embedded:
            obj["scan_source_stamp"] = source["scan_source_stamp"]
            obj["scan_source_nodes"] = source["scan_source_nodes"]
            obj["scan_source_sources"] = source["scan_source_sources"]
        elif "source_stamp" in m:
            obj["scan_source_stamp"] = m["source_stamp"]
            obj["scan_source_nodes"] = json.dumps(m["source_nodes"])
        source.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        self.report({'INFO'}, f"Created {count:,} snap vertices")
        return {'FINISHED'}


class SCAN_OT_modeling_box(bpy.types.Operator):
    bl_idname = "scan_viewer.modeling_box"
    bl_label = "Add modeling box"
    bl_description = "Place a box at the view center, then move or scale it around the area to model"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and _is_stream_source(context.active_object)

    def execute(self, context):
        source = context.active_object
        view = context.space_data.region_3d if context.space_data and context.space_data.type == 'VIEW_3D' else None
        if view:
            center = view.view_location.copy()
        else:
            center = source.matrix_world @ Vector([(a+b)*0.5-o for a,b,o in
                zip(source["scan_bounds_min"], source["scan_bounds_max"], source["scan_origin"])])
        bpy.ops.mesh.primitive_cube_add(size=2, location=center)
        box = context.active_object
        box.name = "Modeling box"
        box.scale = Vector(context.scene.scan_crop_size) * 0.5
        context.view_layer.update()
        world = box.matrix_world.copy()
        box.parent = source
        box.matrix_world = world
        box.display_type = 'WIRE'
        box.show_in_front = True
        box.hide_render = True
        box["scan_crop_source"] = source.name
        context.scene.scan_section_box = box
        return {'FINISHED'}


class SCAN_OT_modeling_crop(bpy.types.Operator):
    bl_idname = "scan_viewer.modeling_crop"
    bl_label = "Load full detail in box"
    bl_description = "Read every source point in the box and retain its source ID"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and "scan_crop_source" in context.active_object

    def execute(self, context):
        box = context.active_object
        source = bpy.data.objects.get(box["scan_crop_source"])
        if source is None or not _is_stream_source(source):
            self.report({'ERROR'}, "The source COPC scan is no longer in the scene")
            return {'CANCELLED'}
        context.view_layer.update()
        if abs(box.matrix_world.determinant()) < 1e-12:
            self.report({'ERROR'}, "The modeling box must have a nonzero size")
            return {'CANCELLED'}
        origin = list(source["scan_origin"])
        specs = []
        seen = set()
        candidates = [source] + sorted((item for item in context.scene.objects
            if item != source and _is_stream_source(item)), key=lambda item: item.name)
        for candidate in candidates:
            path = candidate["scan_source"]
            if path in seen:
                continue
            seen.add(path)
            candidate_origin = list(candidate["scan_origin"])
            box_to_source = candidate.matrix_world.inverted() @ box.matrix_world
            corners = [box_to_source @ Vector((x,y,z)) for x in (-1,1) for y in (-1,1) for z in (-1,1)]
            bounds = [[min(point[i] for point in corners)+candidate_origin[i] for i in range(3)],
                      [max(point[i] for point in corners)+candidate_origin[i] for i in range(3)]]
            if candidate != source and any(bounds[0][i] > candidate["scan_bounds_max"][i]
                   or bounds[1][i] < candidate["scan_bounds_min"][i] for i in range(3)):
                continue
            clip = box.matrix_world.inverted() @ candidate.matrix_world
            to_output = source.matrix_world.inverted() @ candidate.matrix_world
            specs.append({"source": path, "origin": candidate_origin, "bounds": bounds,
                          "clip_matrix": [list(row) for row in clip],
                          "to_output": [list(row) for row in to_output]})
            if candidate.get("scan_octree"):
                specs[-1]["cache"] = candidate["scan_octree"]
        if not specs:
            self.report({'ERROR'}, "The modeling box does not meet a scan")
            return {'CANCELLED'}
        output = _cache_root() / f"crop-{time.time_ns()}"
        request = {"op": "crop", "source": source["scan_source"], "origin": origin,
                   "bounds": specs[0]["bounds"], "source_specs": specs,
                   "point_limit": context.scene.scan_crop_limit, "output": str(output)}
        future = _ensure_runtime().submit_crop(request)
        _modeling_jobs[future] = (source.name, box.name, source.matrix_world.copy(), output)
        context.window_manager.scan_viewer_status = "Reading full detail inside modeling box"
        return {'FINISHED'}


class SCAN_OT_pick_point(bpy.types.Operator):
    bl_idname = "scan_viewer.pick_point"
    bl_label = "Pick scan point"
    bl_description = "Click a scan point to place and orient the 3D cursor using a local fitted plane"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D' and any(
            _is_stream_source(obj) and not obj.hide_get() for obj in context.view_layer.objects)

    def invoke(self, context, event):
        context.window_manager.modal_handler_add(self)
        context.window_manager.scan_viewer_status = "Click a scan point, or press Esc"
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            context.window_manager.scan_viewer_status = "Pick cancelled"
            return {'CANCELLED'}
        if event.type != 'LEFTMOUSE' or event.value != 'PRESS':
            return {'PASS_THROUGH'}
        region = next((item for item in context.area.regions if item.type == 'WINDOW'), None)
        if region is None:
            return {'CANCELLED'}
        x, y = event.mouse_x - region.x, event.mouse_y - region.y
        if not (0 <= x < region.width and 0 <= y < region.height):
            return {'PASS_THROUGH'}
        view = context.area.spaces.active.region_3d
        ray_origin = view3d_utils.region_2d_to_origin_3d(region, view, (x, y))
        ray_direction = view3d_utils.region_2d_to_vector_3d(region, view, (x, y))
        objects = []
        specs = []
        section = context.scene.scan_section_box
        for obj in context.view_layer.objects:
            if not _is_stream_source(obj) or obj.hide_get() or obj.hide_viewport:
                continue
            inverse = obj.matrix_world.inverted()
            local_origin = inverse @ ray_origin
            local_direction = (inverse.to_3x3() @ ray_direction).normalized()
            origin = list(obj["scan_origin"])
            spec = {"source": obj["scan_source"], "origin": origin,
                    "ray_origin": [local_origin[i] + origin[i] for i in range(3)],
                    "ray_direction": list(local_direction),
                    "max_distance": context.area.spaces.active.clip_end,
                    "class_filter": obj.scan_class_filter}
            if obj.get("scan_octree"):
                spec["cache"] = obj["scan_octree"]
            if section is not None and section.get("scan_crop_source") == obj.name:
                spec["clip_matrix"] = [list(row) for row in section.matrix_world.inverted() @ obj.matrix_world]
            objects.append(obj)
            specs.append(spec)
        radius = max(0.03, view.view_distance * 2.0 / max(region.height, 1))
        request = {"op": "pick", "pick_sources": specs,
                   "pick_radius": radius, "plane_radius": max(0.75, radius * 4)}
        future = _ensure_runtime().submit_pick(request)
        _pick_jobs[future] = objects
        context.window_manager.scan_viewer_status = "Finding scan point and fitting plane"
        return {'FINISHED'}


class SCAN_OT_frame(bpy.types.Operator):
    bl_idname = "scan_viewer.frame"
    bl_label = "Frame scan"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and "scan_cache" in context.active_object

    def execute(self, context):
        _frame_scan(context.active_object)
        _redraw()
        return {'FINISHED'}


class SCAN_OT_frame_all(bpy.types.Operator):
    bl_idname = "scan_viewer.frame_all"
    bl_label = "Frame all scans"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return any("scan_cache" in obj for obj in context.view_layer.objects)

    def execute(self, context):
        _frame_all(context)
        _redraw()
        return {'FINISHED'}


class SCAN_PT_viewer(bpy.types.Panel):
    bl_label = "Scan Viewer"
    bl_idname = "SCAN_PT_viewer"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Scan"

    def draw(self, context):
        col = self.layout.column(align=True)
        col.operator("scan_viewer.import_cloud", icon='IMPORT')
        col.operator("scan_viewer.import_folder", icon='FILE_FOLDER')
        col.prop(context.scene, "scan_point_budget")
        col.prop(context.scene, "scan_streaming")
        if context.scene.scan_streaming:
            col.prop(context.scene, "scan_pixel_error")
            col.prop(context.scene, "scan_section_box")
            row = col.row(align=True)
            row.prop(context.scene, "scan_cpu_cache_mb")
            row.prop(context.scene, "scan_gpu_cache_mb")
            if _runtime:
                stats = _runtime.stats()
                col.label(text=f"Detail: level {stats['max_level']}, {stats['pending']} loading")
                col.label(text=f"Points drawn: {_draw_stats['points']:,}")
                col.label(text=f"CPU {stats['cpu_bytes']/1048576:.0f} MB, GPU {stats['gpu_bytes']/1048576:.0f} MB")
                if stats['error']:
                    col.label(text=stats['error'][:60], icon='ERROR')
        col.operator("scan_viewer.frame_all", icon='VIEWZOOM')
        if any(_is_stream_source(item) and not item.hide_get() for item in context.view_layer.objects):
            col.operator("scan_viewer.pick_point", icon='PIVOT_CURSOR')
        for _proc, _temp, _final, source, started, _frame in _jobs.values():
            col.label(text=f"Importing {Path(source).name}: {time.time() - started:.0f}s")
        if _pending:
            col.label(text=f"Queued: {len(_pending)}")
        if _jobs or _pending:
            col.operator("scan_viewer.cancel_import", icon='CANCEL')
        if _octree_jobs:
            for _proc, _temp, _final, name, started in _octree_jobs.values():
                col.label(text=f"Indexing {name}: {time.time() - started:.0f}s")
            col.operator("scan_viewer.cancel_octree", icon='CANCEL')
        elif context.window_manager.scan_viewer_status:
            col.label(text=context.window_manager.scan_viewer_status[:45])
        obj = context.active_object
        if obj is not None and "scan_crop_source" in obj:
            col.label(text="Move or scale this box around the modeling area")
            col.prop(context.scene, "scan_crop_limit")
            col.operator("scan_viewer.modeling_crop", icon='BORDER_RECT')
            if _modeling_jobs:
                col.label(text="Reading source points...")
            return
        if obj is None or "scan_cache" not in obj:
            col.label(text="Select a scan to adjust its view")
            return
        col.label(text=f"Source: {obj['scan_source_points']:,} points")
        col.label(text=f"Cached: {obj['scan_retained_points']:,} points")
        col.prop(obj, "scan_display_count")
        col.prop(obj, "scan_point_size")
        col.prop(obj, "scan_color_mode")
        col.prop(obj, "scan_class_filter")
        if obj.scan_color_mode == 'INTENSITY':
            row = col.row(align=True)
            row.prop(context.scene, "scan_intensity_min")
            row.prop(context.scene, "scan_intensity_max")
        col.operator("scan_viewer.frame", icon='VIEWZOOM')
        col.operator("scan_viewer.snap_sample", icon='MESH_DATA')
        if not _is_copc_source(obj) and not obj.get("scan_full_resolution_crop"):
            col.operator("scan_viewer.build_octree", icon='MOD_DATA_TRANSFER')
        if _is_stream_source(obj):
            col.prop(context.scene, "scan_crop_size")
            col.operator("scan_viewer.modeling_box", icon='CUBE')
        elif obj.get("scan_full_resolution_crop"):
            col.label(text="Full source detail with point IDs", icon='CHECKMARK')
        if any(obj["scan_origin"]):
            box = col.box()
            box.label(text="Georeferenced origin")
            for axis, value in zip("XYZ", obj["scan_origin"]):
                box.label(text=f"{axis}: {value:,.3f}")


_classes = (SCAN_OT_import, SCAN_OT_import_folder, SCAN_OT_cancel,
            SCAN_OT_build_octree, SCAN_OT_cancel_octree, SCAN_OT_snap_sample,
            SCAN_OT_modeling_box, SCAN_OT_modeling_crop, SCAN_OT_pick_point, SCAN_OT_frame,
            SCAN_OT_frame_all, SCAN_PT_viewer)


def register():
    global _handler
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.scan_viewer_status = StringProperty(default="")
    bpy.types.Scene.scan_point_budget = IntProperty(name="View point budget", default=2_000_000,
        min=10_000, max=50_000_000, update=_on_budget)
    bpy.types.Scene.scan_streaming = BoolProperty(name="Stream detail on zoom", default=True, update=_on_stream_toggle)
    bpy.types.Scene.scan_pixel_error = FloatProperty(name="Point spacing in pixels", default=1.5,
        min=0.5, max=10.0, update=_on_redraw)
    bpy.types.Scene.scan_cpu_cache_mb = IntProperty(name="CPU MB", default=256, min=64, max=4096)
    bpy.types.Scene.scan_gpu_cache_mb = IntProperty(name="GPU MB", default=128, min=64, max=2048)
    bpy.types.Scene.scan_section_box = PointerProperty(name="Live section box", type=bpy.types.Object, update=_on_redraw)
    bpy.types.Scene.scan_intensity_min = FloatProperty(name="Low", default=0.0, min=0.0, max=1.0, update=_on_redraw)
    bpy.types.Scene.scan_intensity_max = FloatProperty(name="High", default=1.0, min=0.0, max=1.0, update=_on_redraw)
    bpy.types.Scene.scan_crop_size = FloatVectorProperty(name="Box size", default=(20.0,20.0,20.0),
        min=0.1, max=1000.0, subtype='XYZ', unit='LENGTH')
    bpy.types.Scene.scan_crop_limit = IntProperty(name="Crop point limit", default=1_000_000, min=1000, max=5_000_000)
    bpy.types.Object.scan_display_count = IntProperty(name="Visible points", default=1_000_000,
        min=1_000, max=50_000_000, update=_on_setting)
    bpy.types.Object.scan_point_size = FloatProperty(name="Point size", default=2.0,
        min=1.0, max=12.0, update=_on_redraw)
    bpy.types.Object.scan_color_mode = EnumProperty(name="Color", items=[
        ('RGB', "RGB", "Source color"), ('INTENSITY', "Intensity", "Intensity grayscale"),
        ('CLASS', "Classification", "Color by class")], update=_on_redraw)
    bpy.types.Object.scan_class_filter = IntProperty(name="Class (-1 = all)", default=-1,
        min=-1, max=255, update=_on_redraw)
    _handler = bpy.types.SpaceView3D.draw_handler_add(_draw, (), 'WINDOW', 'POST_VIEW')
    bpy.app.timers.register(_stream_tick, first_interval=0.2, persistent=True)
    bpy.app.handlers.load_pre.append(_before_load)
    atexit.register(_shutdown)


def unregister():
    global _handler, _shader, _section_buffer
    _shutdown()
    atexit.unregister(_shutdown)
    if bpy.app.timers.is_registered(_stream_tick):
        bpy.app.timers.unregister(_stream_tick)
    if _before_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_before_load)
    if _handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handler, 'WINDOW')
        _handler = None
    _shader = None
    _section_buffer = None
    _batches.clear()
    del bpy.types.WindowManager.scan_viewer_status
    del bpy.types.Scene.scan_point_budget
    for name in ("scan_streaming", "scan_pixel_error", "scan_cpu_cache_mb", "scan_gpu_cache_mb", "scan_crop_size", "scan_crop_limit", "scan_section_box", "scan_intensity_min", "scan_intensity_max"):
        delattr(bpy.types.Scene, name)
    for name in ("scan_display_count", "scan_point_size", "scan_color_mode", "scan_class_filter"):
        delattr(bpy.types.Object, name)
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
