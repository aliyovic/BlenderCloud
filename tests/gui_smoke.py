from pathlib import Path
import traceback

import bpy
import numpy as np

root = Path(__file__).resolve().parents[1]
try:
    bpy.ops.preferences.addon_enable(module="scan_viewer")
    import scan_viewer

    for obj in tuple(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    result = bpy.ops.scan_viewer.import_cloud(
        filepath=str(root.parent / "Synthetic_Building/cloud_coloured.e57"),
        max_points=500000,
    )
    assert result == {'FINISHED'}

    def capture():
        scans = [obj for obj in bpy.data.objects if "scan_cache" in obj]
        if not scans:
            return 0.5
        obj = scans[0]
        assert obj["scan_source_points"] == 3427151
        obj.scan_point_size = 3.0
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                area.spaces.active.show_region_ui = True
        scan_viewer._frame_scan(obj)
        scan_viewer._redraw()
        bpy.app.timers.register(save_capture, first_interval=2.0)
        return None

    def save_capture():
        target = root / "tests/gui_synthetic.png"
        bpy.ops.screen.screenshot(filepath=str(target))
        (root / "tests/gui_status.txt").write_text(f"captured {target}; {bpy.context.active_object['scan_retained_points']} points\n")
        print("GUI_SCREENSHOT_OK", target)
        bpy.ops.wm.save_as_mainfile(filepath=str(root / "tests/gui_import.blend"))
        bpy.app.timers.register(capture_ifc, first_interval=1.0)
        return None

    def capture_ifc():
        import ifcopenshell
        import ifcopenshell.geom

        model = ifcopenshell.open(str(root.parent / "Synthetic_Building/model.ifc"))
        settings = ifcopenshell.geom.settings()
        settings.set(settings.USE_WORLD_COORDS, True)
        vertices = []
        for item in model.by_type("IfcProduct"):
            if not item.is_a("IfcWall") and not item.is_a("IfcSlab"):
                continue
            shape = ifcopenshell.geom.create_shape(settings, item)
            xyz = np.asarray(shape.geometry.verts).reshape(-1, 3)
            triangles = np.asarray(shape.geometry.faces).reshape(-1, 3)
            mesh = bpy.data.meshes.new(item.Name or item.is_a())
            mesh.from_pydata(xyz.tolist(), [], triangles.tolist())
            mesh.update()
            obj = bpy.data.objects.new(mesh.name, mesh)
            bpy.context.scene.collection.objects.link(obj)
            obj.display_type = 'WIRE'
            obj.color = (0.0, 1.0, 0.2, 1.0)
            vertices.append(xyz)
        all_xyz = np.concatenate(vertices)
        scan = bpy.context.active_object
        assert np.max(np.abs(all_xyz.min(axis=0) - np.array(scan["scan_bounds_min"]))) < 0.5
        assert np.max(np.abs(all_xyz.max(axis=0) - np.array(scan["scan_bounds_max"]))) < 0.5
        bpy.app.timers.register(save_ifc, first_interval=2.0)
        return None

    def save_ifc():
        target = root / "tests/gui_ifc_overlay.png"
        bpy.ops.screen.screenshot(filepath=str(target))
        print("GUI_IFC_OVERLAY_OK", target)
        for obj in tuple(bpy.data.objects):
            if obj.type == 'MESH':
                bpy.data.objects.remove(obj, do_unlink=True)
        bpy.context.active_object.hide_set(True)
        cloud = scan_viewer._load_object(root / "cache/cras_sample")
        cloud.scan_color_mode = 'CLASS'
        cloud.scan_point_size = 2.0
        bpy.app.timers.register(save_cras, first_interval=2.0)
        return None

    def save_cras():
        target = root / "tests/gui_cras.png"
        bpy.ops.screen.screenshot(filepath=str(target))
        print("GUI_CRAS_OK", target)
        bpy.context.active_object.hide_set(True)
        cloud = scan_viewer._load_object(root / "cache/swiss_copc")
        cloud.scan_color_mode = 'CLASS'
        cloud.scan_point_size = 2.0
        bpy.app.timers.register(save_copc, first_interval=2.0)
        return None

    def save_copc():
        target = root / "tests/gui_copc.png"
        bpy.ops.screen.screenshot(filepath=str(target))
        print("GUI_COPC_OK", target)
        return None

    bpy.app.timers.register(capture, first_interval=0.5)
except Exception:
    (root / "tests/gui_status.txt").write_text(traceback.format_exc())
    raise
