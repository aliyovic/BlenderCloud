"""Portable acceptance checks for project identity and relocatable IFC delivery.

Authoring gate: these tests exercise exported files through build, validation,
and edit APIs. They protect explicit track topology, stable asset identity when
an observation or display name changes, unique IDs across physical assets and
alignments, verified reference facts, and delivery without external inputs.
Shared labels and explicit alignment IDs can otherwise produce duplicate IFC
GUIDs. The pilot-only tests use fixed, distinct track labels and miss these
cases. Physical geometry checks must also survive supported dimension edits and
detect interior rail distortion with unchanged endpoints. Corrupting saved IFC
placement or coordinates, while retaining its sidecars, exposes those skipped
checks. Restoring an old rail width or centred mast/sleeper dimension exposes
geometry that disagrees with a supported edit despite matching metadata and
centres. Head and crown shifts can also preserve width and foot-centre checks;
saved lateral shifts protect the running head's fitted position. No test-only
production API is needed.

Alignment corruption checks protect the saved centreline and its recorded
identity. Moving the curve, bending an interior point, replacing its GUID or
removing interior stations must fail with matching alignment and asset counts.
Project identity must agree in the saved IFC and both sidecars. Changing only
the IFC UUID exposes a mismatch that asset identity and inventory checks miss;
equivalent UUID spellings retain the same project identity.
"""

import hashlib
import json
import shutil
import sys
from pathlib import Path

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from railway_bim.addon.core import build_ifc, rebuild_asset
from railway_bim.addon.validation import validate_ifc

PROJECT_ID = "fd50975a-e67e-4d99-b1e1-31460b477e58"


def fixture_project(root, track_count=2, observation=b"synthetic survey observation"):
    work = root / "work"
    work.mkdir(parents=True)
    scan = root / "fixture.scan"
    scan.write_bytes(observation)
    rails = []
    tracks = []
    sleepers = []
    for number in range(1, track_count + 1):
        rail_ids = []
        centre = (number - 1) * 3.0
        for side, offset in enumerate((-.75, .75), start=1):
            asset_id = f"Rail {number * 2 - 2 + side:02}"
            rail_ids.append(asset_id)
            rails.append({"id": asset_id, "v_coefficients": [centre + offset],
                          "z_coefficients": [1.0], "range_u": [0.0, 2.0], "head_width_m": .07})
        tracks.append({"id": str(number), "name": f"Track {number}", "rail_ids": rail_ids})
        sleepers.append({"id": f"Sleeper {number}", "track": number, "u": 1.0, "v": centre,
                         "z_top": .8, "crossfall": 0.0, "width_u": .2, "length_v": 2.4, "depth": .2})
    spec = {"rails": rails, "sleepers": sleepers,
            "masts": [{"id": "Mast 01", "u": 1.0, "v": -3.0, "z_bottom": 0.0,
                       "z_top": 4.0, "shaft_section_m": [.2, .2]}],
            "wires": [], "supports": [], "droppers": [], "counterweights": [],
            "troughs": [{"id": "Trough south", "path_uvz": [[0., -2., 0.], [2., -2., 0.]],
                        "width_m": .4, "depth_m": .2},
                       {"id": "Trough north", "path_uvz": [[0., 4., 0.], [2., 4., 0.]],
                        "width_m": .4, "depth_m": .2}],
            "terrain": {"grid_step_m": .5}, "assumptions": []}
    header = {"source": str(scan), "sha256": hashlib.sha256(observation).hexdigest(),
              "point_count": 1, "origin": [2689000., 1254000., 450.],
              "bounds_local": [[-10., -10., -2.], [10., 15., 6.]]}
    alignment = {"along": [1., 0.], "across": [0., 1.]}
    contract = {"schema": "railway-project-contract/1", "project_id": PROJECT_ID,
                "name": "Generated railway fixture", "units": "metre", "tracks": tracks,
                "reference": {"local_origin_m": header["origin"],
                              "horizontal": {"crs": "EPSG:2056", "status": "VERIFIED",
                                             "evidence": "Synthetic fixture CRS declaration"},
                              "vertical": {"datum": "LN02", "status": "VERIFIED",
                                           "evidence": "Synthetic fixture datum declaration"},
                              "official_chainage": {"status": "UNKNOWN"}},
                "terrain_regions": [
                    {"id": "Ballast and formation", "selector": {"kind": "lateral_range", "minimum_m": -2.4, "maximum_m": 4.4}},
                    {"id": "North embankment", "selector": {"kind": "lateral_range", "minimum_m": 4.4}},
                    {"id": "South embankment", "selector": {"kind": "remainder"}}]}
    for name, data in (("component_spec.json", spec), ("source_header.json", header), ("alignment.json", alignment)):
        (work / name).write_text(json.dumps(data))
    (root / "project.json").write_text(json.dumps(contract))
    np.savez(work / "terrain.npz", z=np.zeros((5, 40)), valid=np.ones((5, 40), dtype=bool),
             step=.5, u0=0., v0=-5.)
    return root


