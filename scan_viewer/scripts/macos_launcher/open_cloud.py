"""Import Finder-selected point clouds into a fresh Blender window."""

from pathlib import Path
import sys

import bpy


SUPPORTED = {".las", ".laz", ".e57", ".ply", ".xyz", ".pts", ".asc", ".txt", ".csv", ".zip"}


def open_clouds():
    paths = list(dict.fromkeys(Path(value).resolve() for value in sys.argv[sys.argv.index("--") + 1:]))
    if not paths:
        raise ValueError("No point cloud was selected.")
    for path in paths:
        if not path.is_file() or path.suffix.lower() not in SUPPORTED:
            raise ValueError(f"Unsupported point cloud: {path}")

    roots = {"scan_viewer"}
    roots.update(name for name in bpy.context.preferences.addons.keys()
                 if name.startswith("bl_ext.") and name.endswith(".scan_viewer"))
    for name in roots:
        if name in bpy.context.preferences.addons:
            bpy.ops.preferences.addon_disable(module=name)
    for name in tuple(sys.modules):
        if any(name == root or name.startswith(root + ".") for root in roots):
            del sys.modules[name]
    sys.path.insert(0, str(Path(__file__).resolve().parent / "addons"))
    bpy.ops.preferences.addon_enable(module="scan_viewer")
    import scan_viewer

    # This script runs only in the new Blender process started by the launcher.
    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    if "scan_scene_origin" in bpy.context.scene:
        del bpy.context.scene["scan_scene_origin"]
    bpy.context.scene.unit_settings.system = 'METRIC'
    bpy.context.preferences.view.show_splash = False
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                space = area.spaces.active
                space.show_region_ui = True
                space.overlay.show_floor = False
                space.overlay.show_axis_x = False
                space.overlay.show_axis_y = False

    attempts = 0

    def show_scan_tab():
        nonlocal attempts
        attempts += 1
        waiting = False
        # Blender creates sidebar categories when each visible region first draws.
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type != 'VIEW_3D':
                    continue
                for region in area.regions:
                    if region.type != 'UI':
                        continue
                    if region.is_property_readonly("active_panel_category"):
                        waiting = True
                        continue
                    try:
                        region.active_panel_category = "Scan"
                        region.tag_redraw()
                    except (AttributeError, TypeError):
                        waiting = True
        return 0.2 if waiting and attempts < 20 else None

    bpy.app.timers.register(show_scan_tab, first_interval=0.2)

    single = len(paths) == 1
    limit = max(1_000, bpy.context.scene.scan_point_budget // len(paths))
    for path in paths:
        scan_viewer._queue_import(path, limit, frame=single)
    if not single and not scan_viewer._jobs and not scan_viewer._pending:
        scan_viewer._frame_all(bpy.context)
        scan_viewer._bulk_frame = False
    bpy.context.window_manager.scan_viewer_status = f"Opening {len(paths)} point cloud(s)"

    def finish():
        if scan_viewer._jobs or scan_viewer._pending:
            return 0.25
        scans = [obj for obj in bpy.context.scene.objects if "scan_cache" in obj]
        for obj in scans:
            octree = scan_viewer._octree_path(obj["scan_source"])
            if (octree / "manifest.json").is_file():
                obj["scan_octree"] = str(octree)
            print(f"SCAN_VIEWER_OPENED {obj['scan_source']} {obj['scan_source_points']} source points, "
                  f"{obj['scan_retained_points']} cached points", flush=True)
        if len(scans) == len(paths):
            bpy.context.window_manager.scan_viewer_status = f"Loaded {', '.join(path.name for path in paths)}"
        scan_viewer._redraw()
        return None

    bpy.app.timers.register(finish, first_interval=0.25)


def start():
    try:
        open_clouds()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        message = str(exc)
        bpy.context.window_manager.popup_menu(
            lambda menu, context: menu.layout.label(text=message),
            title="Point cloud import failed", icon='ERROR',
        )
    return None


bpy.app.timers.register(start, first_interval=0.5)
