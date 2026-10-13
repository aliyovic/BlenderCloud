"""Create editable Blender geometry from a prepared local project."""
from pathlib import Path
from copy import deepcopy
import json
import uuid

import bpy
import numpy as np
from mathutils import Vector

from .core import DataError, load_manifest


def _contract_project_id(contract):
    if (not isinstance(contract, dict) or contract.get("schema") != "railway-project-contract/1"
            or contract.get("units") != "metre"):
        raise DataError("The railway project reference is unsupported.")
    try:
        return str(uuid.UUID(contract["project_id"]))
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise DataError("The project identity must be a persistent UUID.") from exc


def _scene_contract(scene):
    if "railway_project_contract_json" not in scene:
        return None
    try:
        contract = json.loads(scene["railway_project_contract_json"])
    except (ValueError, TypeError) as exc:
        raise DataError("The scene has an invalid railway project reference.") from exc
    _contract_project_id(contract)
    return contract


def scene_project_id(scene):
    """Read a saved project UUID without replacing malformed identity."""
    contract = _scene_contract(scene)
    contract_id = _contract_project_id(contract) if contract is not None else None
    ifc_id = None
    if "railway_ifc_project_id" in scene:
        try:
            ifc_id = str(uuid.UUID(scene["railway_ifc_project_id"]))
        except (ValueError, TypeError, AttributeError) as exc:
            raise DataError("The checked IFC project identity must be a persistent UUID.") from exc
    if contract_id is not None and ifc_id is not None and contract_id != ifc_id:
        raise DataError("The scene has conflicting project identity references.")
    return contract_id or ifc_id


def _merge_reference_fact(saved, incoming, label, value_key=None):
    ranks = {"UNKNOWN": 0, "ASSUMED": 1, "DECLARED": 2, "VERIFIED": 3}
    if not isinstance(saved, dict) or not isinstance(incoming, dict):
        raise DataError(f"The project {label} reference is invalid.")
    if saved.get("status") not in ranks or incoming.get("status") not in ranks:
        raise DataError(f"The project {label} reference status is invalid.")
    if value_key:
        old, new = saved.get(value_key), incoming.get(value_key)
        if old not in (None, "", "UNKNOWN") and new not in (None, "", "UNKNOWN") and old != new:
            raise DataError(f"This bundle uses another project {label}. Open it in a new scene.")
    merged = deepcopy(saved)
    for key, value in incoming.items():
        if key not in ("status", "evidence") and merged.get(key) in (None, "", "UNKNOWN"):
            merged[key] = deepcopy(value)
    if ranks[incoming["status"]] > ranks[saved["status"]]:
        merged["status"] = incoming["status"]
        evidence = [saved.get("evidence", ""), incoming.get("evidence", "")]
        merged["evidence"] = "\n".join(dict.fromkeys(value for value in evidence if value))
    return merged


def _merge_project_contract(saved, incoming):
    """Keep established project fields while accepting compatible GIS facts."""
    if saved is None:
        return deepcopy(incoming)
    if incoming is None:
        return deepcopy(saved)
    if _contract_project_id(saved) != _contract_project_id(incoming):
        raise DataError("This bundle belongs to another project. Open it in a new scene.")
    merged = deepcopy(saved)
    for key in ("tracks", "terrain_regions", "sampling"):
        value = incoming.get(key)
        if value and saved.get(key) and value != saved[key]:
            raise DataError(f"This bundle has conflicting project {key}. Open it in a new scene.")
        if value and not saved.get(key):
            merged[key] = deepcopy(value)
    for key, value in incoming.items():
        merged.setdefault(key, deepcopy(value))
    old, new = saved.get("reference"), incoming.get("reference")
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise DataError("The project geographic reference is invalid.")
    reference = merged["reference"]
    for key, value in new.items():
        if key in ("horizontal", "vertical", "official_chainage", "design_alignment", "gauge") and key in old:
            value_key = "crs" if key == "horizontal" else "datum" if key == "vertical" else None
            label = "CRS" if key == "horizontal" else "height datum" if key == "vertical" else key
            reference[key] = _merge_reference_fact(old[key], value, label, value_key)
        else:
            reference.setdefault(key, deepcopy(value))
    return merged