def _guids(path):
    model = ifcopenshell.open(str(path))
    return {pset["AssetId"]: product.GlobalId for product in model.by_type("IfcProduct")
            if (pset := ifcopenshell.util.element.get_psets(product).get("Pset_RailwayBIMAsset"))}


def test_explicit_single_and_three_track_projects_export_complete_ifc(tmp_path):
    for count in (1, 3):
        reconstruction = fixture_project(tmp_path / f"source-{count}", track_count=count)
        output = tmp_path / f"tracks-{count}.ifc"
        build_ifc(reconstruction, output)
        model = ifcopenshell.open(str(output))
        assert len(model.by_type("IfcRail")) == count * 2
        assert len(model.by_type("IfcAlignment")) == count
        tracks = [part for part in model.by_type("IfcRailwayPart") if part.PredefinedType == "TRACK"]
        assert {part.Name for part in tracks} == {f"Track {i}" for i in range(1, count + 1)}
        assert validate_ifc(output)["errors"] == []


def test_new_observation_keeps_same_project_asset_guids(tmp_path):
    original = fixture_project(tmp_path / "first")
    rescan = fixture_project(tmp_path / "rescan", observation=b"new survey observation")
    first, second = tmp_path / "first.ifc", tmp_path / "second.ifc"
    build_ifc(original, first)
    build_ifc(rescan, second)
    assert _guids(first) == _guids(second)
    first_register = json.loads(first.with_suffix(".asset-register.json").read_text())
    second_register = json.loads(second.with_suffix(".asset-register.json").read_text())
    assert first_register["source_sha256"] != second_register["source_sha256"]


def test_saved_ifc_project_uuid_must_match_delivery_contract(tmp_path):
    reconstruction = fixture_project(tmp_path / "source")
    output = tmp_path / "railway.ifc"
    build_ifc(reconstruction, output)
    assert validate_ifc(output)["errors"] == []
    model = ifcopenshell.open(str(output))
    metadata = ifcopenshell.util.element.get_psets(model.by_type("IfcProject")[0])["Pset_RailwayBIMProject"]
    pset = model.by_id(metadata["id"])
    identity = next(property for property in pset.HasProperties if property.Name == "ProjectId")
    identity.NominalValue = model.create_entity("IfcText", "121eb942-5202-4c4f-bbcb-66b6c493961d")
    model.write(str(output))
    report = validate_ifc(output)
    assert next(check for check in report["checks"] if check["name"] == "asset_register")["status"] == "passed"
    assert "ifc_project_identity" in {failure["check"] for failure in report["errors"]}
    identity.NominalValue = model.create_entity("IfcText", PROJECT_ID.replace("-", "").upper())
    model.write(str(output))
    register_path = output.with_suffix(".asset-register.json")
    register = json.loads(register_path.read_text())
    register["project_id"] = PROJECT_ID.upper()
    register_path.write_text(json.dumps(register))
    assert validate_ifc(output)["errors"] == []


