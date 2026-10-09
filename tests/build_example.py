from pathlib import Path
import sys

import bpy
import ifcopenshell
import ifcopenshell.geom

root = Path(__file__).resolve().parents[1]
bpy.ops.preferences.addon_enable(module="scan_viewer")
import scan_viewer

for obj in tuple(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)

cloud = scan_viewer._load_object(root / "cache/synthetic")
cloud.scan_point_size = 3.0
cloud.scan_display_count = 856788

model = ifcopenshell.open(str(root.parent / "Synthetic_Building/model.ifc"))
settings = ifcopenshell.geom.settings()
settings.set(settings.USE_WORLD_COORDS, True)
collection = bpy.data.collections.new("IFC reference")
bpy.context.scene.collection.children.link(collection)
count = 0
for item in model.by_type("IfcProduct"):
    if not item.is_a("IfcWall") and not item.is_a("IfcSlab"):
        continue
    shape = ifcopenshell.geom.create_shape(settings, item)
    coords = shape.geometry.verts
    faces = shape.geometry.faces
    vertices = [coords[i:i + 3] for i in range(0, len(coords), 3)]
    triangles = [faces[i:i + 3] for i in range(0, len(faces), 3)]
    mesh = bpy.data.meshes.new(item.Name or item.is_a())
    mesh.from_pydata(vertices, [], triangles)
    mesh.update()
    obj = bpy.data.objects.new(mesh.name, mesh)
    collection.objects.link(obj)
    obj.display_type = 'WIRE'
    obj["ifc_global_id"] = item.GlobalId
    count += 1

for obj in bpy.context.selected_objects:
    obj.select_set(False)
cloud.select_set(True)
bpy.context.view_layer.objects.active = cloud
scan_viewer._frame_scan(cloud)

destination = root / "examples/synthetic_scan_ifc.blend"
destination.parent.mkdir(exist_ok=True)
bpy.ops.wm.save_as_mainfile(filepath=str(destination))
print("EXAMPLE_READY", destination, count, "IFC objects")