def scene_origin(scene):
    anchors = []
    if scene.get("railway_project_contract_json"):
        contract = json.loads(scene["railway_project_contract_json"])
        if contract.get("schema") != "railway-project-contract/1":
            raise DataError("The scene has an unsupported railway project reference.")
        anchors.append(contract["reference"]["local_origin_m"])
    if scene.get("swiss_gis_origin_json"):
        anchors.append(json.loads(scene["swiss_gis_origin_json"]))
    for key in ("scan_scene_origin", "LV95_origin_m"):
        if key in scene:
            anchors.append(list(scene[key]))
    if not anchors:
        if any("scan_cache" in obj for obj in scene.objects):
            raise DataError("This scene contains an unreferenced scan. Use a new scene for LV95 data.")
        return None
    if any(len(value) != 3 or not np.isfinite(value).all() for value in anchors):
        raise DataError("The scene has an invalid geographic origin.")
    if any(not np.allclose(anchors[0], value, atol=1e-6, rtol=0) for value in anchors[1:]):
        raise DataError("The scene has conflicting geographic origins.")
    return tuple(anchors[0])


def material(name, color):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*color, 1)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (*color, 1)
    bsdf.inputs["Roughness"].default_value = .85
    return mat


def vertex_material(name):
    mat = material(name, (.4, .4, .4))
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    color = nodes.new("ShaderNodeAttribute")
    color.attribute_name = "swiss_rgb"
    bsdf = nodes.get("Principled BSDF")
    links.new(color.outputs["Color"], bsdf.inputs["Base Color"])
    return mat


def mesh_object(collection, name, vertices, faces, origin, role):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices.tolist(), [], faces.tolist() if hasattr(faces, "tolist") else faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj["swiss_gis_role"] = role
    obj["swiss_gis_origin_json"] = json.dumps(origin)
    return obj


def set_colors(obj, rgb):
    attr = obj.data.color_attributes.new(name="swiss_rgb", type="BYTE_COLOR", domain="POINT")
    rgba = np.column_stack((rgb, np.ones(len(rgb)))).astype(np.float32)
    attr.data.foreach_set("color_srgb", rgba.ravel())


def point_display(obj, radius):
    tree = bpy.data.node_groups.new("Swiss GIS scan points", "GeometryNodeTree")
    tree.interface.new_socket(name="Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket(name="Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    socket = tree.interface.new_socket(name="Radius metres", in_out="INPUT", socket_type="NodeSocketFloat")
    socket.default_value = radius
    socket.min_value = .001
    socket.max_value = .25
    inp = tree.nodes.new("NodeGroupInput")
    out = tree.nodes.new("NodeGroupOutput")
    points = tree.nodes.new("GeometryNodeMeshToPoints")
    points.mode = "VERTICES"
    assign = tree.nodes.new("GeometryNodeSetMaterial")
    assign.inputs["Material"].default_value = obj.data.materials[0]
    tree.links.new(inp.outputs["Geometry"], points.inputs["Mesh"])
    tree.links.new(inp.outputs["Radius metres"], points.inputs["Radius"])
    tree.links.new(points.outputs["Points"], assign.inputs["Geometry"])
    tree.links.new(assign.outputs["Geometry"], out.inputs["Geometry"])
    mod = obj.modifiers.new("Scan point display", "NODES")
    mod.node_group = tree


def image_material(name, path, transparent=True):
    mat = material(name, (.5, .5, .5))
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    texture = nodes.new("ShaderNodeTexImage")
    texture.image = bpy.data.images.load(str(path), check_existing=False)
    texture.image.pack()
    bsdf = nodes.get("Principled BSDF")
    if transparent:
        links.new(texture.outputs["Color"], bsdf.inputs["Base Color"])
        links.new(texture.outputs["Alpha"], bsdf.inputs["Alpha"])
    else:
        mix = nodes.new("ShaderNodeMixRGB")
        mix.inputs[1].default_value = (.36, .42, .29, 1)
        links.new(texture.outputs["Alpha"], mix.inputs[0])
        links.new(texture.outputs["Color"], mix.inputs[2])
        links.new(mix.outputs[0], bsdf.inputs["Base Color"])
    return mat


def image_plane(collection, name, path, area, origin, height, role):
    x0, y0, x1, y1 = area
    vertices = np.array([[x0-origin[0], y0-origin[1], height-origin[2]],
                         [x1-origin[0], y0-origin[1], height-origin[2]],
                         [x1-origin[0], y1-origin[1], height-origin[2]],
                         [x0-origin[0], y1-origin[1], height-origin[2]]])
    obj = mesh_object(collection, name, vertices, [(0, 1, 2, 3)], origin, role)
    uv = obj.data.uv_layers.new(name="LV95 imagery")
    uv.data.foreach_set("uv", np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=np.float32).ravel())
    obj.data.materials.append(image_material(name, path))
    obj["swiss_gis_area_json"] = json.dumps(area)
    return obj