@pytest.mark.parametrize("alignment_ids", [
    None,
    ["Survey alignment A", "Survey alignment B"],
])
def test_alignment_identity_survives_shared_and_renamed_track_labels(tmp_path, alignment_ids):
    reconstruction = fixture_project(tmp_path / "source")
    contract = json.loads((reconstruction / "project.json").read_text())
    for index, track in enumerate(contract["tracks"]):
        track["name"] = "Shared track label"
        if alignment_ids is not None:
            track["alignment_asset_id"] = alignment_ids[index]
    first, renamed = tmp_path / "first.ifc", tmp_path / "renamed.ifc"
    build_ifc(reconstruction, first, project_contract=contract)
    register = json.loads(first.with_suffix(".asset-register.json").read_text())
    expected_ids = set(alignment_ids or ["Track 1 measured alignment", "Track 2 measured alignment"])
    model = ifcopenshell.open(str(first))
    root_guids = [entity.GlobalId for entity in model.by_type("IfcRoot")]
    assert len(root_guids) == len(set(root_guids))
    assert {record["asset_id"] for record in register["alignments"]} == expected_ids
    for index, track in enumerate(contract["tracks"]):
        track["name"] = f"Renamed track {index + 1}"
    build_ifc(reconstruction, renamed, project_contract=contract)
    assert _guids(first) == _guids(renamed)


@pytest.mark.parametrize("alignment_ids", [
    ["Rail 01", "Survey alignment B"],
    ["Shared alignment", "Shared alignment"],
])
def test_alignment_id_collision_rejects_overwrite_without_changing_delivery(tmp_path, alignment_ids):
    reconstruction = fixture_project(tmp_path / "source")
    output = tmp_path / "railway.ifc"
    build_ifc(reconstruction, output)
    paths = [output, output.with_suffix(".railway-project.json"), output.with_suffix(".asset-register.json")]
    before = {path: path.read_bytes() for path in paths}
    contract = json.loads((reconstruction / "project.json").read_text())
    for track, asset_id in zip(contract["tracks"], alignment_ids):
        track["alignment_asset_id"] = asset_id
    with pytest.raises(ValueError, match="asset IDs must be unique"):
        build_ifc(reconstruction, output, overwrite=True, project_contract=contract)
    assert {path: path.read_bytes() for path in paths} == before


def test_verified_reference_facts_survive_ifc_export_and_validation(tmp_path):
    reconstruction = fixture_project(tmp_path / "source")
    output = tmp_path / "verified.ifc"
    build_ifc(reconstruction, output)
    model = ifcopenshell.open(str(output))
    for product in model.by_type("IfcRail"):
        pset = ifcopenshell.util.element.get_psets(product)["Pset_RailwayBIMAsset"]
        assert pset["HorizontalCRSStatus"] == "VERIFIED"
        assert pset["VerticalDatumStatus"] == "VERIFIED"
    crs = model.by_type("IfcProjectedCRS")[0]
    assert crs.Name == "EPSG:2056" and crs.VerticalDatum == "LN02"
    report = validate_ifc(output)
    assert report["errors"] == []
    assert not {"horizontal_crs_assumed", "vertical_datum_unknown"} & {item["code"] for item in report["review_items"]}


