"""Check saved authoring links and a Bonsai mesh edit across an IFC reimport.

Run each stage in a separate Blender process. The edit stage writes a proof IFC
beside the deliverable. It never writes the primary IFC or Blender workspace.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

import addon_utils
import bpy
import numpy as np
from mathutils import Vector


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = [2689160.0, 1254972.0, 452.0]
SCAN_NAME = "Source scan, full 5,500,234 points"
EDIT_DELTA_M = 0.025
IMPORT_BOUNDS_TOLERANCE_M = 0.001


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("saved", "edit", "reimport"), default="saved")
    parser.add_argument("--blend", type=Path, default=ROOT / "output/railway_crop.blend")
    parser.add_argument("--ifc", type=Path, default=ROOT / "output/railway_crop.ifc")
    parser.add_argument("--proof-ifc", type=Path, default=ROOT / "output/railway_crop.edit_proof.ifc")
    parser.add_argument("--reconstruction", type=Path, default=ROOT.parent / "infrastructure/reconstruction")
    return parser.parse_args(sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else [])


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def ensure_bonsai() -> None:
    if not hasattr(bpy.types.Scene, "BIMProperties"):
        addon_utils.enable("bl_ext.user_default.bonsai", default_set=False, persistent=False)
    if not hasattr(bpy.types.Scene, "BIMProperties"):
        raise RuntimeError("Bonsai is unavailable.")


def world_vertices(obj: bpy.types.Object) -> np.ndarray:
    coords = np.empty((len(obj.data.vertices), 3), np.float32)
    obj.data.vertices.foreach_get("co", coords.ravel())
    transform = np.asarray(obj.matrix_world, dtype=np.float64)
    return coords.astype(np.float64) @ transform[:3, :3].T + transform[:3, 3]


def bounds(coords: np.ndarray) -> np.ndarray:
    if len(coords) == 0 or not np.isfinite(coords).all():
        raise RuntimeError("Expected nonempty finite geometry.")
    return np.array([coords.min(axis=0), coords.max(axis=0)])


def ifc_bounds(product) -> np.ndarray:
    import ifcopenshell.geom

    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    settings.set("dimensionality", ifcopenshell.ifcopenshell_wrapper.CURVES_SURFACES_AND_SOLIDS)
    shape = ifcopenshell.geom.create_shape(settings, product)
    return bounds(np.asarray(shape.geometry.verts, dtype=np.float64).reshape(-1, 3))


def product_inventory(model) -> dict[str, dict[str, str | None]]:
    return {
        p.GlobalId: {"class": p.is_a(), "name": p.Name, "tag": getattr(p, "Tag", None)}
        for p in model.by_type("IfcProduct")
    }


def check_saved(args: argparse.Namespace) -> dict:
    import bonsai.tool as tool
    import ifcopenshell

    bpy.ops.wm.open_mainfile(filepath=str(args.blend.resolve()))
    scene = bpy.context.scene
    model = tool.Ifc.get()
    if model is None:
        raise RuntimeError("Bonsai did not reopen the saved workspace's IFC project.")
    expected = ifcopenshell.open(str(args.ifc.resolve()))
    assert model.schema_identifier == "IFC4X3_ADD2"
    assert product_inventory(model) == product_inventory(expected), "IFC products changed on workspace reopening."
    assert Path(tool.Ifc.get_path()).resolve() == args.ifc.resolve(), "Workspace opened a different IFC."
    assert scene.unit_settings.system == "METRIC" and scene.unit_settings.scale_length == 1.0
    assert list(scene.get("LV95_origin_m", [])) == ORIGIN
    assert {"BIM model", "Scan overlay", "Scan reference"}.issubset(scene.view_layers.keys())

    scan = bpy.data.objects.get(SCAN_NAME)
    assert scan is not None and scan.type == "MESH", "Saved source scan is missing."
    with np.load(args.reconstruction / "work/source.npz") as source:
        xyz = source["xyz"]
        rgb = source["rgb"]
        assert len(scan.data.vertices) == len(xyz) == 5_500_234
        stored = np.empty((len(xyz), 3), np.float32)
        scan.data.vertices.foreach_get("co", stored.ravel())
        assert np.array_equal(stored, xyz), "At least one source point changed."
        assert np.allclose(np.asarray(scan.matrix_world), np.eye(4), atol=0, rtol=0)
        colors = scan.data.color_attributes.get("Scan RGB")
        assert colors and colors.domain == "POINT" and len(colors.data) == len(rgb)
        rgba = np.empty((len(rgb), 4), np.float32)
        colors.data.foreach_get("color_srgb", rgba.ravel())
        expected_rgb = np.rint(rgb.astype(np.float64) / 65535.0 * 255.0) / 255.0
        rgb_error = float(np.max(np.abs(rgba[:, :3] - expected_rgb)))
        assert rgb_error < 1e-6, f"Source colours changed: {rgb_error}"
        coordinate_sha = hashlib.sha256(stored.tobytes()).hexdigest()

    represented = [p for p in model.by_type("IfcProduct") if p.Representation]
    failures = []
    mesh_count = 0
    checked_classes = set()
    import_bounds_differences = {}
    for product in represented:
        obj = tool.Ifc.get_object(product)
        if obj is None or tool.Ifc.get_entity(obj) != product:
            failures.append(product.GlobalId)
            continue
        if obj.type == "MESH":
            mesh_count += 1
            imported = world_vertices(obj)
            if not np.isfinite(imported).all():
                failures.append(product.GlobalId)
            if product.is_a() not in checked_classes:
                # Circular sweeps use different tessellation settings in Bonsai and create_shape.
                deviation = float(np.max(np.abs(bounds(imported) - ifc_bounds(product))))
                assert deviation <= IMPORT_BOUNDS_TOLERANCE_M, (
                    f"Imported engineering coordinates differ from IFC geometry: {product.Name}"
                )
                import_bounds_differences[product.is_a()] = deviation
                checked_classes.add(product.is_a())
    assert not failures, f"Saved IFC links or geometry are invalid: {failures}"
    assert mesh_count > 0
    return {
        "ok": True,
        "stage": "saved",
        "blend": str(args.blend.resolve()),
        "ifc_path": tool.Ifc.get_path(),
        "schema": model.schema_identifier,
        "blender_version": bpy.app.version_string,
        "bonsai_version": tool.Blender.get_bonsai_version(),
        "source_points": len(scan.data.vertices),
        "source_coordinates_exact": True,
        "source_coordinate_sha256": coordinate_sha,
        "source_rgb_preserved": True,
        "maximum_rgb_error": rgb_error,
        "source_matrix_identity": True,
        "represented_products": len(represented),
        "linked_meshes": mesh_count,
        "coordinate_import_checked_classes": sorted(checked_classes),
        "maximum_import_bounds_difference_by_class_m": import_bounds_differences,
        "import_bounds_tolerance_m": IMPORT_BOUNDS_TOLERANCE_M,
        "product_classes": dict(sorted(Counter(p.is_a() for p in represented).items())),
        "view_layers": list(scene.view_layers.keys()),
        "origin_m": ORIGIN,
    }


def edit_proof(args: argparse.Namespace) -> dict:
    import bonsai.tool as tool
    import ifcopenshell

    if args.proof_ifc.resolve() == args.ifc.resolve():
        raise ValueError("Edit proof must use a separate IFC path.")
    saved = check_saved(args)
    model = tool.Ifc.get()
    original_sha = digest(args.ifc)
    original_blend_sha = digest(args.blend)
    inventory = product_inventory(model)
    candidates = [
        p for p in model.by_type("IfcElement")
        if p.Representation and "mast" in (p.Name or "").lower()
    ]
    if not candidates:
        raise RuntimeError("No represented mast is available for the edit proof.")
    candidates.sort(key=lambda p: (getattr(p, "Tag", "") or "", p.Name or "", p.GlobalId))
    product = candidates[0]
    obj = tool.Ifc.get_object(product)
    if obj is None or obj.type != "MESH":
        raise RuntimeError("The proof mast is not an editable Bonsai mesh.")
    before = ifc_bounds(product)
    assert np.allclose(bounds(world_vertices(obj)), before, atol=2e-5, rtol=0)
    other = candidates[1] if len(candidates) > 1 else None
    other_before = ifc_bounds(other) if other else None
    obj.data = obj.data.copy()
    threshold = float((before[0, 2] + before[1, 2]) / 2)
    selected_vertices = np.flatnonzero(world_vertices(obj)[:, 2] > threshold)
    assert len(selected_vertices) > 0, "Mast has no top vertices."
    local_delta = obj.matrix_world.inverted().to_3x3() @ Vector((0, 0, EDIT_DELTA_M))
    for index in selected_vertices:
        obj.data.vertices[int(index)].co += local_delta
    obj.data.update()
    for selected in bpy.context.selected_objects:
        selected.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.context.view_layer.update()
    assert bpy.ops.bim.update_representation(
        obj=obj.name, ifc_representation_class="IfcTessellatedFaceSet"
    ) == {"FINISHED"}
    assert bpy.ops.bim.save_project(
        filepath=str(args.proof_ifc.resolve()), should_save_as=True, skip_recent=True
    ) == {"FINISHED"}

    exported = ifcopenshell.open(str(args.proof_ifc))
    after = ifc_bounds(exported.by_guid(product.GlobalId))
    expected_after = before.copy()
    expected_after[1, 2] += EDIT_DELTA_M
    assert np.allclose(after, expected_after, atol=2e-5, rtol=0), "Bonsai export lost the mesh edit."
    assert product_inventory(exported) == inventory, "Product GUIDs, classes, or names changed."
    if other:
        assert np.allclose(ifc_bounds(exported.by_guid(other.GlobalId)), other_before, atol=1e-8, rtol=0)
    assert digest(args.ifc) == original_sha, "The edit proof changed the primary IFC."
    assert digest(args.blend) == original_blend_sha, "The edit proof changed the primary workspace."
    return {
        "ok": True,
        "stage": "edit",
        "proof_ifc": str(args.proof_ifc.resolve()),
        "primary_ifc_sha256": original_sha,
        "primary_blend_sha256": original_blend_sha,
        "edited_guid": product.GlobalId,
        "edited_name": product.Name,
        "edit": "Raise the mast's upper mesh vertices by 0.025 metres through Bonsai update_representation.",
        "selected_vertices": int(len(selected_vertices)),
        "before_bounds_m": before.tolist(),
        "expected_after_bounds_m": expected_after.tolist(),
        "exported_bounds_m": after.tolist(),
        "other_mast_guid": other.GlobalId if other else None,
        "other_mast_unchanged": other is not None,
        "all_product_guids_preserved": True,
        "product_inventory": inventory,
        "saved_workspace_checked": saved["ok"],
        "independent_reimport_pending": True,
    }


def reimport_proof(args: argparse.Namespace) -> dict:
    import bonsai.tool as tool

    evidence = json.loads(args.proof_ifc.with_suffix(".json").read_text())
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    assert bpy.ops.bim.load_project(
        filepath=str(args.proof_ifc.resolve()), should_start_fresh_session=False, skip_recent=True
    ) == {"FINISHED"}
    model = tool.Ifc.get()
    product = model.by_guid(evidence["edited_guid"])
    obj = tool.Ifc.get_object(product)
    assert obj and obj.type == "MESH" and tool.Ifc.get_entity(obj) == product
    imported_bounds = bounds(world_vertices(obj))
    assert np.allclose(imported_bounds, evidence["expected_after_bounds_m"], atol=2e-5, rtol=0)
    assert product_inventory(model) == evidence["product_inventory"]
    assert digest(args.ifc) == evidence["primary_ifc_sha256"]
    assert digest(args.blend) == evidence["primary_blend_sha256"]
    return {
        "ok": True,
        "stage": "reimport",
        "proof_ifc": str(args.proof_ifc.resolve()),
        "edited_guid": product.GlobalId,
        "imported_bounds_m": imported_bounds.tolist(),
        "mesh_edit_survived_separate_process_reimport": True,
        "all_product_guids_preserved": True,
        "primary_ifc_unchanged": True,
        "primary_blend_unchanged": True,
        "blender_version": bpy.app.version_string,
        "bonsai_version": tool.Blender.get_bonsai_version(),
    }


def main() -> None:
    args = arguments()
    ensure_bonsai()
    if args.stage == "saved":
        receipt = check_saved(args)
        target = args.blend.with_suffix(".check.json")
    elif args.stage == "edit":
        receipt = edit_proof(args)
        target = args.proof_ifc.with_suffix(".json")
    else:
        receipt = reimport_proof(args)
        target = args.proof_ifc.with_suffix(".reimport.json")
    target.write_text(json.dumps(receipt, indent=2) + "\n")
    summary = {key: value for key, value in receipt.items() if key != "product_inventory"}
    print("RAILWAY_BIM_CHECK", json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