def texture_terrain(obj, path, area, origin):
    uv = obj.data.uv_layers.new(name="LV95 imagery")
    positions = np.empty(len(obj.data.vertices) * 3, dtype=np.float32)
    obj.data.vertices.foreach_get("co", positions)
    positions = positions.reshape(-1, 3).astype(np.float64)
    indices = np.empty(len(obj.data.loops), dtype=np.int32)
    obj.data.loops.foreach_get("vertex_index", indices)
    global_xy = positions[:, :2] + np.array(origin[:2])
    coords = (global_xy - np.array(area[:2])) / (np.array(area[2:]) - np.array(area[:2]))
    uv.data.foreach_set("uv", coords[indices].astype(np.float32).ravel())
    obj.data.materials.clear()
    obj.data.materials.append(image_material("Local aerial on terrain", path, transparent=False))


def frame_area(context, manifest, top=False):
    area, origin = manifest["area"], manifest["origin"]
    center = Vector(((area[0]+area[2])/2-origin[0], (area[1]+area[3])/2-origin[1], 0))
    extent = max(area[2]-area[0], area[3]-area[1])
    for screen in bpy.data.screens:
        for region in screen.areas:
            if region.type != "VIEW_3D":
                continue
            space = region.spaces.active
            space.clip_start = .05
            space.clip_end = max(1000, extent * 10)
            space.region_3d.view_location = center
            space.region_3d.view_distance = extent * .95
            space.region_3d.view_perspective = "ORTHO"
            if top:
                space.region_3d.view_rotation = (1, 0, 0, 0)
            else:
                direction = Vector((.6, -.8, .9))
                space.region_3d.view_rotation = direction.to_track_quat("Z", "Y")
            space.shading.type = "MATERIAL"
            space.overlay.show_floor = False
            region.tag_redraw()


def _scene_terrain_references(scene):
    references = [(obj, obj["swiss_gis_terrain_vertical_datum"]) for obj in scene.objects
                  if "swiss_gis_terrain_vertical_datum" in obj]
    try:
        manifest = json.loads(scene.get("swiss_gis_manifest_json", "null"))
    except (ValueError, TypeError):
        return references
    if not isinstance(manifest, dict):
        return references
    datums = {item.get("vertical_datum") for item in manifest.get("terrain_sources", [])
              if item.get("vertical_datum") not in (None, "", "UNKNOWN")}
    if len(datums) == 1:
        datum = next(iter(datums))
        for obj in scene.objects:
            if (obj.get("swiss_gis_role") == "terrain" and "swiss_gis_terrain_vertical_datum" not in obj
                    and any(collection.get("swiss_gis_project") == scene.get("swiss_gis_bundle")
                            for collection in obj.users_collection)):
                references.append((obj, datum))
    return references