@pytest.mark.parametrize("corruption, expected_check", [
    ("shift", "fitted_measured_alignments"),
    ("bend", "fitted_measured_alignments"),
    ("identity", "alignment_register"),
    ("shortcut", "fitted_measured_alignments"),
])
def test_saved_alignment_corruption_is_rejected_with_matching_inventory(tmp_path, corruption, expected_check):
    reconstruction = fixture_project(tmp_path / "source")
    if corruption == "shortcut":
        path = reconstruction / "work/component_spec.json"
        spec = json.loads(path.read_text())
        for rail in spec["rails"][:2]:
            rail["v_coefficients"] = [.001, 0., rail["v_coefficients"][0]]
        path.write_text(json.dumps(spec))
    output = tmp_path / "railway.ifc"
    build_ifc(reconstruction, output)
    model = ifcopenshell.open(str(output))
    alignment = next(product for product in model.by_type("IfcAlignment")
                     if product.Name == "Track 1 measured alignment")
    if corruption == "shift":
        axis = model.create_entity("IfcAxis2Placement3D", Location=model.create_entity(
            "IfcCartesianPoint", Coordinates=(.5, 0., 0.)))
        alignment.ObjectPlacement = model.create_entity("IfcLocalPlacement",
            PlacementRelTo=alignment.ObjectPlacement, RelativePlacement=axis)
    elif corruption == "bend":
        curve = alignment.Representation.Representations[0].Items[0]
        point = curve.Points[len(curve.Points) // 2]
        x, y, z = point.Coordinates
        point.Coordinates = (x, y + .5, z)
    elif corruption == "shortcut":
        curve = alignment.Representation.Representations[0].Items[0]
        curve.Points = (curve.Points[0], curve.Points[-1])
    else:
        alignment.GlobalId = ifcopenshell.guid.new()
    model.write(str(output))
    report = validate_ifc(output)
    assert expected_check in {failure["check"] for failure in report["errors"]}
    assert next(check for check in report["checks"] if check["name"] == "measured_alignments")["status"] == "passed"
    assert next(check for check in report["checks"] if check["name"] == "asset_register")["status"] == "passed"


@pytest.mark.parametrize("asset_id, changes, corruption, expected_check", [
    ("Mast 01", {"height_m": 5.0}, "placement", "fitted_asset_centres"),
    ("Sleeper 1", {"depth": .3}, "placement", "fitted_asset_centres"),
    ("Rail 01", {"head_width_m": .09}, "placement", "fitted_rail_endpoints"),
    ("Rail 01", None, "interior_section", "fitted_rail_geometry"),
    ("Rail 01", {"head_width_m": .09}, "head_width", "fitted_rail_geometry"),
    ("Mast 01", {"height_m": 5.0}, "mast_height", "fitted_asset_dimensions"),
    ("Sleeper 1", {"width_u": .3, "length_v": 2.8, "depth": .3}, "sleeper_dimensions", "fitted_asset_dimensions"),
    ("Rail 01", None, "head_shift", "fitted_rail_geometry"),
    ("Rail 01", None, "crown_shift", "fitted_rail_geometry"),
])
def test_saved_physical_geometry_corruption_is_rejected_with_matching_parameters(
        tmp_path, asset_id, changes, corruption, expected_check):
    reconstruction = fixture_project(tmp_path / "source")
    if corruption == "sleeper_dimensions":
        spec_path = reconstruction / "work/component_spec.json"
        spec = json.loads(spec_path.read_text())
        for rail in spec["rails"][:2]:
            rail["v_coefficients"] = [.04, rail["v_coefficients"][0]]
        spec["sleepers"][0]["crossfall"] = .03
        spec_path.write_text(json.dumps(spec))
        (reconstruction / "work/alignment.json").write_text(json.dumps({"along": [0., 1.], "across": [-1., 0.]}))
        header_path = reconstruction / "work/source_header.json"
        header = json.loads(header_path.read_text())
        header["bounds_local"] = [[-15., -10., -2.], [10., 10., 6.]]
        header_path.write_text(json.dumps(header))
    output = tmp_path / "railway.ifc"
    build_ifc(reconstruction, output)
    if corruption == "head_width":
        assert validate_ifc(output)["errors"] == []
    if changes:
        rebuild_asset(output, asset_id, changes)
    valid = validate_ifc(output)
    assert valid["errors"] == []
    if changes:
        assert "edited_source_parameters" in {item["code"] for item in valid["review_items"]}
    model = ifcopenshell.open(str(output))
    product = model.by_guid(_guids(output)[asset_id])
    if corruption == "placement":
        axis = model.create_entity("IfcAxis2Placement3D", Location=model.create_entity(
            "IfcCartesianPoint", Coordinates=(.5, 0., 0.)))
        product.ObjectPlacement = model.create_entity("IfcLocalPlacement",
            PlacementRelTo=product.ObjectPlacement, RelativePlacement=axis)
    elif corruption == "mast_height":
        solid = product.Representation.Representations[0].Items[0]
        solid.Depth = 4.0
        x, y, z = solid.Position.Location.Coordinates
        solid.Position.Location.Coordinates = (x, y, z + .5)
    elif corruption == "sleeper_dimensions":
        solid = product.Representation.Representations[0].Items[0]
        for point in solid.SweptArea.OuterCurve.Points:
            lateral, height = point.Coordinates
            relative_height = height - .03 * lateral
            point.Coordinates = (lateral, -.15 + (relative_height + .15) * (.2 / .3) + .03 * lateral)
    else:
        mesh = product.Representation.Representations[0].Items[0]
        coordinates = np.asarray(mesh.Coordinates.CoordList)
        if corruption == "head_width":
            head = (coordinates[:, 2] > .96) & (coordinates[:, 2] < .999)
            assert np.any(head)
            coordinates[head, 1] = -.75 + (coordinates[head, 1] + .75) * (.07 / .09)
        elif corruption in {"head_shift", "crown_shift"}:
            shifted = (coordinates[:, 2] >= .964 - 1e-7 if corruption == "head_shift"
                       else np.isclose(coordinates[:, 2], 1., atol=1e-7))
            assert np.any(shifted)
            coordinates[shifted, 1] += .02
        else:
            stations = np.unique(coordinates[:, 0])
            endpoints = np.isin(coordinates[:, 0], stations[[0, -1]])
            retained = coordinates[endpoints].copy()
            coordinates[np.isclose(coordinates[:, 0], stations[len(stations) // 2]), 1] += .5
            assert np.array_equal(coordinates[endpoints], retained)
        mesh.Coordinates.CoordList = coordinates.tolist()
    model.write(str(output))
    report = validate_ifc(output)
    if corruption in {"mast_height", "sleeper_dimensions"}:
        assert next(check for check in report["checks"] if check["name"] == "fitted_asset_centres")["status"] == "passed"
    assert expected_check in {failure["check"] for failure in report["errors"]}
    assert next(check for check in report["checks"] if check["name"] == "asset_register")["status"] == "passed"
    assert next(check for check in report["checks"] if check["name"] == "fitted_measured_alignments")["status"] == "passed"


def test_relocated_delivery_validates_and_edits_without_source_inputs(tmp_path):
    reconstruction = fixture_project(tmp_path / "source")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    output = bundle / "railway.ifc"
    build_ifc(reconstruction, output)
    project = json.loads(output.with_suffix(".railway-project.json").read_text())
    assert not Path(project["ifc_path"]).is_absolute()
    assert not Path(project["reconstruction_dir"]).is_absolute()
    moved = tmp_path / "relocated"
    shutil.move(bundle, moved)
    shutil.rmtree(reconstruction)
    output = moved / output.name
    before = _guids(output)
    report = validate_ifc(output)
    assert report["errors"] == []
    assert "source_file_unavailable" in {item["code"] for item in report["review_items"]}
    rebuild_asset(output, "Mast 01", {"height_m": 5.0})
    assert _guids(output) == before
    model = ifcopenshell.open(str(output))
    mast = model.by_guid(before["Mast 01"])
    shape = ifcopenshell.geom.create_shape(ifcopenshell.geom.settings(), mast)
    vertices = np.asarray(shape.geometry.verts).reshape(-1, 3)
    assert np.isclose(np.ptp(vertices[:, 2]), 5.0)
    assert validate_ifc(output)["errors"] == []
