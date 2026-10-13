"""Reopen railway IFC exports and check structure, geometry, and source evidence."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from pathlib import Path
import uuid

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element
import ifcopenshell.util.geolocation
import ifcopenshell.util.placement
import ifcopenshell.validate
import numpy as np

from .contract import legacy_reference, resolve_reference, validate_contract


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _root_path(entity):
    """Follow real decomposition relationships and reject cycles."""
    result, seen = [], set()
    while entity is not None:
        if entity.id() in seen:
            return result, True
        result.append(entity)
        seen.add(entity.id())
        parents = list(getattr(entity, "Decomposes", ()) or ())
        if not parents:
            parents = list(getattr(entity, "Nests", ()) or ())
        entity = parents[0].RelatingObject if len(parents) == 1 else None
    return result, False


def _expected_asset_ids(spec, contract=None):
    ids = set()
    for group in ("rails", "sleepers", "masts", "supports", "droppers", "troughs", "counterweights"):
        ids.update(item["id"] for item in spec.get(group, []))
    for wire in spec.get("wires", []):
        offsets = wire.get("strand_offsets_vz_m", [[0, 0]])
        if len(offsets) > 1:
            ids.update(f"{wire['id']} strand {i + 1}" for i in range(len(offsets)))
        else:
            ids.add(wire["id"])
    if spec.get("terrain"):
        ids.update(region["id"] for region in contract["terrain_regions"])
    return ids


def _rail_section_errors(vertices, parameters, basis, sample_count):
    """Recover saved cross-sections without depending on exporter vertex order."""
    start, end = parameters["range_u"]
    span = end - start
    if not math.isfinite(span) or span <= 0:
        raise ValueError("Rail needs a finite fitted interval")
    if sample_count is None:
        sample_count = max(2, math.ceil(span / .2) + 1)
    if isinstance(sample_count, bool) or not isinstance(sample_count, int) or not 2 <= sample_count <= 100_000:
        raise ValueError("Rail has invalid recorded sampling")
    uv = np.linalg.solve(basis, vertices[:, :2].T).T
    stations = uv[:, 0].copy()
    lateral_coefficients = parameters["v_coefficients"]
    first = np.polyder(lateral_coefficients)
    second = np.polyder(first)
    for _ in range(12):
        lateral = np.polyval(lateral_coefficients, stations)
        slope = np.polyval(first, stations)
        denominator = 1 + slope ** 2 + (lateral - uv[:, 1]) * np.polyval(second, stations)
        if not np.isfinite(denominator).all() or np.any(np.abs(denominator) < 1e-12):
            raise ValueError("Rail section projection is singular or non-finite")
        change = ((stations - uv[:, 0]) + (lateral - uv[:, 1]) * slope) / denominator
        stations -= change
        if not np.isfinite(stations).all():
            raise ValueError("Rail section projection produces non-finite stations")
        if np.max(np.abs(change)) < 1e-10:
            break
    else:
        raise ValueError("Rail section projection did not converge")
    expected = np.linspace(start, end, sample_count)
    indices = np.clip(np.rint((stations - start) * (sample_count - 1) / span), 0, sample_count - 1).astype(int)
    station_error = float(np.max(np.abs(stations - expected[indices])))
    slope = np.polyval(first, stations)
    lateral_offset = (-(uv[:, 0] - stations) * slope
                      + uv[:, 1] - np.polyval(lateral_coefficients, stations)) / np.sqrt(1 + slope ** 2)
    lower, upper = np.full(sample_count, np.inf), np.full(sample_count, -np.inf)
    highest = np.full(sample_count, -np.inf)
    np.minimum.at(lower, indices, lateral_offset)
    np.maximum.at(upper, indices, lateral_offset)
    np.maximum.at(highest, indices, vertices[:, 2])
    present = np.isfinite(highest)
    centre_errors = np.hypot((lower[present] + upper[present]) / 2,
                            highest[present] - np.polyval(parameters["z_coefficients"], expected[present]))
    if not np.isfinite(centre_errors).all():
        raise ValueError("Rail section comparison produces non-finite errors")
    head_width = parameters["head_width_m"]
    if isinstance(head_width, bool) or not isinstance(head_width, (int, float)) or not math.isfinite(head_width) or head_width <= 0:
        raise ValueError("Rail needs a positive finite head width")
    # The recorded profile has head flanks 10 to 36 mm below its crown.
    # Exclude the narrower crown and the wider foot when measuring head width.
    depth_below_top = highest[indices] - vertices[:, 2]
    head = (depth_below_top >= .010 - 1e-5) & (depth_below_top <= .036 + 1e-5)
    head_lower, head_upper = np.full(sample_count, np.inf), np.full(sample_count, -np.inf)
    np.minimum.at(head_lower, indices[head], lateral_offset[head])
    np.maximum.at(head_upper, indices[head], lateral_offset[head])
    if not np.isfinite(head_lower).all() or not np.isfinite(head_upper).all():
        raise ValueError("Rail needs measured head flanks at every recorded section")
    head_width_error = float(np.max(np.abs(head_upper - head_lower - head_width)))
    if not math.isfinite(head_width_error):
        raise ValueError("Rail head width comparison produces non-finite errors")
    crown = depth_below_top <= 1e-5
    crown_lower, crown_upper = np.full(sample_count, np.inf), np.full(sample_count, -np.inf)
    np.minimum.at(crown_lower, indices[crown], lateral_offset[crown])
    np.maximum.at(crown_upper, indices[crown], lateral_offset[crown])
    if not np.isfinite(crown_lower).all() or not np.isfinite(crown_upper).all():
        raise ValueError("Rail needs measured crown vertices at every recorded section")
    head_centre_error = float(max(np.max(np.abs((head_lower + head_upper) / 2)),
                                  np.max(np.abs((crown_lower + crown_upper) / 2))))
    if not math.isfinite(head_centre_error):
        raise ValueError("Rail head position comparison produces non-finite errors")
    return {"max_cross_section_error_m": float(centre_errors.max()), "station_error_m": station_error,
            "missing_sections": int(np.count_nonzero(~present)), "max_head_width_error_m": head_width_error,
            "max_head_centre_error_m": head_centre_error}


def validate_ifc(ifc_path, reconstruction_dir=None, *, report_path=None, expected_asset_count=None):
    """Return serializable checks; unresolved survey facts produce ``review``.

    Geometry checks compare the exported representation with its recorded
    parameters. Unedited parameters retain their fitted-source meaning; edits
    remain a scan-fit review item. These checks do not establish accuracy
    against survey control or the raw point cloud.
    """
    ifc_path = Path(ifc_path).resolve()
    report = {
        "schema": "railway-bim-validation/1",
        "ifc_path": str(ifc_path),
        "status": "failed",
        "checks": [],
        "errors": [],
        "review_items": [],
        "counts": {},
        "geometry_bounds_local_m": None,
        "scope": "Export, IFC structure, fitted-source coordinates, and provenance; survey acceptance is separate.",
    }

    def check(name, passed, detail):
        report["checks"].append({"name": name, "status": "passed" if passed else "failed", "detail": detail})
        if not passed:
            report["errors"].append({"check": name, "detail": detail})

    def review(code, detail):
        report["review_items"].append({"code": code, "detail": detail})

    def finish():
        report["ok"] = not report["errors"]
        report["status"] = "failed" if report["errors"] else "review" if report["review_items"] else "passed"
        if report_path is not None:
            path = Path(report_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        return report

    try:
        model = ifcopenshell.open(str(ifc_path))
        check("reopen", True, "Exported IFC reopened from disk.")
    except Exception as exc:
        check("reopen", False, str(exc))
        return finish()

    report["ifc_schema"] = model.schema_identifier
    check("ifc_4_3", model.schema_identifier.startswith("IFC4X3"), model.schema_identifier)
    if not model.schema_identifier.startswith("IFC4X3"):
        return finish()
    logger = ifcopenshell.validate.json_logger()
    try:
        ifcopenshell.validate.validate(model, logger, express_rules=True)
        failures = [item for item in logger.statements if item.get("level") in ("error", "critical")]
        check("express_and_schema", not failures, [
            {"message": item.get("message", ""), "instance": str(item.get("instance", "")),
             "attribute": str(item.get("attribute", ""))} for item in failures
        ])
    except Exception as exc:
        check("express_and_schema", False, str(exc))

    roots = model.by_type("IfcRoot")
    guids = [item.GlobalId for item in roots]
    duplicate_guids = [guid for guid, count in Counter(guids).items() if count > 1]
    check("unique_ifc_guids", all(guids) and not duplicate_guids, {"roots": len(roots), "duplicates": duplicate_guids})

    projects = model.by_type("IfcProject")
    railways = model.by_type("IfcRailway") if model.schema_identifier.startswith("IFC4X3") else []
    check("project_and_railway", len(projects) == 1 and len(railways) == 1,
          {"projects": len(projects), "railways": len(railways)})
    parts = model.by_type("IfcRailwayPart") if railways else []
    hierarchy_errors = []
    for entity in railways + parts:
        path, cycle = _root_path(entity)
        if cycle or not path[-1].is_a("IfcProject") or (entity in parts and not any(item.is_a("IfcRailway") for item in path)):
            hierarchy_errors.append(entity.GlobalId)
    check("spatial_hierarchy", bool(parts) and not hierarchy_errors,
          {"railway_parts": len(parts), "invalid_paths": hierarchy_errors})

    assets, alignments, asset_data, parameter_data = {}, {}, {}, {}
    duplicate_ids, provenance_errors = [], []
    for product in model.by_type("IfcProduct"):
        pset = ifcopenshell.util.element.get_psets(product).get("Pset_RailwayBIMAsset", {})
        asset_id = pset.get("AssetId")
        if not asset_id:
            continue
        target = alignments if product.is_a("IfcAlignment") else assets
        if asset_id in target:
            duplicate_ids.append(asset_id)
        target[asset_id] = product
        asset_data[asset_id] = pset
        for key in ("SourceComponentId", "SourceSHA256", "ParametersJSON", "ParameterStatusJSON", "SourceFile"):
            if not pset.get(key):
                provenance_errors.append(f"{asset_id}: missing {key}")
        for key in ("ParametersJSON", "ParameterStatusJSON"):
            try:
                value = json.loads(pset.get(key, ""))
                if not isinstance(value, dict):
                    raise ValueError("Expected an object")
                if key == "ParametersJSON":
                    parameter_data[asset_id] = value
            except (ValueError, TypeError):
                provenance_errors.append(f"{asset_id}: invalid {key}")
    check("persistent_asset_ids", bool(assets) and not duplicate_ids,
          {"assets": len(assets), "alignments": len(alignments), "duplicates": duplicate_ids})
    check("asset_provenance", not provenance_errors, provenance_errors)

    containment_errors = []
    for asset_id, product in assets.items():
        relationships = list(getattr(product, "ContainedInStructure", ()) or ())
        if len(relationships) != 1 or not relationships[0].RelatingStructure.is_a("IfcRailwayPart"):
            containment_errors.append(asset_id)
    check("asset_containment", not containment_errors, containment_errors)
    unregistered = [item.GlobalId for item in model.by_type("IfcElement") if item.Representation and item not in assets.values()]
    check("all_represented_assets_registered", not unregistered, unregistered)

    register_path = ifc_path.with_suffix(".asset-register.json")
    project_path = ifc_path.with_suffix(".railway-project.json")
    project = None
    register = None
    registered_alignments = {}
    try:
        project, register = _read_json(project_path), _read_json(register_path)
        registered = {item["asset_id"]: item for item in register["assets"]}
        mismatches = sorted(set(assets) ^ set(registered))
        mismatches.extend(asset_id for asset_id, product in assets.items()
                          if asset_id in registered and registered[asset_id]["ifc_guid"] != product.GlobalId)
        mismatches.extend(asset_id for asset_id in assets
                          if asset_id in registered and parameter_data.get(asset_id) != registered[asset_id]["parameters"])
        check("asset_register", not mismatches and len(registered) == len(register["assets"]), mismatches)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        check("asset_register", False, str(exc))

    try:
        records = register["alignments"]
        registered_alignments = {item["asset_id"]: item for item in records}
        mismatches = sorted(set(alignments) ^ set(registered_alignments))
        mismatches.extend(asset_id for asset_id, product in alignments.items()
                          if asset_id in registered_alignments and (
                              registered_alignments[asset_id]["ifc_guid"] != product.GlobalId
                              or registered_alignments[asset_id]["parameters"] != parameter_data.get(asset_id)))
        if project["alignments"] != records:
            mismatches.append("Project and register alignment records disagree")
        check("alignment_register", not mismatches and len(records) == len(registered_alignments), mismatches)
    except (KeyError, TypeError) as exc:
        check("alignment_register", False, str(exc))

    source_spec = source_header = alignment = contract = None
    reference = None
    register_project_id = None
    if project is not None:
        try:
            source_spec, source_header, alignment = (project["source_component_spec"],
                                                      project["source_header"], project["alignment"])
            if project.get("contract"):
                contract = validate_contract(project["contract"], source_spec, source_header)
                try:
                    register_project_id = str(uuid.UUID(register["project_id"]))
                except (AttributeError, KeyError, TypeError, ValueError):
                    register_project_id = None
                check("project_contract", project.get("schema") == "railway-bim-project/2"
                      and isinstance(register, dict) and register.get("schema") == "railway-bim-asset-register/2"
                      and register_project_id == contract["project_id"],
                      {"project_id": contract["project_id"], "register_project_id": register_project_id})
            elif project.get("schema") == "railway-bim-project/1":
                from .pilot import import_swiss_crop

                contract = import_swiss_crop(source_spec, source_header)
            else:
                raise ValueError("Unsupported reconstruction sidecar schema")
            project_metadata = (ifcopenshell.util.element.get_psets(projects[0]).get("Pset_RailwayBIMProject", {})
                                if len(projects) == 1 else {})
            recorded_id = project_metadata.get("ProjectId")
            try:
                ifc_project_id = str(uuid.UUID(recorded_id))
            except (AttributeError, TypeError, ValueError):
                ifc_project_id = None
            identity_required = project.get("schema") == "railway-bim-project/2"
            identity_matches = (ifc_project_id == contract["project_id"]
                                and (not identity_required or register_project_id == ifc_project_id))
            check("ifc_project_identity", len(projects) == 1
                  and (identity_matches or not identity_required and recorded_id is None),
                  {"ifc_project_id": ifc_project_id, "contract_project_id": contract["project_id"],
                   "register_project_id": register_project_id, "required": identity_required})
            reference = contract["reference"]
            if reconstruction_dir is not None:
                root = Path(reconstruction_dir).resolve()
                work = root if (root / "component_spec.json").is_file() else root / "work"
                external_spec = _read_json(work / "component_spec.json")
                external_header = _read_json(work / "source_header.json")
                external_alignment = _read_json(work / "alignment.json")
                check("external_source_evidence", external_spec == source_spec
                      and {key: value for key, value in external_header.items() if key != "source"}
                      == {key: value for key, value in source_header.items() if key != "source"}
                      and external_alignment == alignment, "Embedded fitted evidence compared with supplied inputs.")
            expected_ids = _expected_asset_ids(source_spec, contract)
            missing, unexpected = sorted(expected_ids - assets.keys()), sorted(assets.keys() - expected_ids)
            check("source_asset_inventory", not missing and not unexpected,
                  {"expected": len(expected_ids), "missing": missing, "unexpected": unexpected})
            source_hash_errors = [asset_id for asset_id, pset in asset_data.items()
                                  if pset.get("SourceSHA256") != source_header["sha256"]]
            check("source_fingerprint_metadata", not source_hash_errors
                  and register is not None and register.get("source_sha256") == source_header["sha256"], source_hash_errors)
            source_path = resolve_reference(ifc_path.parent, source_header["source"])
            if source_path.is_file():
                with source_path.open("rb") as source_file:
                    digest = hashlib.file_digest(source_file, "sha256").hexdigest()
                check("source_file_fingerprint", digest == source_header["sha256"], {"sha256": digest})
            else:
                review("source_file_unavailable", "The original crop is unavailable; its stored fingerprint cannot be checked.")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            check("source_reference", False, str(exc))
    else:
        review("source_reference_unavailable", "The delivery has no embedded fitted source evidence.")

    if expected_asset_count is not None:
        check("expected_asset_count", len(assets) == expected_asset_count,
              {"expected": expected_asset_count, "actual": len(assets)})
    report["counts"] = dict(Counter(item.is_a() for item in assets.values()))
    report["counts"].update(assets=len(assets), alignments=len(alignments))
    check("railway_classes", len(model.by_type("IfcRail")) == len(source_spec["rails"]) if source_spec else bool(model.by_type("IfcRail")),
          {"rails": len(model.by_type("IfcRail"))})
    sleepers = [item for item in model.by_type("IfcTrackElement") if item.PredefinedType == "SLEEPER"]
    if source_spec:
        check("sleeper_classification", len(sleepers) == len(source_spec.get("sleepers", [])),
              {"expected": len(source_spec.get("sleepers", [])), "actual": len(sleepers)})
    expected_alignments = len(contract["tracks"]) if contract else len(project.get("alignments", [])) if project else 0
    check("measured_alignments", bool(expected_alignments) and len(alignments) == expected_alignments,
          {"count": len(alignments), "expected": expected_alignments, "kind": "Measured rail-pair polylines."})

    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    settings.set(settings.DIMENSIONALITY, ifcopenshell.ifcopenshell_wrapper.CURVES_SURFACES_AND_SOLIDS)
    geometry, geometry_errors, minimum, maximum = {}, [], None, None
    for asset_id, product in {**assets, **alignments}.items():
        try:
            shape = ifcopenshell.geom.create_shape(settings, product)
            vertices = np.asarray(shape.geometry.verts, dtype=float).reshape(-1, 3)
            if not len(vertices) or not np.isfinite(vertices).all():
                raise ValueError("Geometry is empty or has non-finite coordinates.")
            if asset_id in assets and not len(shape.geometry.faces):
                raise ValueError("Physical asset has no tessellated faces.")
            geometry[asset_id] = vertices
            lo, hi = vertices.min(axis=0), vertices.max(axis=0)
            minimum = lo if minimum is None else np.minimum(minimum, lo)
            maximum = hi if maximum is None else np.maximum(maximum, hi)
        except Exception as exc:
            geometry_errors.append({"asset_id": asset_id, "message": str(exc)})
    check("tessellated_geometry", not geometry_errors, {"products": len(geometry), "failures": geometry_errors})
    if minimum is not None:
        report["geometry_bounds_local_m"] = [minimum.tolist(), maximum.tolist()]
        extent = reference.get("max_local_extent_m", 1000.0) if reference else 1000.0
        check("local_coordinates", bool(np.max(np.abs([minimum, maximum])) < extent),
              {"bounds": report["geometry_bounds_local_m"], "maximum_extent_m": extent})

    conversions = model.by_type("IfcMapConversion")
    check("map_conversion", len(conversions) == 1, {"count": len(conversions)})
    if source_header and len(conversions) == 1:
        reference = reference or legacy_reference(source_header)
        horizontal, vertical = reference["horizontal"], reference["vertical"]
        statuses = {asset_id: [pset.get("HorizontalCRSStatus"), pset.get("VerticalDatumStatus")]
                    for asset_id, pset in asset_data.items()
                    if pset.get("HorizontalCRSStatus") != horizontal["status"]
                    or pset.get("VerticalDatumStatus") != vertical["status"]}
        check("survey_reference_status", not statuses, statuses)
        crs = conversions[0].TargetCRS
        check("survey_reference_values", crs.Name == horizontal.get("crs", "UNKNOWN")
              and (vertical["status"] == "UNKNOWN" or crs.VerticalDatum == vertical.get("datum")),
              {"horizontal_crs": crs.Name, "vertical_datum": crs.VerticalDatum})
        samples = np.array([[0, 0, 0], [17.25, -9.5, 4.0], [-25.0, 12.75, -0.7]])
        expected = samples + np.asarray(source_header["origin"], dtype=float)
        mapped = np.array([ifcopenshell.util.geolocation.auto_xyz2enh(model, *point) for point in samples])
        restored = np.array([ifcopenshell.util.geolocation.auto_enh2xyz(model, *point) for point in mapped])
        error = float(np.max(np.abs(mapped - expected)))
        roundtrip = float(np.max(np.abs(restored - samples)))
        check("source_orientation_and_map_roundtrip", error < 1e-7 and roundtrip < 1e-7,
              {"offset_error_m": error, "roundtrip_error_m": roundtrip, "origin_m": source_header["origin"]})
        if minimum is not None:
            source_bounds = np.asarray(source_header["bounds_local"], dtype=float)
            check("source_crop_bounds", bool(np.all(minimum >= source_bounds[0] - 1.0)
                  and np.all(maximum <= source_bounds[1] + 1.0)),
                  {"model": report["geometry_bounds_local_m"], "source": source_bounds.tolist(), "margin_m": 1.0})

    if source_spec and alignment:
        basis = np.array([alignment["along"], alignment["across"]], dtype=float).T

        def source_xyz(points):
            points = np.asarray(points, dtype=float).reshape(-1, 3)
            return np.column_stack((points[:, :2] @ basis.T, points[:, 2]))

        rail_checks, rail_section_checks = [], []
        for rail in source_spec["rails"]:
            asset_id = rail["id"]
            if asset_id not in geometry:
                continue
            parameters = parameter_data.get(asset_id)
            if parameters is None:
                continue
            if parameters != rail:
                review("edited_source_parameters", f"{asset_id} has edited parameters. Review its fit against the scan.")
            us = np.asarray(parameters["range_u"])
            uv = np.column_stack((us, np.polyval(parameters["v_coefficients"], us)))
            points = np.column_stack((uv @ basis.T, np.polyval(parameters["z_coefficients"], us)))
            distances = [float(np.linalg.norm(geometry[asset_id] - point, axis=1).min()) for point in points]
            rail_checks.append({"asset_id": asset_id, "endpoint_to_model_vertex_m": distances})
            result = {"asset_id": asset_id, "passed": False}
            try:
                result.update(_rail_section_errors(geometry[asset_id], parameters, basis,
                              (contract or {}).get("sampling", {}).get("rail_sample_count")))
                result["passed"] = (result["max_cross_section_error_m"] <= 1e-5
                                    and result["station_error_m"] <= 1e-5 and not result["missing_sections"]
                                    and result["max_head_width_error_m"] <= 1e-5
                                    and result["max_head_centre_error_m"] <= 1e-5)
            except (KeyError, TypeError, ValueError, np.linalg.LinAlgError) as exc:
                result["message"] = str(exc)
            rail_section_checks.append(result)
        check("fitted_rail_endpoints", all(max(item["endpoint_to_model_vertex_m"]) <= 0.05 for item in rail_checks), rail_checks)
        check("fitted_rail_geometry", bool(rail_section_checks) and all(item["passed"] for item in rail_section_checks), rail_section_checks)

        alignment_checks = []
        rail_index = {rail["id"]: rail for rail in source_spec["rails"]}
        for track in contract["tracks"] if contract else []:
            result = {"track_id": track["id"], "passed": False}
            try:
                matches = [record for record in registered_alignments.values()
                           if str(record["parameters"]["track"]) == track["id"]]
                if len(matches) != 1:
                    raise ValueError("Track needs exactly one recorded measured alignment")
                record = matches[0]
                asset_id = record["asset_id"]
                result["asset_id"] = asset_id
                rails = [rail_index[identity] for identity in track["rail_ids"]]
                if record["parameters"]["rails"] != rails:
                    raise ValueError("Recorded alignment rail pair differs from fitted source evidence")
                product = alignments[asset_id]
                axes = [representation for representation in product.Representation.Representations
                        if representation.RepresentationIdentifier == "Axis"]
                if len(axes) != 1 or len(axes[0].Items) != 1 or not axes[0].Items[0].is_a("IfcPolyline"):
                    raise ValueError("Measured alignment needs its recorded polyline representation")
                points = np.asarray([point.Coordinates for point in axes[0].Items[0].Points], dtype=float)
                if points.ndim != 2 or points.shape[1] != 3 or len(points) < 2 or not np.isfinite(points).all():
                    raise ValueError("Measured alignment has invalid path coordinates")
                placement = ifcopenshell.util.placement.get_local_placement(product.ObjectPlacement)
                points = (placement @ np.column_stack((points, np.ones(len(points)))).T).T[:, :3]
                if not np.isfinite(points).all():
                    raise ValueError("Measured alignment placement produces non-finite coordinates")
                us = np.linalg.solve(basis, points[:, :2].T)[0]
                differences = np.diff(us)
                if np.all(differences < 0):
                    points, us = points[::-1], us[::-1]
                elif not np.all(differences > 0):
                    raise ValueError("Measured alignment does not follow its fitted interval in one direction")
                start, end = max(rail["range_u"][0] for rail in rails), min(rail["range_u"][1] for rail in rails)
                span = end - start
                if not math.isfinite(span) or span <= 0:
                    raise ValueError("Measured alignment has no finite common fitted interval")
                sample_count = contract.get("sampling", {}).get("alignment_sample_count")
                if sample_count is None:
                    sample_count = max(2, math.ceil(span / .2) + 1)
                if isinstance(sample_count, bool) or not isinstance(sample_count, int) or not 2 <= sample_count <= 100_000:
                    raise ValueError("Measured alignment has invalid recorded sampling")
                if len(points) != sample_count:
                    raise ValueError("Measured alignment polyline does not retain its recorded sampling count")
                station_error = float(np.max(np.abs(us - np.linspace(start, end, sample_count))))

                # Keep this source comparison independent of the curve exporter.
                def rail_pair_xyz(values):
                    lateral = sum(np.polyval(rail["v_coefficients"], values) for rail in rails) / 2
                    height = sum(np.polyval(rail["z_coefficients"], values) for rail in rails) / 2
                    return np.column_stack((np.column_stack((values, lateral)) @ basis.T, height))

                error = float(np.linalg.norm(points - rail_pair_xyz(us), axis=1).max())
                endpoint_error = float(np.linalg.norm(points[[0, -1]] - rail_pair_xyz([start, end]), axis=1).max())
                if not np.isfinite([error, endpoint_error]).all():
                    raise ValueError("Measured alignment comparison exceeds finite coordinates")
                result.update(max_path_error_m=error, endpoint_error_m=endpoint_error, station_error_m=station_error,
                              passed=error <= 1e-5 and endpoint_error <= 1e-5 and station_error <= 1e-5)
            except (AttributeError, KeyError, TypeError, ValueError, np.linalg.LinAlgError) as exc:
                result["message"] = str(exc)
            alignment_checks.append(result)
        check("fitted_measured_alignments", bool(alignment_checks) and all(item["passed"] for item in alignment_checks), alignment_checks)

        centre_checks, dimension_checks, anchor_checks = [], [], []
        for group in ("sleepers", "masts", "counterweights"):
            for item in source_spec.get(group, []):
                asset_id = item["id"]
                if asset_id not in geometry:
                    continue
                parameters = parameter_data.get(asset_id)
                if parameters is None:
                    continue
                if any(parameters.get(key) != value for key, value in item.items()):
                    review("edited_source_parameters", f"{asset_id} has edited parameters. Review its fit against the scan.")
                mid_z = ((parameters["z_bottom"] + parameters["z_top"]) / 2 if group == "masts"
                         else parameters["z_top"] - parameters["depth"] / 2)
                expected_centre = source_xyz([[parameters["u"], parameters["v"], mid_z]])[0]
                error = float(np.linalg.norm(geometry[asset_id].mean(axis=0) - expected_centre))
                centre_checks.append({"asset_id": asset_id, "centre_error_m": error})
                if group not in {"masts", "sleepers"}:
                    continue
                result = {"asset_id": asset_id, "passed": False}
                try:
                    vertices = geometry[asset_id]
                    if group == "masts":
                        expected_dimensions = {"height_m": parameters["z_top"] - parameters["z_bottom"]}
                        actual_dimensions = {"height_m": float(np.ptp(vertices[:, 2]))}
                    else:
                        tracks = [track for track in (contract or {}).get("tracks", [])
                                  if track["id"] == str(parameters["track"])]
                        if len(tracks) != 1:
                            raise ValueError("Sleeper needs one recorded track association")
                        rail = rail_index[tracks[0]["rail_ids"][0]]
                        slope = float(np.polyval(np.polyder(rail["v_coefficients"]), parameters["u"]))
                        tangent = np.array([1., slope]) / math.hypot(1., slope)
                        normal = np.array([-slope, 1.]) / math.hypot(1., slope)
                        uv = np.linalg.solve(basis, vertices[:, :2].T).T
                        along, across = uv @ tangent, uv @ normal
                        level_height = vertices[:, 2] - parameters["crossfall"] * across
                        expected_dimensions = {key: parameters[key] for key in ("width_u", "length_v", "depth")}
                        actual_dimensions = {"width_u": float(np.ptp(along)), "length_v": float(np.ptp(across)),
                                             "depth": float(np.ptp(level_height))}
                    errors = [abs(actual_dimensions[key] - value) for key, value in expected_dimensions.items()]
                    if not np.isfinite(list(actual_dimensions.values()) + errors).all():
                        raise ValueError("Recorded dimension comparison produces non-finite values")
                    result.update(expected_dimensions_m=expected_dimensions, actual_dimensions_m=actual_dimensions,
                                  max_dimension_error_m=max(errors), passed=max(errors) <= 1e-5)
                except (KeyError, TypeError, ValueError, np.linalg.LinAlgError) as exc:
                    result["message"] = str(exc)
                dimension_checks.append(result)
        check("fitted_asset_centres", all(item["centre_error_m"] < 1e-5 for item in centre_checks), centre_checks)
        check("fitted_asset_dimensions", all(item["passed"] for item in dimension_checks), dimension_checks)

        def anchor_check(asset_id, points, tolerance):
            if asset_id not in geometry:
                return
            distances = [float(np.linalg.norm(geometry[asset_id] - point, axis=1).min()) for point in source_xyz(points)]
            anchor_checks.append({"asset_id": asset_id, "endpoint_to_model_vertex_m": distances, "tolerance_m": tolerance})

        for wire in source_spec.get("wires", []):
            offsets = wire.get("strand_offsets_vz_m", [[0, 0]])
            for i, delta in enumerate(offsets):
                asset_id = f"{wire['id']} strand {i + 1}" if len(offsets) > 1 else wire["id"]
                points = np.asarray(wire["path_uvz"], dtype=float)[[0, -1]].copy()
                points[:, 1:] += delta
                anchor_check(asset_id, points, wire["radius_m"] + 1e-5)
        for item in source_spec.get("supports", []) + source_spec.get("droppers", []):
            anchor_check(item["id"], [item["start_uvz"], item["end_uvz"]], item["radius_m"] + 1e-5)
        for item in source_spec.get("troughs", []):
            anchor_check(item["id"], np.asarray(item["path_uvz"])[[0, -1]], item["width_m"] / 2 + 1e-5)
        check("fitted_wire_and_trough_endpoints",
              all(max(item["endpoint_to_model_vertex_m"]) <= item["tolerance_m"] for item in anchor_checks), anchor_checks)

    if reference:
        if reference["horizontal"]["status"] in {"ASSUMED", "UNKNOWN"}:
            review("horizontal_crs_" + reference["horizontal"]["status"].lower(),
                   "The horizontal CRS needs survey evidence. " + reference["horizontal"].get("evidence", ""))
        if reference["vertical"]["status"] == "UNKNOWN":
            review("vertical_datum_unknown", "The height datum is unknown. Map height retains the recorded source offset.")
        if reference.get("official_chainage", {}).get("status", "UNKNOWN") == "UNKNOWN":
            review("official_chainage_unknown", "Track distances follow measured alignments. No official route chainage was supplied.")
    if source_spec:
        for item in source_spec.get("assumptions", []):
            review("source_reconstruction_assumption", item)
    return finish()