def build(context, folder, radius=.025):
    existing_project = scene_project_id(context.scene)
    saved_contract = _scene_contract(context.scene)
    folder = Path(folder).resolve()
    manifest = load_manifest(folder)
    ifc_crs = context.scene.get("railway_ifc_horizontal_crs", "UNKNOWN")
    if isinstance(ifc_crs, str):
        ifc_crs = ifc_crs.strip().upper()
    if ifc_crs not in (None, "", "UNKNOWN") and ifc_crs != f"EPSG:{manifest['epsg']}":
        raise DataError("This bundle uses another horizontal CRS. Open it in a new scene.")
    contract = manifest.get("project_contract")
    if contract is not None:
        incoming_project = _contract_project_id(contract)
        if existing_project is not None and existing_project != incoming_project:
            raise DataError("This bundle has another project identity. Open it in a new scene.")
    merged_contract = _merge_project_contract(saved_contract, contract)
    origin = manifest["origin"]
    existing = scene_origin(context.scene)
    if existing and not np.allclose(existing, origin, atol=1e-6, rtol=0):
        raise DataError("This bundle uses another scene origin. Prepare it again in this scene or open a new scene.")
    scene_datum = context.scene.get("swiss_gis_vertical_datum", "UNKNOWN")
    incoming_datum = manifest["vertical_datum"]
    project_datum = (merged_contract["reference"]["vertical"].get("datum", "UNKNOWN")
                     if merged_contract is not None else "UNKNOWN")
    terrain_datums = [source.get("vertical_datum", "UNKNOWN") for source in manifest["terrain_sources"]]
    terrain_references = _scene_terrain_references(context.scene)
    project_datums = {value for value in (scene_datum, incoming_datum, project_datum) if value not in (None, "", "UNKNOWN")}
    retained_datums = [datum for obj, datum in terrain_references]
    retained_datums.append(context.scene.get("railway_ifc_vertical_datum", "UNKNOWN"))
    known_datums = project_datums | {value for value in (*terrain_datums, *retained_datums) if value not in (None, "", "UNKNOWN")}
    if len(known_datums) > 1:
        raise DataError("This bundle uses another height datum. Open it in a new scene.")
    if merged_contract is not None:
        manifest["project_contract"] = merged_contract
    # Validate all arrays before touching the scene.
    arrays = {}
    for name in ("scan", "ground", "terrain"):
        with np.load(folder / f"{name}.npz", allow_pickle=False) as source:
            arrays[name] = {key: source[key] for key in source.files}
    scan = arrays["scan"]
    if scan["points"].shape != (manifest["preview_points"], 3) or scan["rgb"].shape != scan["points"].shape:
        raise DataError("The scan bundle has inconsistent point arrays.")
    for name in ("ground", "terrain"):
        vertices, faces = arrays[name]["vertices"], arrays[name]["faces"]
        if vertices.ndim != 2 or vertices.shape[1] != 3 or faces.ndim != 2 or faces.shape[1] != 4:
            raise DataError(f"Invalid {name} mesh arrays.")
        if not np.isfinite(vertices).all() or (faces.size and (faces.min() < 0 or faces.max() >= len(vertices))):
            raise DataError(f"Invalid {name} mesh coordinates.")
    if not np.isfinite(scan["points"]).all():
        raise DataError("The scan bundle contains invalid coordinates.")
    scene = context.scene
    collection = bpy.data.collections.new("Swiss GIS, local railway context")
    collection["swiss_gis_project"] = str(folder)
    scene.collection.children.link(collection)
    try:
        obj = mesh_object(collection, "Railway scan, local preview", scan["points"], [], origin, "scan")
        set_colors(obj, scan["rgb"])
        obj.data.attributes.new("scan_class", "INT", "POINT").data.foreach_set("value", scan["classification"].astype(np.int32))
        for name, values in (("source_id_low", scan["source_ids"] & 0xffffffff), ("source_id_high", scan["source_ids"] >> 32)):
            obj.data.attributes.new(name, "INT", "POINT").data.foreach_set("value", values.astype(np.int32))
        obj.data.materials.append(vertex_material("Measured scan RGB"))
        point_display(obj, radius)
        obj["swiss_gis_source"] = manifest["source"]["path"]
        obj["swiss_gis_source_point_count"] = manifest["source"]["point_count"]
        obj["swiss_gis_preview_stride"] = manifest["preview_stride"]
        ground = arrays["ground"]
        if len(ground["faces"]):
            terrain = mesh_object(collection, "Observed scan ground, class 2", ground["vertices"], ground["faces"], origin, "scan_ground")
            set_colors(terrain, ground["rgb"])
            terrain.data.materials.append(vertex_material("Observed ground RGB"))
            terrain.hide_set(True)
            terrain.hide_render = True
        terrain = arrays["terrain"]
        aerial = folder / "aerial.png"
        if len(terrain["faces"]):
            terrain_obj = mesh_object(collection, "Local terrain GeoTIFF", terrain["vertices"], terrain["faces"], origin, "terrain")
            terrain_obj["swiss_gis_terrain_vertical_datum"] = next(
                (value for value in terrain_datums if value not in (None, "", "UNKNOWN")), "UNKNOWN")
            terrain_obj.data.materials.append(material("Local terrain", (.36, .42, .29)))
            if aerial.is_file():
                texture_terrain(terrain_obj, aerial, manifest["aerial_area"], origin)
        plan = image_plane(collection, "Scan RGB plan, observed pixels only", folder / "scan_plan.png",
                           manifest["scan_plan_area"], origin, manifest["source"]["bounds"][0][2] - .5, "scan_plan")
        plan["description"] = "Topmost scan RGB per cell. This is not aerial imagery."
        plan.hide_set(True)
        plan.hide_render = True
        if aerial.is_file():
            plane = image_plane(collection, "Local aerial map", aerial, manifest["aerial_area"], origin,
                                manifest["source"]["bounds"][0][2] - 1, "aerial_map")
            if len(terrain["faces"]):
                plane.hide_set(True)
                plane.hide_render = True
        area = manifest["area"]
        bounds = np.array([[area[0]-origin[0], area[1]-origin[1], 0], [area[2]-origin[0], area[1]-origin[1], 0],
                           [area[2]-origin[0], area[3]-origin[1], 0], [area[0]-origin[0], area[3]-origin[1], 0]])
        outline = mesh_object(collection, "Selected LV95 area", bounds, [], origin, "area")
        outline.data.clear_geometry()
        outline.data.from_pydata(bounds.tolist(), [(0, 1), (1, 2), (2, 3), (3, 0)], [])
        outline.data.update()
        outline.show_in_front = True
        outline.hide_render = True
        scene.unit_settings.system = "METRIC"
        scene.unit_settings.scale_length = 1
        scene.unit_settings.length_unit = "METERS"
        scene["swiss_gis_origin_json"] = json.dumps(origin)
        if manifest.get("project_contract"):
            scene["railway_project_contract_json"] = json.dumps(manifest["project_contract"])
        scene["swiss_gis_epsg"] = 2056
        scene["swiss_gis_vertical_datum"] = next(iter(project_datums), "UNKNOWN")
        scene["swiss_gis_manifest_json"] = json.dumps(manifest)
        scene["swiss_gis_bundle"] = str(folder)
        references = sorted({item["source_reference"] for item in manifest["terrain_sources"] + manifest["imagery_sources"]
                             if item.get("source_reference")})
        scene["swiss_gis_source_references_json"] = json.dumps(references)
        if "scan_scene_origin" not in scene:
            scene["scan_scene_origin"] = origin
        for selected in context.selected_objects:
            selected.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        context.view_layer.update()
        frame_area(context, manifest)
        for terrain_obj, datum in terrain_references:
            terrain_obj["swiss_gis_terrain_vertical_datum"] = datum
        return manifest
    except Exception:
        for obj in list(collection.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.collections.remove(collection)
        raise


def export_reference(scene, filepath):
    if not scene.get("swiss_gis_manifest_json"):
        raise DataError("Build a Swiss GIS scene before exporting its reference.")
    manifest = json.loads(scene["swiss_gis_manifest_json"])
    reference = {
        "epsg": 2056, "origin": manifest["origin"], "vertical_datum": manifest["vertical_datum"],
        "vertical_datum_declared": manifest["vertical_datum_declared"],
        "crs_assumed": manifest["source"]["crs_assumed"], "units": "metres",
        "axes": manifest["axes"], "coordinate_rule": manifest["coordinate_rule"],
        "source": manifest["source"]["path"], "area": manifest["area"],
        "warnings": manifest["warnings"],
        "context_sources": manifest["terrain_sources"] + manifest["imagery_sources"],
    }
    Path(filepath).write_text(json.dumps(reference, indent=2, allow_nan=False) + "\n")
    return reference
