"""Import the pilot IFC through Bonsai and save its scan reference in Blender.

Run with Blender, for example::

    Blender --background --python build_bonsai_scene.py -- --ifc railway_crop.ifc

The source reconstruction is read-only. No preferences are saved.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import addon_utils
import bpy
from mathutils import Vector


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
ORIGIN = [2689160.0, 1254972.0, 452.0]
SCAN_NAME = "Source scan, full 5,500,234 points"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ifc", type=Path, default=ROOT / "output/railway_crop.ifc")
    parser.add_argument("--blend", type=Path, default=ROOT / "output/railway_crop.blend")
    parser.add_argument(
        "--source-blend", type=Path, default=WORKSPACE / "infrastructure/reconstruction/model.blend"
    )
    parser.add_argument(
        "--reconstruction", type=Path, default=WORKSPACE / "infrastructure/reconstruction"
    )
    parser.add_argument("--render", action="store_true", help="Render a PNG from the imported IFC geometry.")
    return parser.parse_args(sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else [])


def ensure_bonsai() -> None:
    if not hasattr(bpy.types.Scene, "BIMProperties"):
        addon_utils.enable("bl_ext.user_default.bonsai", default_set=False, persistent=False)
    if not hasattr(bpy.types.Scene, "BIMProperties"):
        raise RuntimeError("Bonsai could not register. Install a Bonsai build for this Blender version.")


def ensure_railway_bim() -> None:
    if hasattr(bpy.types.Scene, "railway_bim"):
        return
    sys.path.insert(0, str(WORKSPACE))
    import railway_bim.addon as addon

    addon.register()


def set_excluded(layer: bpy.types.ViewLayer, collection: bpy.types.Collection, excluded: bool) -> None:
    def visit(node: bpy.types.LayerCollection) -> bool:
        if node.collection == collection:
            node.exclude = excluded
            return True
        return any(visit(child) for child in node.children)

    if not visit(layer.layer_collection):
        raise RuntimeError(f"Collection is absent from view layer: {collection.name}")


def append_reference(source: Path) -> tuple[bpy.types.Collection, bpy.types.Collection]:
    requested = ["Reference scan, full source", "Cameras and lighting"]
    with bpy.data.libraries.load(str(source), link=False) as (available, requested_data):
        missing = set(requested) - set(available.collections)
        if missing:
            raise RuntimeError(f"Source reconstruction collections are missing: {sorted(missing)}")
        requested_data.collections = requested
    reference, presentation = requested_data.collections
    bpy.context.scene.collection.children.link(reference)
    bpy.context.scene.collection.children.link(presentation)
    scan = bpy.data.objects.get(SCAN_NAME)
    if scan is None or scan.type != "MESH" or len(scan.data.vertices) != 5_500_234:
        raise RuntimeError("The source reconstruction does not contain the complete pilot scan.")
    if list(scan.get("LV95_origin_m", [])) != ORIGIN:
        raise RuntimeError("Scan reference origin differs from the IFC pilot origin.")
    scan.hide_render = True
    scan.hide_select = True
    reference.name = "Scan reference | full source"
    presentation.name = "Presentation | cameras and lights"
    return reference, presentation


def configure_view(scene: bpy.types.Scene) -> None:
    camera = bpy.data.objects.get("Overview")
    if camera and camera.type == "CAMERA":
        scene.camera = camera
        rotation = camera.rotation_euler.to_quaternion()
    else:
        rotation = (Vector((0, 0, 3)) - Vector((-40, -50, 45))).to_track_quat("-Z", "Y")
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == "VIEW_3D":
                space = area.spaces.active
                space.shading.type = "MATERIAL"
                space.shading.use_scene_world = False
                space.shading.use_scene_lights = False
                space.overlay.show_floor = False
                space.overlay.show_axis_x = False
                space.overlay.show_axis_y = False
                space.clip_end = 1000
                space.region_3d.view_rotation = rotation
                space.region_3d.view_location = (0, 0, 3)
                space.region_3d.view_distance = 80
                space.region_3d.view_perspective = "PERSP"
            elif area.type == "PROPERTIES":
                area.spaces.active.context = "SCENE"
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = 1600
    scene.render.resolution_y = 1000
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.world = bpy.data.worlds.new("Railway BIM world")
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs[0].default_value = (0.18, 0.22, 0.27, 1)
    scene.world.node_tree.nodes["Background"].inputs[1].default_value = 0.3


def main() -> None:
    args = arguments()
    args.ifc = args.ifc.resolve()
    args.blend = args.blend.resolve()
    args.source_blend = args.source_blend.resolve()
    if not args.ifc.is_file() or not args.source_blend.is_file():
        raise FileNotFoundError("Build the IFC and keep the source reconstruction available before importing.")
    if args.blend == args.source_blend:
        raise ValueError("The output Blender file must differ from the source reconstruction.")
    ensure_bonsai()
    ensure_railway_bim()
    import bonsai.tool as tool

    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    result = bpy.ops.bim.load_project(
        filepath=str(args.ifc),
        should_start_fresh_session=False,
        import_without_ifc_data=False,
        skip_recent=True,
    )
    if result != {"FINISHED"} or tool.Ifc.get() is None:
        raise RuntimeError(f"Bonsai failed to import {args.ifc}: {result}")
    model = tool.Ifc.get()
    if model.schema_identifier != "IFC4X3_ADD2":
        raise RuntimeError(f"Expected IFC4X3_ADD2, got {model.schema_identifier}")
    products = [p for p in model.by_type("IfcProduct") if p.Representation]
    missing = [p.GlobalId for p in products if tool.Ifc.get_object(p) is None]
    if missing:
        raise RuntimeError(f"Bonsai did not associate represented products: {missing}")

    scene = bpy.context.scene
    scene.name = "Railway BIM | IFC 4.3"
    ifc_collections = list(scene.collection.children)
    reference, presentation = append_reference(args.source_blend)
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    scene.unit_settings.length_unit = "METERS"
    scene["LV95_origin_m"] = ORIGIN
    scene["railway_bim_ifc_path"] = str(args.ifc)
    scene["railway_bim_reconstruction_dir"] = str(args.reconstruction.resolve())
    scene["railway_bim_source_blend"] = str(args.source_blend)
    scene["source_points"] = 5_500_234
    scene["coordinate_rule"] = "Source survey XYZ = local engineering XYZ + [2689160, 1254972, 452] metres."
    scene["coordinate_status"] = "Map conversion retains the source offset. Horizontal CRS is assumed; height datum unknown."
    scene["railway_bim_schema"] = model.schema_identifier
    scene["railway_bim_version"] = "0.1.0"
    if hasattr(scene, "railway_bim"):
        scene.railway_bim.reconstruction_dir = str(args.reconstruction.resolve())
        scene.railway_bim.output_ifc = str(args.ifc)

    primary = scene.view_layers[0]
    primary.name = "BIM model"
    overlay = scene.view_layers.new("Scan overlay")
    scan_only = scene.view_layers.new("Scan reference")
    overlay.use = False
    scan_only.use = False
    set_excluded(primary, reference, True)
    for collection in ifc_collections:
        set_excluded(scan_only, collection, True)
    for product in products:
        name = (product.Name or "").lower()
        if product.is_a("IfcGeographicElement") or product.is_a("IfcEarthworksFill") or "trough" in name:
            obj = tool.Ifc.get_object(product)
            if obj:
                obj.hide_set(True, view_layer=overlay)
    bpy.context.window.view_layer = primary
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    rails = model.by_type("IfcRail")
    if rails:
        obj = tool.Ifc.get_object(rails[0])
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        if hasattr(scene, "railway_bim"):
            scene.railway_bim.parameter = "head_width_m"
            spec = json.loads((args.reconstruction / "work/component_spec.json").read_text())
            scene.railway_bim.value_m = float(spec["rails"][0]["head_width_m"])
    configure_view(scene)
    text = bpy.data.texts.new("Railway BIM read me")
    text.write(
        "Railway pilot imported through Bonsai from IFC4X3_ADD2.\n\n"
        "Choose view layers BIM model, Scan overlay, or Scan reference.\n"
        "The scan mesh retains all 5,500,234 points and their RGB values. Its display modifier samples points only for viewing.\n"
        "Edit IFC objects using Bonsai, then save the IFC project and the Blender workspace.\n"
        "The original source reconstruction remains separate.\n\n"
        "Coordinates are local metres. Add [2689160, 1254972, 452] to recover source survey coordinates.\n"
        "Map conversion retains the source offset. Horizontal CRS is assumed; height datum is unknown.\n"
        "Hidden sections and some profiles are assumptions.\n"
    )
    args.blend.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(args.blend), compress=True)
    receipt = {
        "blend": str(args.blend),
        "ifc": str(args.ifc),
        "schema": model.schema_identifier,
        "blender_version": bpy.app.version_string,
        "bonsai_version": tool.Blender.get_bonsai_version(),
        "source_blend": str(args.source_blend),
        "source_points": len(bpy.data.objects[SCAN_NAME].data.vertices),
        "represented_products": len(products),
        "product_classes": dict(sorted(Counter(p.is_a() for p in products).items())),
        "view_layers": [layer.name for layer in scene.view_layers],
        "origin_m": ORIGIN,
        "ifc_authoring_links": not missing,
        "independent_reopen_pending": True,
    }
    args.blend.with_suffix(".build.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print("RAILWAY_BIM_BUILD", json.dumps(receipt), flush=True)
    if args.render:
        scene.render.filepath = str(args.blend.with_suffix(".png"))
        bpy.ops.render.render(write_still=True)
        print("RAILWAY_BIM_RENDER", scene.render.filepath, flush=True)


if __name__ == "__main__":
    main()
